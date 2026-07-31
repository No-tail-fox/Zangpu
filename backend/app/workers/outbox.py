from __future__ import annotations

from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.integrations.bifrost.binding_service import binding_remote_fields_complete, persist_created_binding
from backend.app.integrations.bifrost.models import (
    BifrostOutboxPayload,
    BifrostUpstreamError,
    VirtualKeyCreationResult,
    VirtualKeySpec,
    VirtualKeyState,
)
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.outbox import ControlOutbox
from backend.app.security.keyring import CredentialKeyring


class BifrostBindingClient(Protocol):
    async def find_virtual_key_by_name(self, name: str) -> VirtualKeyCreationResult | None: ...

    async def create_virtual_key(self, spec: VirtualKeySpec) -> VirtualKeyCreationResult: ...

    async def get_virtual_key_material(self, virtual_key_id: str) -> VirtualKeyCreationResult: ...

    async def update_virtual_key(self, virtual_key_id: str, spec: VirtualKeySpec) -> VirtualKeyState: ...

    async def disable_virtual_key(self, virtual_key_id: str) -> VirtualKeyState: ...


class BifrostOutboxWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: BifrostBindingClient,
        keyring: CredentialKeyring,
        *,
        max_attempts: int = 8,
        base_retry_seconds: int = 5,
        max_retry_seconds: int = 300,
        claim_timeout_seconds: int = 120,
        batch_size: int = 25,
    ) -> None:
        if (
            not 1 <= max_attempts <= 100
            or not 1 <= batch_size <= 100
            or base_retry_seconds < 1
            or max_retry_seconds < base_retry_seconds
            or claim_timeout_seconds < 1
        ):
            raise ValueError("outbox worker bounds are invalid")
        self._session_factory = session_factory
        self._client = client
        self._keyring = keyring
        self._max_attempts = max_attempts
        self._base_retry_seconds = base_retry_seconds
        self._max_retry_seconds = max_retry_seconds
        self._claim_timeout_seconds = claim_timeout_seconds
        self._batch_size = batch_size

    async def run_once(self, *, now: int) -> int:
        item_ids = self._claim(now=now)
        for item_id in item_ids:
            await self._process(item_id, now=now)
        return len(item_ids)

    def _claim(self, *, now: int) -> list[str]:
        stale_before = now - self._claim_timeout_seconds
        with self._session_factory.begin() as session:
            items = session.scalars(
                select(ControlOutbox)
                .where(
                    ControlOutbox.target == "bifrost",
                    ControlOutbox.attempt_count < self._max_attempts,
                    or_(
                        (
                            ControlOutbox.status.in_(("pending", "failed"))
                            & (ControlOutbox.available_at <= now)
                        ),
                        (
                            (ControlOutbox.status == "processing")
                            & (ControlOutbox.locked_at.is_not(None))
                            & (ControlOutbox.locked_at <= stale_before)
                        ),
                    ),
                )
                .order_by(ControlOutbox.available_at, ControlOutbox.id)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            for item in items:
                item.status = "processing"
                item.attempt_count += 1
                item.locked_at = now
                item.updated_at = now
            return [item.id for item in items]

    async def _process(self, item_id: str, *, now: int) -> None:
        try:
            action, binding, payload = self._load(item_id)
            result = await self._execute(action=action, binding=binding, payload=payload)
            self._complete(item_id, result=result, now=now)
        except BifrostUpstreamError as exc:
            self._fail(item_id, code=exc.code, retryable=exc.retryable, now=now)
        except (ValueError, RuntimeError):
            self._fail(item_id, code="BIFROST_RECONCILIATION_FAILED", retryable=False, now=now)

    def _load(self, item_id: str) -> tuple[str, ApiClientBinding, BifrostOutboxPayload]:
        with self._session_factory() as session:
            item = session.get(ControlOutbox, item_id)
            if item is None or item.status != "processing" or item.target != "bifrost":
                raise ValueError("outbox item is not claimable")
            binding = session.get(ApiClientBinding, item.aggregate_id)
            if binding is None or item.aggregate_type != "api_client_binding":
                raise ValueError("outbox binding is unavailable")
            payload = BifrostOutboxPayload.model_validate(item.payload)
            session.expunge(binding)
            return item.action, binding, payload

    async def _execute(
        self,
        *,
        action: str,
        binding: ApiClientBinding,
        payload: BifrostOutboxPayload,
    ) -> VirtualKeyCreationResult | VirtualKeyState | None:
        if action == "disable":
            if binding.bifrost_virtual_key_id is None:
                return None
            try:
                result = await self._client.disable_virtual_key(binding.bifrost_virtual_key_id)
            except BifrostUpstreamError as exc:
                if exc.code == "BIFROST_NOT_FOUND":
                    return None
                raise
            if result.is_active:
                raise BifrostUpstreamError(
                    code="BIFROST_PROTOCOL_ERROR", status_code=502, retryable=True
                )
            return result
        if payload.desired is None:
            raise ValueError("Bifrost desired configuration is missing")
        if action == "create":
            if binding.bifrost_virtual_key_id:
                result = await self._client.get_virtual_key_material(binding.bifrost_virtual_key_id)
                self._verify_desired(result.state, payload.desired)
                return result
            existing = await self._client.find_virtual_key_by_name(payload.desired.name)
            result = existing or await self._client.create_virtual_key(payload.desired)
            self._verify_desired(result.state, payload.desired)
            return result
        if action == "update" and binding.bifrost_virtual_key_id:
            result = await self._client.update_virtual_key(binding.bifrost_virtual_key_id, payload.desired)
            self._verify_desired(result, payload.desired)
            return result
        raise ValueError("unsupported Bifrost outbox action")

    @staticmethod
    def _verify_desired(state: VirtualKeyState, desired: VirtualKeySpec) -> None:
        if (
            state.name != desired.name
            or state.provider != desired.provider
            or state.model != desired.model
            or not state.is_active
        ):
            raise BifrostUpstreamError(
                code="BIFROST_PROTOCOL_ERROR", status_code=502, retryable=True
            )

    def _complete(
        self,
        item_id: str,
        *,
        result: VirtualKeyCreationResult | VirtualKeyState | None,
        now: int,
    ) -> None:
        with self._session_factory.begin() as session:
            item = session.get(ControlOutbox, item_id)
            if item is None or item.status != "processing":
                return
            binding = session.get(ApiClientBinding, item.aggregate_id)
            if binding is None:
                raise ValueError("outbox binding is unavailable")
            payload = BifrostOutboxPayload.model_validate(item.payload)
            if isinstance(result, VirtualKeyCreationResult):
                persist_created_binding(binding, result, self._keyring, now=now)
            elif item.action == "disable":
                binding.sync_status = "disabled" if binding_remote_fields_complete(binding) else "pending"
                binding.last_sync_error_code = None
                binding.updated_at = now
            else:
                binding.sync_status = "active" if binding_remote_fields_complete(binding) else "pending"
                binding.last_sync_error_code = None
                binding.updated_at = now
            if binding.version != payload.binding_version:
                binding.sync_status = "pending"
            item.status = "completed"
            item.completed_at = now
            item.locked_at = None
            item.last_error_code = None
            item.updated_at = now

    def _fail(self, item_id: str, *, code: str, retryable: bool, now: int) -> None:
        with self._session_factory.begin() as session:
            item = session.get(ControlOutbox, item_id)
            if item is None or item.status != "processing":
                return
            item.status = "failed"
            item.locked_at = None
            item.last_error_code = code[:64]
            if retryable:
                delay = min(self._base_retry_seconds * (2 ** (item.attempt_count - 1)), self._max_retry_seconds)
                item.available_at = now + delay
            else:
                item.attempt_count = self._max_attempts
                item.available_at = now
            item.updated_at = now
            binding = session.get(ApiClientBinding, item.aggregate_id)
            if binding is not None:
                payload_version = item.payload.get("binding_version") if isinstance(item.payload, dict) else None
                if binding.version == payload_version:
                    binding.sync_status = "error"
                    binding.last_sync_error_code = item.last_error_code
                    binding.updated_at = now
