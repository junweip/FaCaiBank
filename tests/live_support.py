"""Shared helpers for the live (deployed-environment) test scripts.

Creates disposable Cognito customers with TOTP MFA, signs them in, and calls the
FaCaiBank API as those customers. Requires operator AWS credentials with
cognito-idp admin rights on the user pool. Never point these scripts at
production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

EVIDENCE_DIR = Path(__file__).parent / "evidence"
TINY_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def totp(secret: str, for_time: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 time-based one-time password (SHA-1), as authenticator apps compute it."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if for_time is None else for_time) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return f"{code:0{digits}d}"


def write_evidence(name: str, evidence: dict[str, Any], path: Path | None = None) -> Path:
    path = path or EVIDENCE_DIR / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    return path


@dataclass
class CognitoCustomer:
    """A disposable customer login with TOTP MFA enrolled."""

    region: str
    user_pool_id: str
    client_id: str
    email: str
    password: str
    totp_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    last_totp_step: int = -1
    _client: Any = field(default=None, repr=False)

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("cognito-idp", region_name=self.region)
        return self._client

    @classmethod
    def create(cls, region: str, user_pool_id: str, client_id: str, label: str = "test") -> "CognitoCustomer":
        customer = cls(
            region=region,
            user_pool_id=user_pool_id,
            client_id=client_id,
            email=f"facaibank-{label}-{uuid.uuid4().hex[:10]}@example.com",
            password=f"Fc!{secrets.token_urlsafe(12)}9a",
        )
        client = customer.client
        client.sign_up(
            ClientId=client_id,
            Username=customer.email,
            Password=customer.password,
            UserAttributes=[{"Name": "email", "Value": customer.email}],
        )
        client.admin_confirm_sign_up(UserPoolId=user_pool_id, Username=customer.email)
        client.admin_update_user_attributes(
            UserPoolId=user_pool_id,
            Username=customer.email,
            UserAttributes=[{"Name": "email_verified", "Value": "true"}],
        )
        start = client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=client_id,
            AuthParameters={"USERNAME": customer.email, "PASSWORD": customer.password},
        )
        if start.get("ChallengeName") != "MFA_SETUP":
            raise RuntimeError(f"Expected MFA_SETUP, got {start.get('ChallengeName')}")
        association = client.associate_software_token(Session=start["Session"])
        customer.totp_secret = association["SecretCode"]
        verified = client.verify_software_token(
            Session=association["Session"], UserCode=customer._fresh_code(), FriendlyDeviceName="live-tests"
        )
        result = client.respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName="MFA_SETUP",
            Session=verified["Session"],
            ChallengeResponses={"USERNAME": customer.email},
        )
        customer._store(result)
        return customer

    def _fresh_code(self) -> str:
        """Cognito rejects a TOTP code that was already used, so wait for a new step."""
        step = int(time.time() // 30)
        if step <= self.last_totp_step:
            time.sleep(30 - time.time() % 30 + 1)
            step = int(time.time() // 30)
        self.last_totp_step = step
        return totp(self.totp_secret)

    def _store(self, result: dict) -> None:
        tokens = result.get("AuthenticationResult")
        if not tokens:
            raise RuntimeError(f"Sign-in did not complete: {result.get('ChallengeName')}")
        self.access_token = tokens["AccessToken"]
        self.refresh_token = tokens.get("RefreshToken", self.refresh_token)

    def sign_in(self) -> str:
        """Full password + TOTP sign-in (also satisfies step-up authentication)."""
        start = self.client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=self.client_id,
            AuthParameters={"USERNAME": self.email, "PASSWORD": self.password},
        )
        result = self.client.respond_to_auth_challenge(
            ClientId=self.client_id,
            ChallengeName="SOFTWARE_TOKEN_MFA",
            Session=start["Session"],
            ChallengeResponses={
                "USERNAME": start.get("ChallengeParameters", {}).get("USER_ID_FOR_SRP", self.email),
                "SOFTWARE_TOKEN_MFA_CODE": self._fresh_code(),
            },
        )
        self._store(result)
        return self.access_token

    def delete(self) -> None:
        try:
            self.client.admin_delete_user(UserPoolId=self.user_pool_id, Username=self.email)
        except self.client.exceptions.UserNotFoundException:
            pass


class BankApi:
    """Minimal JSON client for the FaCaiBank API (stdlib only, so threads are cheap)."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict, dict]:
        request_headers = dict(headers or {})
        if self.token:
            request_headers.setdefault("Authorization", f"Bearer {self.token}")
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            request_headers.setdefault("Content-Type", content_type)
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()
            try:
                parsed = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                parsed = {"raw": payload[:300].decode("utf-8", "replace")}
            return response.status, parsed, dict(response.headers)

    def expect(self, status: int, method: str, path: str, body: Any = None, **kwargs) -> dict:
        code, payload, _ = self.request(method, path, body, **kwargs)
        if code != status:
            raise AssertionError(f"{method} {path}: expected HTTP {status}, got {code}: {payload}")
        return payload

    def upload_kyc(self) -> dict:
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"id.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n"
        ).encode() + TINY_PDF + f"\r\n--{boundary}--\r\n".encode()
        return self.expect(
            200, "POST", "/kyc/upload", raw_body=body, content_type=f"multipart/form-data; boundary={boundary}"
        )


def provision_customer(
    api_url: str,
    region: str,
    user_pool_id: str,
    client_id: str,
    label: str,
    accounts: int = 1,
    deposit: str = "5000.00",
) -> tuple[CognitoCustomer, BankApi, list[str]]:
    """Create a verified customer with funded accounts, ready for money movement."""
    customer = CognitoCustomer.create(region, user_pool_id, client_id, label)
    api = BankApi(api_url, customer.access_token)
    api.expect(201, "POST", "/me", {"full_name": f"Live Test {label.title()}"})
    api.upload_kyc()
    account_ids = []
    for _ in range(accounts):
        account_id = api.expect(201, "POST", "/accounts", {"account_type": "EVERYDAY", "nickname": label})["account_id"]
        if deposit and float(deposit) > 0:
            api.expect(
                201,
                "POST",
                f"/accounts/{account_id}/deposits",
                {"amount": deposit, "bank_code": "DBS", "external_account": "123456789"},
                headers={"Idempotency-Key": f"provision-{uuid.uuid4()}"},
            )
        account_ids.append(account_id)
    return customer, api, account_ids
