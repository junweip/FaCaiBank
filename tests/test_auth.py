import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.backend_api import auth, cache, config, ledger
from conftest import TABLE


POOL = "ap-southeast-1_testpool"
ISSUER = f"https://cognito-idp.ap-southeast-1.amazonaws.com/{POOL}"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwksClient:
    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": PRIVATE_KEY.public_key()})()


@pytest.fixture(autouse=True)
def cognito(aws, monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        config.Settings(
            cognito_user_pool_id=POOL,
            cognito_app_client_id="web-client",
            aws_region="ap-southeast-1",
            ledger_table_name=TABLE,
        ),
    )
    monkeypatch.setattr(auth, "jwks_client", lambda: FakeJwksClient())
    cache.get_cache.cache_clear()
    yield
    cache.get_cache.cache_clear()


def token(key=PRIVATE_KEY, **overrides):
    now = int(time.time())
    claims = {
        "sub": "3f1c7d2e-0000-4000-8000-000000000001",
        "username": "3f1c7d2e-0000-4000-8000-000000000001",
        "iss": ISSUER,
        "client_id": "web-client",
        "token_use": "access",
        "scope": "aws.cognito.signin.user.admin",
        "auth_time": now - 30,
        "iat": now - 30,
        "exp": now + 3600,
        "jti": "token-1",
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def test_valid_access_token_is_accepted():
    user = auth.current_user(f"Bearer {token()}")

    assert user.subject == "3f1c7d2e-0000-4000-8000-000000000001"
    assert user.token_id == "token-1"


@pytest.mark.parametrize(
    "bad_token",
    [
        token(key=OTHER_KEY),
        token(exp=int(time.time()) - 120),
        token(iss="https://cognito-idp.ap-southeast-1.amazonaws.com/other-pool"),
        token(token_use="id"),
        token(client_id="another-app"),
    ],
    ids=["wrong-signature", "expired", "wrong-issuer", "id-token", "wrong-client"],
)
def test_invalid_tokens_are_rejected(bad_token):
    with pytest.raises(HTTPException) as error:
        auth.current_user(f"Bearer {bad_token}")

    assert error.value.status_code == 401


def test_missing_header_is_rejected():
    with pytest.raises(HTTPException) as error:
        auth.current_user(None)

    assert error.value.status_code == 401


def test_revoked_session_is_rejected():
    user = auth.current_user(f"Bearer {token(jti='to-revoke')}")
    auth.revoke_session(user)

    with pytest.raises(HTTPException, match="signed out"):
        auth.current_user(f"Bearer {token(jti='to-revoke')}")


def test_sign_out_handled_by_another_worker_is_honoured():
    # Each worker has its own in-process cache, so a revocation made by one worker
    # is invisible to the others unless it is also written to the shared ledger.
    user = auth.current_user(f"Bearer {token(jti='signed-out-elsewhere')}")
    auth.revoke_session(user)
    cache.get_cache.cache_clear()  # a different worker: a fresh, empty in-process cache
    assert not cache.get_cache().exists(cache.revoked_key("signed-out-elsewhere"))

    with pytest.raises(HTTPException, match="signed out"):
        auth.current_user(f"Bearer {token(jti='signed-out-elsewhere')}")


def test_revocation_is_stored_durably_and_expires_with_the_token():
    expires_at = int(time.time()) + 600
    user = auth.current_user(f"Bearer {token(jti='durable', exp=expires_at)}")
    auth.revoke_session(user)

    assert ledger.is_session_revoked("durable")
    assert not ledger.is_session_revoked("never-revoked")
    item = ledger.from_av(
        ledger.dynamodb_client().get_item(
            TableName=TABLE, Key=ledger.to_av(ledger.session_revocation_key("durable"))
        )["Item"]
    )
    assert item["expires_at"] == expires_at
    assert item["record_type"] == "REVOKED_SESSION"


def test_shared_cache_answers_without_a_ledger_read(monkeypatch):
    monkeypatch.setattr(cache.MemoryCache, "shared", True)  # behave like ElastiCache
    store = cache.get_cache()
    monkeypatch.setattr(ledger, "is_session_revoked", lambda token_id: pytest.fail("ledger read"))

    assert auth.current_user(f"Bearer {token(jti='fast-path')}").token_id == "fast-path"
    store.set(cache.revoked_key("fast-path"), True, 60)
    with pytest.raises(HTTPException, match="signed out"):
        auth.current_user(f"Bearer {token(jti='fast-path')}")


def test_step_up_challenge_follows_rfc_9470():
    stale = auth.current_user(f"Bearer {token(auth_time=int(time.time()) - 900)}")

    with pytest.raises(HTTPException) as error:
        auth.require_recent_authentication(stale, "high_value_transfer")

    assert error.value.status_code == 401
    assert error.value.detail["code"] == "step_up_required"
    assert 'error="insufficient_user_authentication"' in error.value.headers["WWW-Authenticate"]
    auth.require_recent_authentication(auth.current_user(f"Bearer {token()}"), "high_value_transfer")
