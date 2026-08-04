from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.app.integrations.bifrost.binding_service import (
    binding_remote_fields_complete,
    decrypt_binding_value,
)
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential
from backend.app.security.dependencies import AuthenticatedCaller, ResolvedCallerCredential
from backend.app.security.keyring import (
    CredentialDecryptionError,
    CredentialKeyring,
    KeyVersionUnavailable,
)


class CallerPolicyError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("caller policy is unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class CallerPolicy:
    api_client_id: str
    credential_id: str
    allowed_endpoints: tuple[str, ...]
    allowed_models: tuple[str, ...]
    qps_limit: int
    concurrency_limit: int
    daily_request_limit: int | None
    daily_token_limit: int | None
    total_request_limit: int | None
    total_token_limit: int | None
    max_output_tokens_per_request: int
    service_user_id: str
    bifrost_virtual_key: SecretStr

    def __repr__(self) -> str:
        return (
            f"CallerPolicy(api_client_id={self.api_client_id!r}, credential_id={self.credential_id!r}, "
            f"allowed_endpoints={self.allowed_endpoints!r}, allowed_models={self.allowed_models!r}, "
            "bifrost_virtual_key=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class CallerMetadataPolicy:
    api_client_id: str
    credential_id: str
    allowed_endpoints: tuple[str, ...]
    allowed_models: tuple[str, ...]
    qps_limit: int
    daily_request_limit: int | None
    daily_token_limit: int | None
    total_request_limit: int | None
    total_token_limit: int | None


class DatabaseCredentialResolver:
    __slots__ = ("_sessions",)

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def _resolve(self, key_id: str) -> ResolvedCallerCredential | None:
        with self._sessions() as session:
            row = session.execute(
                select(ApiClientCredential, ApiClient)
                .join(ApiClient, ApiClient.id == ApiClientCredential.api_client_id)
                .where(ApiClientCredential.key_id == key_id)
            ).one_or_none()
            if row is None:
                return None
            credential, client = row
            return ResolvedCallerCredential(
                credential_id=credential.id,
                api_client_id=credential.api_client_id,
                key_id=credential.key_id,
                secret_ciphertext=credential.secret_ciphertext,
                secret_nonce=credential.secret_nonce,
                master_key_id=credential.master_key_id,
                credential_status=credential.status,
                credential_expires_at=credential.expires_at,
                client_status=client.status,
            )

    async def __call__(self, key_id: str) -> ResolvedCallerCredential | None:
        return await run_in_threadpool(self._resolve, key_id)


def load_caller_policy(
    session: Session,
    *,
    caller: AuthenticatedCaller,
    keyring: CredentialKeyring,
    now: int,
) -> CallerPolicy:
    row = session.execute(
        select(ApiClient, ApiClientCredential, ApiClientBinding)
        .join(ApiClientCredential, ApiClientCredential.api_client_id == ApiClient.id)
        .join(ApiClientBinding, ApiClientBinding.api_client_id == ApiClient.id)
        .where(
            ApiClient.id == caller.api_client_id,
            ApiClientCredential.id == caller.credential_id,
            ApiClientCredential.key_id == caller.key_id,
        )
    ).one_or_none()
    if row is None:
        raise CallerPolicyError("CALLER_STATE_UNAVAILABLE")
    client, credential, binding = row
    if client.status != "active":
        raise CallerPolicyError("CLIENT_DISABLED")
    if credential.status != "active":
        raise CallerPolicyError("CREDENTIAL_REVOKED")
    if credential.expires_at is not None and credential.expires_at <= now:
        raise CallerPolicyError("CREDENTIAL_EXPIRED")
    service_user_id = binding.zangpu_service_user_id
    if (
        binding.sync_status != "active"
        or not binding_remote_fields_complete(binding)
        or service_user_id is None
    ):
        raise CallerPolicyError("CALLER_BINDING_UNAVAILABLE")
    try:
        UUID(service_user_id)
    except ValueError as exc:
        raise CallerPolicyError("CALLER_BINDING_UNAVAILABLE") from exc
    try:
        virtual_key = decrypt_binding_value(binding, keyring)
    except (CredentialDecryptionError, KeyVersionUnavailable, ValueError) as exc:
        raise CallerPolicyError("CALLER_BINDING_UNAVAILABLE") from exc

    return CallerPolicy(
        api_client_id=client.id,
        credential_id=credential.id,
        allowed_endpoints=tuple(client.allowed_endpoints),
        allowed_models=tuple(client.allowed_models),
        qps_limit=client.qps_limit,
        concurrency_limit=client.concurrency_limit,
        daily_request_limit=client.daily_request_limit,
        daily_token_limit=client.daily_token_limit,
        total_request_limit=client.total_request_limit,
        total_token_limit=client.total_token_limit,
        max_output_tokens_per_request=client.max_output_tokens_per_request,
        service_user_id=service_user_id,
        bifrost_virtual_key=virtual_key,
    )


def load_caller_metadata_policy(
    session: Session,
    *,
    caller: AuthenticatedCaller,
    now: int,
) -> CallerMetadataPolicy:
    row = session.execute(
        select(ApiClient, ApiClientCredential)
        .join(ApiClientCredential, ApiClientCredential.api_client_id == ApiClient.id)
        .where(
            ApiClient.id == caller.api_client_id,
            ApiClientCredential.id == caller.credential_id,
            ApiClientCredential.key_id == caller.key_id,
        )
    ).one_or_none()
    if row is None:
        raise CallerPolicyError("CALLER_STATE_UNAVAILABLE")
    client, credential = row
    if client.status != "active":
        raise CallerPolicyError("CLIENT_DISABLED")
    if credential.status != "active":
        raise CallerPolicyError("CREDENTIAL_REVOKED")
    if credential.expires_at is not None and credential.expires_at <= now:
        raise CallerPolicyError("CREDENTIAL_EXPIRED")
    return CallerMetadataPolicy(
        api_client_id=client.id,
        credential_id=credential.id,
        allowed_endpoints=tuple(client.allowed_endpoints),
        allowed_models=tuple(client.allowed_models),
        qps_limit=client.qps_limit,
        daily_request_limit=client.daily_request_limit,
        daily_token_limit=client.daily_token_limit,
        total_request_limit=client.total_request_limit,
        total_token_limit=client.total_token_limit,
    )
