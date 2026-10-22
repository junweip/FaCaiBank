"""Fraud scoring before every transfer commits.

The primary scorer is the SageMaker serverless endpoint (see app/fraud_model).
If it is not configured, slow or failing, the API falls back to conservative
rules so a transfer is never committed without a score.
"""

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import config


logger = logging.getLogger(__name__)
SINGAPORE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class FraudFeatures:
    amount: float
    amount_to_balance: float
    hour_sgt: int
    is_new_payee: int
    transfers_last_hour: int

    def as_payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FraudResult:
    score: float
    model: str
    decision: str


def extract_features(
    amount: Decimal,
    sender_balance: Decimal,
    receiver_account_id: str,
    recent_entries: list[dict],
    now: datetime | None = None,
) -> FraudFeatures:
    now = now or datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    recent_debits = [entry for entry in recent_entries if entry.get("entry_type") == "DEBIT"]
    known_payees = {entry.get("counterparty_account_id") for entry in recent_debits}
    transfers_last_hour = 0
    for entry in recent_debits:
        try:
            created = datetime.fromisoformat(str(entry.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= one_hour_ago:
            transfers_last_hour += 1
    balance = max(Decimal(sender_balance), Decimal("0.01"))
    return FraudFeatures(
        amount=float(amount),
        amount_to_balance=round(float(Decimal(amount) / balance), 4),
        hour_sgt=now.astimezone(SINGAPORE).hour,
        is_new_payee=int(receiver_account_id not in known_payees),
        transfers_last_hour=transfers_last_hour,
    )


def rule_score(features: FraudFeatures) -> float:
    """Degraded-mode rules used when the model endpoint is unavailable."""
    score = 0.05
    if features.amount >= 10_000:
        score = 0.95
    elif features.amount >= 2_500:
        score = 0.65
    if features.is_new_payee and features.amount >= 1_000:
        score = max(score, 0.62)
    if features.transfers_last_hour >= 10:
        score = max(score, 0.7)
    if features.hour_sgt < 5 and features.amount >= 1_000:
        score = min(0.99, score + 0.1)
    return round(score, 4)


@lru_cache(maxsize=1)
def sagemaker_runtime():
    settings = config.settings
    return boto3.client(
        "sagemaker-runtime",
        region_name=settings.aws_region,
        config=Config(
            connect_timeout=1,
            read_timeout=settings.fraud_timeout_seconds,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def model_score(features: FraudFeatures) -> float | None:
    endpoint = config.settings.fraud_endpoint_name
    if not endpoint:
        return None
    try:
        response = sagemaker_runtime().invoke_endpoint(
            EndpointName=endpoint,
            ContentType="application/json",
            Accept="application/json",
            Body=json.dumps(features.as_payload()),
        )
        score = float(json.loads(response["Body"].read())["score"])
    except (BotoCoreError, ClientError, ValueError, KeyError, TypeError):
        logger.warning("Fraud endpoint %s unavailable; using rules", endpoint, exc_info=True)
        return None
    if math.isnan(score) or not 0 <= score <= 1:
        logger.warning("Fraud endpoint returned an invalid score %r; using rules", score)
        return None
    return score


def score_transfer(features: FraudFeatures) -> FraudResult:
    settings = config.settings
    score = model_score(features)
    model = f"sagemaker:{settings.fraud_endpoint_name}"
    if score is None:
        score = rule_score(features)
        model = "rules-fallback"
    if Decimal(str(score)) >= settings.fraud_block_threshold:
        decision = "BLOCK"
    elif Decimal(str(score)) >= settings.fraud_step_up_threshold:
        decision = "STEP_UP"
    else:
        decision = "ALLOW"
    return FraudResult(score=round(score, 4), model=model, decision=decision)
