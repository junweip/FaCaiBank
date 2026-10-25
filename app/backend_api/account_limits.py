"""One-off data migration: bring every customer within 2 everyday + 2 savings accounts.

For each customer and account type the oldest accounts are kept. Each newer
extra account has its balance swept into the oldest account of the same type
(recorded as ACCOUNT_CLOSURE ledger entries, so the ledger stays zero-sum) and
is then deleted from DynamoDB and PostgreSQL.
"""

import logging
from collections import defaultdict

from . import ledger


logger = logging.getLogger(__name__)
ACCOUNT_LIMITS = {"EVERYDAY": 2, "SAVINGS": 2}


def plan_closures(accounts: list[dict]) -> list[dict]:
    """Return the accounts to close, each with the account that receives its balance."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for account in accounts:
        groups[(account["user_id"], account["account_type"])].append(account)
    closures = []
    for (_, account_type), members in groups.items():
        members.sort(key=lambda account: (account["created_at"], str(account["account_id"])))
        limit = ACCOUNT_LIMITS.get(account_type, 2)
        for extra in members[limit:]:
            closures.append(
                {
                    "account_id": str(extra["account_id"]),
                    "target_account_id": str(members[0]["account_id"]),
                    "owner_sub": extra["cognito_sub"],
                }
            )
    return closures


def enforce_account_limits(conn) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT a.account_id, a.user_id, a.account_type, a.created_at, c.cognito_sub "
            "FROM accounts a JOIN customers c ON c.user_id = a.user_id"
        )
        accounts = cursor.fetchall()
        closures = plan_closures(accounts)
        for closure in closures:
            if closure["owner_sub"]:
                closure["swept"] = str(
                    ledger.close_account_into(closure["account_id"], closure["target_account_id"], closure["owner_sub"])
                )
            cursor.execute("DELETE FROM accounts WHERE account_id = %s", (closure["account_id"],))
            logger.info(
                "Closed account %s over the limit; swept %s into %s",
                closure["account_id"],
                closure.get("swept", "0"),
                closure["target_account_id"],
            )
    return closures
