"""Bedrock ops assistant: summarise alarms and findings for the on-call engineer.

Flow per SNS alert:
1. Mask obvious PII and account numbers locally (defence in depth).
2. Run the masked text through the Bedrock Guardrail (ApplyGuardrail, INPUT).
3. Ask Claude on Amazon Bedrock for a short triage summary and runbook steps.
4. Run the answer through the guardrail again (OUTPUT) and publish it to the
   ops-summaries SNS topic.

The assistant only suggests; it has no permissions to change infrastructure,
so a human approves every fix.
"""

import json
import logging
import os
import re
from functools import lru_cache

import boto3
from anthropic import AnthropicBedrockMantle, BetaRefusalFallbackMiddleware


logger = logging.getLogger()
logger.setLevel(logging.INFO)

MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-opus-5-5")
FALLBACK_MODEL_ID = os.getenv("BEDROCK_FALLBACK_MODEL_ID", "anthropic.claude-opus-4-8")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", os.getenv("AWS_REGION", "ap-southeast-1"))
RUNBOOK_BASE_URL = os.getenv("RUNBOOK_BASE_URL", "docs/runbooks")

MASKS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "[ID]"),
    (re.compile(r"\+?65[ -]?\d{4}[ -]?\d{4}\b|\b[89]\d{7}\b"), "[PHONE]"),
    (re.compile(r"\b[STFGM]\d{7}[A-Z]\b"), "[NRIC]"),
    (re.compile(r"\b\d{9,16}\b"), "[NUMBER]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[ACCESS_KEY]"),
]

SYSTEM_PROMPT = f"""You help the FaCaiBank on-call engineer triage an operational alert for a digital bank
running on AWS in ap-southeast-1 (Singapore). Write a short plain-text briefing with these sections:

What happened: one or two sentences in plain language.
Likely impact: which customer journeys or targets (99.99% availability, balance p95 under 200 ms,
transfer commit p95 under 1 s, RPO 0 / RTO 10 min) are at risk.
First checks: three to five concrete checks in the AWS console or CLI, most likely cause first.
Runbook: the most relevant file under {RUNBOOK_BASE_URL}/ (monitoring-and-alerts.md, restore.md,
rds-failover.md, worker-recovery.md, security-incident.md, deploy-and-rollback.md).

You only advise. Never claim you have changed anything, and phrase fixes as proposals for a human to approve.
Placeholders like [EMAIL] or [ID] are masked data; do not guess what they were."""


def mask(text: str) -> str:
    for pattern, replacement in MASKS:
        text = pattern.sub(replacement, text)
    return text


@lru_cache(maxsize=1)
def claude():
    return AnthropicBedrockMantle(
        aws_region=BEDROCK_REGION,
        middleware=[BetaRefusalFallbackMiddleware([{"model": FALLBACK_MODEL_ID}])],
    )


@lru_cache(maxsize=1)
def bedrock_runtime():
    return boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)


def apply_guardrail(text: str, source: str) -> str:
    response = bedrock_runtime().apply_guardrail(
        guardrailIdentifier=os.environ["GUARDRAIL_ID"],
        guardrailVersion=os.environ["GUARDRAIL_VERSION"],
        source=source,
        content=[{"text": {"text": text}}],
    )
    if response.get("action") == "GUARDRAIL_INTERVENED":
        outputs = response.get("outputs") or []
        logger.info("Guardrail intervened on %s", source)
        return "\n".join(output.get("text", "") for output in outputs) or "[content withheld by guardrail]"
    return text


def summarise(alert_text: str) -> str:
    response = claude().beta.messages.create(
        model=MODEL_ID,
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Alert received:\n\n{alert_text}"}],
    )
    if response.stop_reason == "refusal":
        logger.warning("Model declined to summarise the alert")
        return ""
    return "\n".join(block.text for block in response.content if block.type == "text").strip()


def fallback_summary(subject: str, alert_text: str) -> str:
    return (
        f"Automatic summary unavailable. Raw alert ({subject}):\n\n{alert_text[:3000]}\n\n"
        f"Start with {RUNBOOK_BASE_URL}/monitoring-and-alerts.md."
    )


def lambda_handler(event, context):
    sns = boto3.client("sns")
    published = 0
    for record in event.get("Records", []):
        message = record.get("Sns", {})
        subject = message.get("Subject") or "FaCaiBank alert"
        raw = message.get("Message", "")
        try:
            raw = json.dumps(json.loads(raw), indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
        safe_input = apply_guardrail(mask(raw), "INPUT")
        try:
            summary = summarise(safe_input)
        except Exception:
            logger.exception("Bedrock summarisation failed")
            summary = ""
        summary = apply_guardrail(summary, "OUTPUT") if summary else fallback_summary(subject, safe_input)
        sns.publish(
            TopicArn=os.environ["OPS_TOPIC_ARN"],
            Subject=f"[ops assistant] {subject}"[:100],
            Message=f"{summary}\n\n-- Suggested by the FaCaiBank ops assistant (Claude on Amazon Bedrock). "
            "A human must approve any change.",
        )
        published += 1
    return {"published": published}
