import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from botocore.exceptions import ClientError

from app.backend_api import cache, config, db, external_bank, fraud, metrics


def test_fraud_features_capture_new_payees_velocity_and_singapore_hour():
    now = datetime(2026, 10, 22, 18, 30, tzinfo=timezone.utc)
    recent = [
        {"entry_type": "DEBIT", "counterparty_account_id": "known", "created_at": (now - timedelta(minutes=5)).isoformat()},
        {"entry_type": "DEBIT", "counterparty_account_id": "known", "created_at": (now - timedelta(hours=3)).isoformat()},
        {"entry_type": "CREDIT", "counterparty_account_id": "other", "created_at": now.isoformat()},
    ]

    features = fraud.extract_features(Decimal("250.00"), Decimal("1000.00"), "new-payee", recent, now)

    assert features.is_new_payee == 1
    assert features.transfers_last_hour == 1
    assert features.hour_sgt == 2
    assert features.amount_to_balance == 0.25
    assert fraud.extract_features(Decimal("1"), Decimal("10"), "known", recent, now).is_new_payee == 0


def test_rule_fallback_thresholds_drive_decisions(monkeypatch):
    monkeypatch.setattr(config, "settings", config.Settings(fraud_endpoint_name=""))

    def decision(amount, new_payee=0, hour=12):
        features = fraud.FraudFeatures(amount, 0.1, hour, new_payee, 0)
        return fraud.score_transfer(features).decision

    assert decision(50) == "ALLOW"
    assert decision(3000) == "STEP_UP"
    assert decision(1500, new_payee=1) == "STEP_UP"
    assert decision(12000) == "BLOCK"


def test_sagemaker_failure_falls_back_to_rules(monkeypatch):
    class BrokenRuntime:
        def invoke_endpoint(self, **kwargs):
            raise ClientError({"Error": {"Code": "ModelError", "Message": "cold"}}, "InvokeEndpoint")

    class WorkingRuntime:
        def invoke_endpoint(self, **kwargs):
            self.kwargs = kwargs
            return {"Body": io.BytesIO(b'{"score": 0.31}')}

    monkeypatch.setattr(config, "settings", config.Settings(fraud_endpoint_name="facaibank-fraud"))
    features = fraud.FraudFeatures(100.0, 0.1, 12, 0, 0)
    monkeypatch.setattr(fraud, "sagemaker_runtime", lambda: BrokenRuntime())
    assert fraud.score_transfer(features).model == "rules-fallback"

    runtime = WorkingRuntime()
    monkeypatch.setattr(fraud, "sagemaker_runtime", lambda: runtime)
    result = fraud.score_transfer(features)
    assert result.score == 0.31
    assert result.model == "sagemaker:facaibank-fraud"
    assert json.loads(runtime.kwargs["Body"])["amount"] == 100.0


def test_mock_partner_bank_is_deterministic_and_enforces_rules():
    first = external_bank.pull_funds("DBS", "123456789", Decimal("10.00"), "user:key")
    again = external_bank.pull_funds("DBS", "123456789", Decimal("10.00"), "user:key")

    assert first.approved and first.reference == again.reference
    assert not external_bank.push_funds("DBS", "123450000", Decimal("10.00"), "k").approved
    assert not external_bank.push_funds("DBS", "123456789", Decimal("20000.01"), "k").approved
    assert external_bank.validate_destination("XYZ", "123456789") == "Unsupported partner bank"
    assert external_bank.validate_destination("DBS", "12ab") is not None


def test_latency_recorder_publishes_raw_values_for_percentiles():
    recorder = metrics.LatencyRecorder("FaCaiBank/API", "dev", "ap-southeast-1", enabled=True)
    for millis in (12.4, 12.6, 180.0, 950.0):
        recorder.record("balance", millis, 200)
    recorder.record("transfer", 400.0, 503)
    recorder.record("open_account", 30.0, 201)
    recorder.record("health", 1.0, 200)

    data = {(datum["MetricName"], datum["Dimensions"][1]["Value"]): datum for datum in recorder.drain()}

    latency = data[("Latency", "balance")]
    assert latency["Values"] == [12.0, 13.0, 180.0, 950.0]
    assert latency["Counts"] == [1.0, 1.0, 1.0, 1.0]
    assert data[("Requests", "balance")]["Value"] == 4
    assert data[("Errors5xx", "transfer")]["Value"] == 1
    assert data[("Requests", "other")]["Value"] == 1
    assert not any(endpoint == "health" for _, endpoint in data)
    assert recorder.drain() == []


def test_memory_cache_expires_entries(monkeypatch):
    store = cache.MemoryCache()
    clock = [100.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: clock[0])

    store.set("profile:a", {"name": "A"}, ttl_seconds=60)
    assert store.get("profile:a") == {"name": "A"}
    clock[0] += 61
    assert store.get("profile:a") is None


def test_redis_cache_fails_open():
    class Down:
        def get(self, key):
            raise ConnectionError("down")

        exists = set = delete = get

    store = cache.RedisCache(Down())

    assert store.get("x") is None
    assert store.exists("x") is False
    store.set("x", 1, 10)


def _profile_rows(*nicknames):
    customer = {
        "user_id": "c1",
        "full_name": "A",
        "email": "a@example.com",
        "kyc_status": "VERIFIED_MOCK",
        "kyc_verified_at": None,
        "alert_subscription_arn": None,
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    accounts = [
        {"account_id": f"id-{nickname}", "account_type": "EVERYDAY", "nickname": nickname, "currency": "SGD", "created_at": None}
        for nickname in nicknames
    ]
    return [customer, accounts]


def test_profile_is_not_held_in_a_process_local_cache(monkeypatch):
    # Two workers each have their own MemoryCache, so a write handled by the other
    # one can never invalidate this copy. The profile must be read fresh instead.
    from conftest import FakeDatabase
    from app.backend_api import main

    database = FakeDatabase()
    monkeypatch.setattr(db, "primary", database)
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    monkeypatch.setattr(cache, "get_cache", lambda store=cache.MemoryCache(): store)

    database.results = _profile_rows("first")
    assert [a["nickname"] for a in main.load_profile("user-a")["accounts"]] == ["first"]

    database.results = _profile_rows("first", "second")
    assert [a["nickname"] for a in main.load_profile("user-a")["accounts"]] == ["first", "second"]


def test_profile_is_cached_and_invalidated_in_a_shared_cache(monkeypatch):
    from conftest import FakeDatabase
    from app.backend_api import main

    class SharedCache(cache.MemoryCache):
        shared = True

    database = FakeDatabase()
    monkeypatch.setattr(db, "primary", database)
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    monkeypatch.setattr(cache, "get_cache", lambda store=SharedCache(): store)

    database.results = _profile_rows("first")
    main.load_profile("user-a")
    queries = len(database.statements)

    assert main.load_profile("user-a")["accounts"][0]["nickname"] == "first"
    assert len(database.statements) == queries

    main.invalidate_profile("user-a")
    database.results = _profile_rows("first", "second")
    assert len(main.load_profile("user-a")["accounts"]) == 2


def test_account_limit_plan_keeps_the_two_oldest_of_each_type():
    from datetime import datetime, timedelta
    from app.backend_api import account_limits

    start = datetime(2026, 10, 1)
    accounts = [
        {"account_id": f"e{index}", "user_id": "u1", "account_type": "EVERYDAY", "created_at": start + timedelta(days=index), "cognito_sub": "s1"}
        for index in range(4)
    ] + [
        {"account_id": "s0", "user_id": "u1", "account_type": "SAVINGS", "created_at": start, "cognito_sub": "s1"},
        {"account_id": "x0", "user_id": "u2", "account_type": "EVERYDAY", "created_at": start, "cognito_sub": "s2"},
    ]

    plan = account_limits.plan_closures(accounts)

    assert sorted(item["account_id"] for item in plan) == ["e2", "e3"]
    assert {item["target_account_id"] for item in plan} == {"e0"}


def test_migrations_apply_in_order_once_under_an_advisory_lock(monkeypatch):
    data_migrations = []
    monkeypatch.setattr(db, "DATA_MIGRATIONS", {"0003_data": lambda conn: data_migrations.append(conn)})

    class Cursor:
        def __init__(self):
            self.statements = []
            self.done = [{"version": "0001_initial"}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, parameters=None):
            self.statements.append((statement, parameters))

        def fetchall(self):
            return self.done

    class Connection:
        def __init__(self):
            self.cursor_obj = Cursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            pass

    connection = Connection()

    applied = db.apply_migrations(connection)

    statements = [statement for statement, _ in connection.cursor_obj.statements]
    assert statements[0].startswith("SELECT pg_advisory_xact_lock")
    assert applied == ["0002_customer_identity", "0003_data"]
    assert len(data_migrations) == 1
    assert any("cognito_sub" in statement for statement in statements)


def test_database_endpoint_accepts_terraform_output_with_port():
    assert db.parse_database_endpoint("facaibank.example.rds.amazonaws.com:5432", 5432) == (
        "facaibank.example.rds.amazonaws.com",
        5432,
    )
