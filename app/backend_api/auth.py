"""Cognito access-token verification and step-up authentication.

Every banking request must carry a Cognito access token in the Authorization
header. Tokens are verified locally against the user pool's JWKS (RS256),
issuer, token_use and app client. High-risk operations additionally require a
recent sign-in (RFC 9470 style ``insufficient_user_authentication`` challenge).
"""

import logging
import time
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException

from . import cache, config, ledger


logger = logging.getLogger(__name__)
REQUIRED_CLAIMS = ["exp", "iat", "sub", "token_use", "client_id", "jti", "auth_time"]


@dataclass(frozen=True)
class AuthenticatedUser:
    subject: str
    username: str
    auth_time: int
    token_id: str
    expires_at: int
    access_token: str


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer realm="facaibank"'},
    )


def issuer() -> str:
    settings = config.settings
    if not settings.cognito_user_pool_id:
        raise HTTPException(status_code=503, detail="COGNITO_USER_POOL_ID is not configured")
    return f"https://cognito-idp.{settings.aws_region}.amazonaws.com/{settings.cognito_user_pool_id}"


@lru_cache(maxsize=1)
def jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer()}/.well-known/jwks.json", cache_keys=True, lifespan=3600)


def decode_access_token(token: str) -> dict:
    settings = config.settings
    if not settings.cognito_app_client_id:
        raise HTTPException(status_code=503, detail="COGNITO_APP_CLIENT_ID is not configured")
    try:
        signing_key = jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer(),
            leeway=30,
            options={"require": REQUIRED_CLAIMS, "verify_aud": False},
        )
    except jwt.PyJWKClientConnectionError as error:
        logger.exception("Could not download the Cognito signing keys")
        raise HTTPException(status_code=503, detail="Sign-in service is unavailable") from error
    except jwt.PyJWTError as error:
        raise _unauthorized("Invalid or expired access token") from error

    if claims.get("token_use") != "access":
        raise _unauthorized("An access token is required")
    if claims.get("client_id") != settings.cognito_app_client_id:
        raise _unauthorized("Token was issued to a different application")
    return claims


def is_revoked(token_id: str) -> bool:
    store = cache.get_cache()
    if store.exists(cache.revoked_key(token_id)):
        return True
    # A process-local cache never sees a sign-out handled by another worker, so a
    # miss there proves nothing. The ledger table is the shared record.
    return not store.shared and ledger.is_session_revoked(token_id)


def current_user(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("Sign in to continue")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(token)
    if is_revoked(claims["jti"]):
        raise _unauthorized("This session has been signed out")
    return AuthenticatedUser(
        subject=claims["sub"],
        username=claims.get("username", claims["sub"]),
        auth_time=int(claims["auth_time"]),
        token_id=claims["jti"],
        expires_at=int(claims["exp"]),
        access_token=token,
    )


def require_recent_authentication(user: AuthenticatedUser, reason: str) -> None:
    max_age = config.settings.step_up_max_age_seconds
    if time.time() - user.auth_time <= max_age:
        return
    raise HTTPException(
        status_code=401,
        detail={
            "code": "step_up_required",
            "message": "Confirm it's you with your password and authenticator code.",
            "reason": reason,
            "max_age": max_age,
        },
        headers={
            "WWW-Authenticate": (
                'Bearer error="insufficient_user_authentication", '
                f'error_description="{reason}", max_age={max_age}'
            )
        },
    )


def revoke_session(user: AuthenticatedUser) -> None:
    ttl = max(1, user.expires_at - int(time.time()))
    cache.get_cache().set(cache.revoked_key(user.token_id), True, ttl)
    ledger.revoke_session(user.token_id, user.expires_at)
