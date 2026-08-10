from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from secrets import token_urlsafe

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class AdminSessionError(RuntimeError):
    pass


class AdminSessionClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(pattern=r"^1$")
    actor_id: str = Field(min_length=1, max_length=128)
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    csrf_token: str = Field(min_length=20, max_length=256)


@dataclass(frozen=True, slots=True, repr=False)
class IssuedAdminSession:
    token: str
    csrf_token: str
    expires_at: int

    def __repr__(self) -> str:
        return f"IssuedAdminSession(csrf_token=<redacted>, expires_at={self.expires_at})"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or len(value) > 8192:
        raise AdminSessionError("invalid administrator session")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise AdminSessionError("invalid administrator session") from exc


class AdminSessionManager:
    def __init__(
        self,
        *,
        session_secret: str | SecretStr,
        bootstrap_token: str | SecretStr | None,
        ttl_seconds: int = 3_600,
    ) -> None:
        self._session_secret = SecretStr(session_secret) if isinstance(session_secret, str) else session_secret
        self._bootstrap_token = (
            SecretStr(bootstrap_token) if isinstance(bootstrap_token, str) else bootstrap_token
        )
        if len(self._session_secret.get_secret_value()) < 32:
            raise ValueError("administrator session Secret is too short")
        if self._bootstrap_token is not None and len(self._bootstrap_token.get_secret_value()) < 32:
            raise ValueError("administrator bootstrap token is too short")
        if not 300 <= ttl_seconds <= 86_400:
            raise ValueError("administrator session TTL is out of bounds")
        self._ttl_seconds = ttl_seconds

    def issue(self, bootstrap_token: str, *, actor_id: str = "admin", now: int) -> IssuedAdminSession:
        configured = self._bootstrap_token
        if configured is None or not hmac.compare_digest(configured.get_secret_value(), bootstrap_token):
            raise AdminSessionError("administrator authentication failed")
        if not actor_id or len(actor_id) > 128:
            raise AdminSessionError("administrator identity is invalid")
        claims = AdminSessionClaims(
            version="1",
            actor_id=actor_id,
            issued_at=now,
            expires_at=now + self._ttl_seconds,
            csrf_token=token_urlsafe(24),
        )
        payload = _encode(json.dumps(claims.model_dump(mode="json"), separators=(",", ":"), sort_keys=True).encode())
        body = f"zpa1.{payload}"
        signature = hmac.new(
            self._session_secret.get_secret_value().encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        return IssuedAdminSession(
            token=f"{body}.{signature}",
            csrf_token=claims.csrf_token,
            expires_at=claims.expires_at,
        )

    def verify(self, token: str, *, now: int) -> AdminSessionClaims:
        try:
            version, payload, signature = token.split(".", 2)
            if version != "zpa1" or len(signature) != 64:
                raise AdminSessionError("invalid administrator session")
            body = f"{version}.{payload}"
            expected = hmac.new(
                self._session_secret.get_secret_value().encode(), body.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise AdminSessionError("invalid administrator session")
            claims = AdminSessionClaims.model_validate(json.loads(_decode(payload)))
        except (AdminSessionError, ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise AdminSessionError("invalid administrator session") from exc
        if claims.expires_at <= now or claims.issued_at > now + 60:
            raise AdminSessionError("administrator session expired")
        return claims

    @staticmethod
    def verify_csrf(claims: AdminSessionClaims, supplied: str | None) -> None:
        if not supplied or not hmac.compare_digest(claims.csrf_token, supplied):
            raise AdminSessionError("administrator CSRF validation failed")
