from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from time import time

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.app.api.errors import external_error_response
from backend.app.security.canonical import (
    LOWER_HEX_SHA256_RE,
    SIGNATURE_VERSION,
    CanonicalizationError,
    body_sha256_hex,
    create_canonical_request,
    validate_signing_fields,
    verify_signature,
)
from backend.app.security.keyring import (
    CredentialDecryptionError,
    CredentialKeyring,
    EncryptedSecret,
    KeyVersionUnavailable,
    credential_aad,
)

REQUIRED_AUTH_HEADERS = {
    b"x-zangpu-key": "key_id",
    b"x-zangpu-timestamp": "timestamp",
    b"x-zangpu-nonce": "nonce",
    b"x-zangpu-request-id": "request_id",
    b"x-zangpu-signature-version": "signature_version",
    b"x-zangpu-signature": "signature",
}
MAX_HEADER_COUNT = 64
MAX_HEADER_NAME_LENGTH = 64
MAX_HEADER_VALUE_LENGTH = 8_192
MAX_SIGNED_HEADER_LENGTH = 256


@dataclass(frozen=True, slots=True, repr=False)
class ExternalAuthHeaders:
    key_id: str
    timestamp: str
    nonce: str
    request_id: str
    signature_version: str
    signature: str

    def __repr__(self) -> str:
        return (
            f"ExternalAuthHeaders(key_id={self.key_id!r}, request_id={self.request_id!r}, "
            "nonce=<redacted>, signature=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedCallerCredential:
    credential_id: str
    api_client_id: str
    key_id: str
    secret_ciphertext: str
    secret_nonce: str
    master_key_id: str
    credential_status: str
    credential_expires_at: int | None
    client_status: str

    def __repr__(self) -> str:
        return (
            f"ResolvedCallerCredential(credential_id={self.credential_id!r}, "
            f"api_client_id={self.api_client_id!r}, key_id={self.key_id!r}, "
            "protected_secret=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedCaller:
    api_client_id: str
    credential_id: str
    key_id: str
    request_id: str
    nonce: str
    timestamp: int

    def __repr__(self) -> str:
        return (
            f"AuthenticatedCaller(api_client_id={self.api_client_id!r}, credential_id={self.credential_id!r}, "
            f"key_id={self.key_id!r}, request_id={self.request_id!r}, nonce=<redacted>)"
        )


class ExternalAuthFailure(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: str) -> None:
        super().__init__("external authentication failed")
        self.category = category

    def to_response(self, server_request_id: str) -> JSONResponse:
        return external_error_response("AUTH_FAILED", server_request_id=server_request_id)


class ExternalClientDisabled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("external client is disabled")

    def to_response(self, server_request_id: str) -> JSONResponse:
        return external_error_response("CLIENT_DISABLED", server_request_id=server_request_id)


CredentialResolver = Callable[[str], Awaitable[ResolvedCallerCredential | None]]


def parse_external_auth_headers(raw_headers: Sequence[tuple[bytes, bytes]]) -> ExternalAuthHeaders:
    if len(raw_headers) > MAX_HEADER_COUNT:
        raise ExternalAuthFailure("header_count")

    values: dict[str, str] = {}
    for raw_name, raw_value in raw_headers:
        if len(raw_name) > MAX_HEADER_NAME_LENGTH or len(raw_value) > MAX_HEADER_VALUE_LENGTH:
            raise ExternalAuthFailure("header_length")
        header_name = raw_name.lower()
        field_name = REQUIRED_AUTH_HEADERS.get(header_name)
        if field_name is None:
            continue
        if field_name in values:
            raise ExternalAuthFailure("duplicate_header")
        try:
            value = raw_value.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise ExternalAuthFailure("header_encoding") from exc
        if not value or value != value.strip() or len(value) > MAX_SIGNED_HEADER_LENGTH:
            raise ExternalAuthFailure("header_value")
        values[field_name] = value

    if set(values) != set(REQUIRED_AUTH_HEADERS.values()):
        raise ExternalAuthFailure("missing_header")
    try:
        validate_signing_fields(
            key_id=values["key_id"],
            timestamp=values["timestamp"],
            nonce=values["nonce"],
            request_id=values["request_id"],
        )
    except CanonicalizationError as exc:
        raise ExternalAuthFailure("header_format") from exc
    if values["signature_version"] != SIGNATURE_VERSION:
        raise ExternalAuthFailure("signature_version")
    if not LOWER_HEX_SHA256_RE.fullmatch(values["signature"]):
        raise ExternalAuthFailure("signature_format")
    return ExternalAuthHeaders(**values)


class ExternalAuthenticator:
    def __init__(
        self,
        *,
        keyring: CredentialKeyring,
        resolver: CredentialResolver,
        timestamp_tolerance_seconds: int = 300,
    ) -> None:
        if not 30 <= timestamp_tolerance_seconds <= 900:
            raise ValueError("timestamp tolerance must be between 30 and 900 seconds")
        self._keyring = keyring
        self._resolver = resolver
        self._timestamp_tolerance_seconds = timestamp_tolerance_seconds

    async def authenticate(
        self,
        *,
        method: str,
        raw_path: str,
        raw_query: str,
        body: bytes,
        raw_headers: Sequence[tuple[bytes, bytes]],
        now: int | None = None,
    ) -> AuthenticatedCaller:
        headers = parse_external_auth_headers(raw_headers)
        resolved = await self._resolver(headers.key_id)
        current_time = int(time()) if now is None else now
        if (
            resolved is None
            or resolved.key_id != headers.key_id
            or resolved.credential_status != "active"
            or (resolved.credential_expires_at is not None and current_time >= resolved.credential_expires_at)
        ):
            raise ExternalAuthFailure("credential_state")
        if abs(current_time - int(headers.timestamp)) > self._timestamp_tolerance_seconds:
            raise ExternalAuthFailure("timestamp")

        try:
            canonical = create_canonical_request(
                method=method,
                raw_path=raw_path,
                raw_query=raw_query,
                body_hash=body_sha256_hex(body),
                key_id=headers.key_id,
                timestamp=headers.timestamp,
                nonce=headers.nonce,
                request_id=headers.request_id,
            )
            encrypted = EncryptedSecret(
                ciphertext=resolved.secret_ciphertext,
                nonce=resolved.secret_nonce,
                key_id=resolved.master_key_id,
            )
            plaintext = self._keyring.decrypt(
                encrypted,
                aad=credential_aad(
                    resolved.credential_id,
                    resolved.api_client_id,
                    resolved.key_id,
                    resolved.master_key_id,
                ),
            )
            secret = plaintext.decode("utf-8", errors="strict")
            if not verify_signature(secret, canonical, headers.signature):
                raise ExternalAuthFailure("signature")
        except ExternalAuthFailure:
            raise
        except (
            CanonicalizationError,
            CredentialDecryptionError,
            KeyVersionUnavailable,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            raise ExternalAuthFailure("credential_protection") from exc
        if resolved.client_status != "active":
            raise ExternalClientDisabled

        return AuthenticatedCaller(
            api_client_id=resolved.api_client_id,
            credential_id=resolved.credential_id,
            key_id=resolved.key_id,
            request_id=headers.request_id,
            nonce=headers.nonce,
            timestamp=int(headers.timestamp),
        )


async def authenticate_http_request(
    request: Request,
    authenticator: ExternalAuthenticator,
    *,
    now: int | None = None,
) -> AuthenticatedCaller:
    try:
        raw_path_bytes = request.scope.get("raw_path")
        raw_path = request.url.path if raw_path_bytes is None else raw_path_bytes.decode("ascii", errors="strict")
        raw_query = request.scope.get("query_string", b"").decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExternalAuthFailure("request_target_encoding") from exc
    return await authenticator.authenticate(
        method=request.method,
        raw_path=raw_path,
        raw_query=raw_query,
        body=await request.body(),
        raw_headers=request.scope.get("headers", []),
        now=now,
    )
