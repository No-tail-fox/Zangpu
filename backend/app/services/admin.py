from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.integrations.bifrost.binding_service import (
    desired_config_hash,
    stage_bifrost_binding_sync,
    stage_local_client_disable,
)
from backend.app.integrations.bifrost.models import VirtualKeySpec
from backend.app.models.audits import ApiClientAdminAudit
from backend.app.models.base import new_uuid, utc_epoch_seconds, validate_string_set
from backend.app.models.bindings import ApiClientBinding, BindingSummary
from backend.app.models.clients import ENDPOINT_PERMISSIONS, ApiClient, ClientSummary
from backend.app.models.credentials import ApiClientCredential, CredentialSummary
from backend.app.security.credentials import OneTimeSecretAlreadyRead, create_protected_credential
from backend.app.security.keyring import CredentialKeyring


class AdminCallerNotFound(LookupError):
    pass


class AdminCallerStateError(RuntimeError):
    pass


class AdminCallerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    service_user_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    allowed_endpoints: list[str] = Field(default_factory=lambda: ["chat.completions"])
    allowed_models: list[str] = Field(min_length=1, max_length=256)
    group_ids: list[str] = Field(default_factory=list, max_length=256)
    qps_limit: int = Field(default=10, ge=1, le=100_000)
    concurrency_limit: int = Field(default=2, ge=1, le=10_000)
    daily_request_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    daily_token_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    total_request_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    total_token_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    max_output_tokens_per_request: int = Field(default=4096, ge=1, le=1_000_000)

    @field_validator("allowed_endpoints")
    @classmethod
    def validate_allowed_endpoints(cls, values: object) -> list[str]:
        return validate_string_set(
            "allowed_endpoints", values, allowed=ENDPOINT_PERMISSIONS, max_items=len(ENDPOINT_PERMISSIONS)
        )

    @field_validator("allowed_models", "group_ids")
    @classmethod
    def validate_string_lists(cls, values: object, info: Any) -> list[str]:
        return validate_string_set(info.field_name, values)

    @model_validator(mode="after")
    def validate_binding_model(self) -> AdminCallerCreateRequest:
        if self.model not in self.allowed_models:
            raise ValueError("binding model must be included in allowed_models")
        return self


class AdminCallerPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    allowed_endpoints: list[str] | None = Field(default=None, max_length=len(ENDPOINT_PERMISSIONS))
    allowed_models: list[str] | None = Field(default=None, min_length=1, max_length=256)
    group_ids: list[str] | None = Field(default=None, max_length=256)
    qps_limit: int | None = Field(default=None, ge=1, le=100_000)
    concurrency_limit: int | None = Field(default=None, ge=1, le=10_000)
    daily_request_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    daily_token_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    total_request_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    total_token_limit: int | None = Field(default=None, ge=1, le=2**63 - 1)
    max_output_tokens_per_request: int | None = Field(default=None, ge=1, le=1_000_000)

    @field_validator("allowed_endpoints")
    @classmethod
    def validate_allowed_endpoints(cls, values: object) -> list[str] | None:
        if values is None:
            return None
        return validate_string_set(
            "allowed_endpoints", values, allowed=ENDPOINT_PERMISSIONS, max_items=len(ENDPOINT_PERMISSIONS)
        )

    @field_validator("allowed_models", "group_ids")
    @classmethod
    def validate_string_lists(cls, values: object, info: Any) -> list[str] | None:
        if values is None:
            return None
        return validate_string_set(info.field_name, values)

    @model_validator(mode="after")
    def validate_nullable_fields(self) -> AdminCallerPatchRequest:
        nullable = {
            "description",
            "daily_request_limit",
            "daily_token_limit",
            "total_request_limit",
            "total_token_limit",
        }
        for field_name in self.model_fields_set - {"expected_version"}:
            if getattr(self, field_name) is None and field_name not in nullable:
                raise ValueError(f"{field_name} cannot be cleared")
        return self


class AdminCallerDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: ClientSummary
    credentials: list[CredentialSummary]
    binding: BindingSummary | None


@dataclass(slots=True, repr=False)
class AdminCallerCreateResult:
    client: ClientSummary
    credential: CredentialSummary
    binding: BindingSummary
    outbox_id: str
    _secret: str | None

    def __repr__(self) -> str:
        return (
            f"AdminCallerCreateResult(client_id={self.client.id!r}, "
            f"credential_id={self.credential.id!r}, secret=<redacted>)"
        )

    def take_secret(self) -> str:
        if self._secret is None:
            raise OneTimeSecretAlreadyRead("credential Secret has already been read")
        secret = self._secret
        self._secret = None
        return secret


@dataclass(slots=True, repr=False)
class AdminCredentialIssueResult:
    credential: CredentialSummary
    _secret: str | None

    def __repr__(self) -> str:
        return f"AdminCredentialIssueResult(credential_id={self.credential.id!r}, secret=<redacted>)"

    def take_secret(self) -> str:
        if self._secret is None:
            raise OneTimeSecretAlreadyRead("credential Secret has already been read")
        secret = self._secret
        self._secret = None
        return secret


class AdminCallerService:
    def __init__(self, sessions: sessionmaker[Session], keyring: CredentialKeyring) -> None:
        self._sessions = sessions
        self._keyring = keyring

    def create_caller(
        self,
        request: AdminCallerCreateRequest,
        *,
        actor_id: str,
        idempotency_key: str,
        now: int | None = None,
    ) -> AdminCallerCreateResult:
        changed_at = utc_epoch_seconds() if now is None else now
        with self._sessions.begin() as session:
            client = ApiClient(
                id=new_uuid(),
                name=request.name,
                description=request.description,
                status="active",
                allowed_endpoints=request.allowed_endpoints,
                allowed_models=request.allowed_models,
                group_ids=request.group_ids,
                qps_limit=request.qps_limit,
                concurrency_limit=request.concurrency_limit,
                daily_request_limit=request.daily_request_limit,
                daily_token_limit=request.daily_token_limit,
                total_request_limit=request.total_request_limit,
                total_token_limit=request.total_token_limit,
                max_output_tokens_per_request=request.max_output_tokens_per_request,
                version=1,
                created_by=actor_id,
                updated_by=actor_id,
                created_at=changed_at,
                updated_at=changed_at,
            )
            session.add(client)
            session.flush()

            credential_result = create_protected_credential(
                self._keyring,
                api_client_id=client.id,
                created_by=actor_id,
                now=changed_at,
            )
            session.add(credential_result.credential)
            spec = VirtualKeySpec(
                name=f"zangpu-{client.id}",
                description=request.description or "Managed by Zangpu control plane",
                provider=request.provider,
                model=request.model,
            )
            binding = ApiClientBinding(
                id=new_uuid(),
                api_client_id=client.id,
                zangpu_service_user_id=request.service_user_id,
                bifrost_config_hash=desired_config_hash(spec),
                sync_status="pending",
                version=1,
                created_at=changed_at,
                updated_at=changed_at,
            )
            session.add(binding)
            outbox = stage_bifrost_binding_sync(
                session,
                binding=binding,
                spec=spec,
                action="create",
                idempotency_key=self._idempotency_key("create", actor_id, idempotency_key),
                now=changed_at,
            )
            self._write_audit(
                session,
                actor_id=actor_id,
                client_id=client.id,
                action="caller.created",
                changed_fields=[
                    "name",
                    "description",
                    "allowed_endpoints",
                    "allowed_models",
                    "group_ids",
                    "qps_limit",
                    "concurrency_limit",
                    "daily_request_limit",
                    "daily_token_limit",
                    "total_request_limit",
                    "total_token_limit",
                    "max_output_tokens_per_request",
                    "status",
                ],
                before={},
                after={
                    "name": client.name,
                    "description": client.description,
                    "allowed_endpoints": sorted(client.allowed_endpoints),
                    "allowed_models": sorted(client.allowed_models),
                    "group_ids": sorted(client.group_ids),
                    "qps_limit": client.qps_limit,
                    "concurrency_limit": client.concurrency_limit,
                    "daily_request_limit": client.daily_request_limit,
                    "daily_token_limit": client.daily_token_limit,
                    "total_request_limit": client.total_request_limit,
                    "total_token_limit": client.total_token_limit,
                    "max_output_tokens_per_request": client.max_output_tokens_per_request,
                    "status": client.status,
                },
                now=changed_at,
            )
            session.flush()
            secret = credential_result.take_secret()
            return AdminCallerCreateResult(
                client=ClientSummary.model_validate(client),
                credential=CredentialSummary.model_validate(credential_result.credential),
                binding=BindingSummary.model_validate(binding),
                outbox_id=outbox.id,
                _secret=secret,
            )

    def get_caller(self, client_id: str) -> AdminCallerDetail:
        with self._sessions() as session:
            client = session.get(ApiClient, client_id)
            if client is None:
                raise AdminCallerNotFound("caller was not found")
            return self._detail(client)

    def list_callers(self, *, offset: int = 0, limit: int = 50) -> list[AdminCallerDetail]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("pagination bounds are invalid")
        with self._sessions() as session:
            statement = select(ApiClient).order_by(ApiClient.name, ApiClient.id).offset(offset).limit(limit)
            clients = session.scalars(statement).all()
            return [self._detail(client) for client in clients]

    def disable_caller(
        self,
        client_id: str,
        *,
        actor_id: str,
        idempotency_key: str,
        now: int | None = None,
    ) -> AdminCallerDetail:
        changed_at = utc_epoch_seconds() if now is None else now
        with self._sessions.begin() as session:
            client = session.get(ApiClient, client_id)
            if client is None:
                raise AdminCallerNotFound("caller was not found")
            if client.status == "disabled":
                return self._detail(client)
            if client.status != "active":
                raise AdminCallerStateError("only active callers can be disabled")
            binding = client.binding
            if binding is None:
                raise AdminCallerStateError("caller binding is not configured")
            stage_local_client_disable(
                session,
                client=client,
                binding=binding,
                actor_id=actor_id,
                idempotency_key=self._idempotency_key("disable", actor_id, idempotency_key),
                now=changed_at,
            )
            self._write_audit(
                session,
                actor_id=actor_id,
                client_id=client.id,
                action="caller.disabled",
                changed_fields=["status", "credentials"],
                before={"status": "active", "credentials": "active"},
                after={"status": client.status, "credentials": "revoked"},
                now=changed_at,
            )
            session.flush()
            return self._detail(client)

    def rotate_credential(
        self,
        client_id: str,
        *,
        actor_id: str,
        now: int | None = None,
    ) -> AdminCredentialIssueResult:
        changed_at = utc_epoch_seconds() if now is None else now
        with self._sessions.begin() as session:
            client = session.get(ApiClient, client_id)
            if client is None:
                raise AdminCallerNotFound("caller was not found")
            if client.status != "active":
                raise AdminCallerStateError("disabled caller cannot rotate credentials")
            credential_result = create_protected_credential(
                self._keyring,
                api_client_id=client.id,
                created_by=actor_id,
                now=changed_at,
            )
            session.add(credential_result.credential)
            for credential in client.credentials:
                if credential.status == "active":
                    credential.status = "revoked"
                    credential.revoked_by = actor_id
                    credential.revoked_at = changed_at
                    credential.replaced_by_id = credential_result.credential.id
            client.updated_by = actor_id
            client.updated_at = changed_at
            client.version += 1
            self._write_audit(
                session,
                actor_id=actor_id,
                client_id=client.id,
                action="credential.rotated",
                changed_fields=["credentials"],
                before={"credentials": "active"},
                after={"credentials": "rotated"},
                target_id=credential_result.credential.id,
                now=changed_at,
            )
            session.flush()
            secret = credential_result.take_secret()
            return AdminCredentialIssueResult(
                credential=CredentialSummary.model_validate(credential_result.credential),
                _secret=secret,
            )

    def revoke_credential(
        self,
        client_id: str,
        credential_id: str,
        *,
        actor_id: str,
        now: int | None = None,
    ) -> CredentialSummary:
        changed_at = utc_epoch_seconds() if now is None else now
        with self._sessions.begin() as session:
            client = session.get(ApiClient, client_id)
            if client is None:
                raise AdminCallerNotFound("caller was not found")
            credential = session.get(ApiClientCredential, credential_id)
            if credential is None or credential.api_client_id != client.id:
                raise AdminCallerNotFound("credential was not found")
            if credential.status == "active":
                credential.status = "revoked"
                credential.revoked_by = actor_id
                credential.revoked_at = changed_at
                client.updated_by = actor_id
                client.updated_at = changed_at
                client.version += 1
                self._write_audit(
                    session,
                    actor_id=actor_id,
                    client_id=client.id,
                    action="credential.revoked",
                    changed_fields=["credential_status"],
                    before={"credential_status": "active"},
                    after={"credential_status": "revoked"},
                    target_id=credential.id,
                    now=changed_at,
                )
            session.flush()
            return CredentialSummary.model_validate(credential)

    def update_caller(
        self,
        client_id: str,
        *,
        expected_version: int,
        patch: Mapping[str, object],
        actor_id: str,
        now: int | None = None,
    ) -> AdminCallerDetail:
        changed_at = utc_epoch_seconds() if now is None else now
        validated = AdminCallerPatchRequest.model_validate({"expected_version": expected_version, **dict(patch)})
        with self._sessions.begin() as session:
            client = session.get(ApiClient, client_id)
            if client is None:
                raise AdminCallerNotFound("caller was not found")
            if client.version != validated.expected_version:
                raise AdminCallerStateError("caller version is stale")
            changed_fields: list[str] = []
            before: dict[str, Any] = {}
            after: dict[str, Any] = {}
            for field_name in (
                "name",
                "description",
                "allowed_endpoints",
                "allowed_models",
                "group_ids",
                "qps_limit",
                "concurrency_limit",
                "daily_request_limit",
                "daily_token_limit",
                "total_request_limit",
                "total_token_limit",
                "max_output_tokens_per_request",
            ):
                if field_name not in validated.model_fields_set:
                    continue
                value = getattr(validated, field_name)
                before[field_name] = getattr(client, field_name)
                setattr(client, field_name, value)
                after[field_name] = value
                changed_fields.append(field_name)
            if not changed_fields:
                raise AdminCallerStateError("caller update is empty")
            client.updated_by = actor_id
            client.updated_at = changed_at
            client.version += 1
            self._write_audit(
                session,
                actor_id=actor_id,
                client_id=client.id,
                action="caller.updated",
                changed_fields=changed_fields,
                before=before,
                after=after,
                now=changed_at,
            )
            session.flush()
            return self._detail(client)

    @staticmethod
    def _idempotency_key(action: str, actor_id: str, supplied: str) -> str:
        if not supplied or len(supplied) > 128:
            raise ValueError("administrator idempotency key is invalid")
        digest = sha256(f"{action}\n{actor_id}\n{supplied}".encode()).hexdigest()
        return f"admin:{digest}"

    @staticmethod
    def _detail(client: ApiClient) -> AdminCallerDetail:
        return AdminCallerDetail(
            client=ClientSummary.model_validate(client),
            credentials=[CredentialSummary.model_validate(item) for item in client.credentials],
            binding=BindingSummary.model_validate(client.binding) if client.binding is not None else None,
        )

    @staticmethod
    def _write_audit(
        session: Session,
        *,
        actor_id: str,
        client_id: str,
        action: str,
        changed_fields: list[str],
        before: dict[str, Any],
        after: dict[str, Any],
        target_id: str | None = None,
        now: int,
    ) -> None:
        session.add(
            ApiClientAdminAudit(
                id=new_uuid(),
                actor_user_id=actor_id,
                api_client_id=client_id,
                target_type="client" if action.startswith("caller.") else "credential",
                target_id=target_id or client_id,
                action=action,
                changed_fields=changed_fields,
                before_summary=before,
                after_summary=after,
                created_at=now,
            )
        )
