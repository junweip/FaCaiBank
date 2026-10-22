"""Mock partner-bank integration for deposits and withdrawals.

FAST/PayNow settlement is out of scope for the prototype. This module stands in
for the partner bank: it validates the external account, applies partner
limits and returns a deterministic reference, so retries with the same
Idempotency-Key get the same answer.
"""

import hashlib
import re
import time
from dataclasses import dataclass
from decimal import Decimal

from . import config


PARTNER_BANKS = {
    "DBS": "DBS Bank",
    "OCBC": "OCBC Bank",
    "UOB": "United Overseas Bank",
    "SCB": "Standard Chartered",
}
PARTNER_LIMIT = Decimal("20000.00")
ACCOUNT_NUMBER = re.compile(r"\d{9,12}")


@dataclass(frozen=True)
class BankResponse:
    approved: bool
    reference: str
    reason: str | None = None


def _reference(operation: str, bank_code: str, idempotency_reference: str) -> str:
    digest = hashlib.sha256(f"{operation}:{bank_code}:{idempotency_reference}".encode()).hexdigest()
    return f"MOCK-{bank_code}-{digest[:12].upper()}"


def validate_destination(bank_code: str, account_number: str) -> str | None:
    if bank_code not in PARTNER_BANKS:
        return "Unsupported partner bank"
    if not ACCOUNT_NUMBER.fullmatch(account_number):
        return "External account number must be 9 to 12 digits"
    return None


def _simulate_latency() -> None:
    latency_ms = config.settings.external_bank_latency_ms
    if latency_ms > 0:
        time.sleep(latency_ms / 1000)


def _decide(operation: str, bank_code: str, account_number: str, amount: Decimal, reference: str) -> BankResponse:
    _simulate_latency()
    invalid = validate_destination(bank_code, account_number)
    if invalid:
        return BankResponse(False, _reference(operation, bank_code, reference), invalid)
    if account_number.endswith("0000"):
        return BankResponse(False, _reference(operation, bank_code, reference), "External account is closed")
    if amount > PARTNER_LIMIT:
        return BankResponse(
            False,
            _reference(operation, bank_code, reference),
            f"Amount exceeds the partner limit of {PARTNER_LIMIT} {config.settings.currency}",
        )
    return BankResponse(True, _reference(operation, bank_code, reference))


def pull_funds(bank_code: str, account_number: str, amount: Decimal, reference: str) -> BankResponse:
    """Debit the customer's external account (deposit into FaCaiBank)."""
    return _decide("PULL", bank_code, account_number, amount, reference)


def push_funds(bank_code: str, account_number: str, amount: Decimal, reference: str) -> BankResponse:
    """Credit the customer's external account (withdrawal from FaCaiBank)."""
    return _decide("PUSH", bank_code, account_number, amount, reference)
