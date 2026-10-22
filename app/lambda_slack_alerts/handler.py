"""Forward operational SNS alerts (page, ticket, security, ops summaries) to Slack.

The incoming-webhook URL lives in Secrets Manager. Message formats handled:
CloudWatch alarm JSON, EventBridge findings (GuardDuty, Security Hub, Macie),
AWS Budgets plain text and ops-assistant summaries.
"""

import json
import logging
import os
import urllib.request
from functools import lru_cache

import boto3


logger = logging.getLogger()
logger.setLevel(logging.INFO)
SEVERITY_EMOJI = {"page": ":rotating_light:", "security": ":lock:", "ticket": ":ticket:", "ops": ":robot_face:"}


@lru_cache(maxsize=1)
def webhook_url() -> str:
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["WEBHOOK_SECRET_ARN"])
    return secret["SecretString"].strip()


def channel_kind(topic_arn: str) -> str:
    name = topic_arn.rsplit(":", 1)[-1]
    for kind in ("security", "ticket", "ops", "page"):
        if kind in name:
            return kind
    return "page"


def describe(message: str, subject: str | None) -> str:
    try:
        body = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return f"*{subject or 'Alert'}*\n{message[:2500]}"
    if "AlarmName" in body:
        return (
            f"*{body['AlarmName']}* is *{body.get('NewStateValue', '?')}*\n"
            f"{body.get('AlarmDescription') or ''}\n_{body.get('NewStateReason', '')[:500]}_"
        )
    detail = body.get("detail", {}) if isinstance(body, dict) else {}
    if body.get("detail-type") == "GuardDuty Finding":
        return f"*GuardDuty severity {detail.get('severity')}*: {detail.get('title')}\n{detail.get('description', '')[:800]}"
    if body.get("detail-type", "").startswith("Security Hub Findings"):
        finding = (detail.get("findings") or [{}])[0]
        severity = finding.get("Severity", {}).get("Label")
        return f"*Security Hub {severity}*: {finding.get('Title')}\n{finding.get('Description', '')[:800]}"
    if body.get("detail-type") == "Macie Finding":
        return f"*Macie finding*: {detail.get('title')} (severity {detail.get('severity', {}).get('description')})"
    return f"*{subject or body.get('detail-type', 'Alert')}*\n```{json.dumps(body)[:2500]}```"


def lambda_handler(event, context):
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        kind = channel_kind(sns.get("TopicArn", ""))
        text = f"{SEVERITY_EMOJI[kind]} [{kind.upper()}] " + describe(sns.get("Message", ""), sns.get("Subject"))
        request = urllib.request.Request(
            webhook_url(),
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            logger.info("Posted %s alert to Slack: HTTP %s", kind, response.status)
    return {"posted": len(event.get("Records", []))}
