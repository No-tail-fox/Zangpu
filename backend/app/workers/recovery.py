from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.integrations.openwebui.models import (
    CreditOperationSnapshot,
    CreditOperationStatus,
    OpenWebUIUpstreamError,
)
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.operations import ApiCallOperation
from backend.app.services.quota import OperationTerminal, finalize_operation, record_provider_usage


class CreditRecoveryClient(Protocol):
    async def get_operation_status(
        self,
        *,
        operation_id: str,
        service_user_id: str,
    ) -> CreditOperationStatus: ...

    async def settle_operation(
        self,
        *,
        operation_id: str,
        service_user_id: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> CreditOperationSnapshot: ...

    async def cancel_operation(
        self,
        *,
        operation_id: str,
        service_user_id: str,
    ) -> CreditOperationSnapshot: ...


@dataclass(frozen=True, slots=True)
class _RecoveryCandidate:
    operation_id: str
    service_user_id: str
    model_id: str
    settlement_id: str | None
    usage_operation_id: str | None
    provider_usage_recorded: bool
    prompt_tokens: int
    completion_tokens: int
    started_at: int


class ExternalChatRecoveryWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        openwebui: CreditRecoveryClient,
        *,
        stale_after_seconds: int = 300,
        batch_size: int = 25,
    ) -> None:
        if not 30 <= stale_after_seconds <= 86_400 or not 1 <= batch_size <= 100:
            raise ValueError("chat recovery worker bounds are invalid")
        self._session_factory = session_factory
        self._openwebui = openwebui
        self._stale_after_seconds = stale_after_seconds
        self._batch_size = batch_size

    async def run_once(self, *, now: int) -> int:
        if now < 0:
            raise ValueError("recovery time is invalid")
        candidates = self._load_candidates(now=now)
        for candidate in candidates:
            try:
                await self._process(candidate, now=now)
            except (OpenWebUIUpstreamError, ValueError, RuntimeError):
                continue
        return len(candidates)

    def _load_candidates(self, *, now: int) -> list[_RecoveryCandidate]:
        stale_before = now - self._stale_after_seconds
        with self._session_factory() as session:
            rows = session.execute(
                select(ApiCallOperation, ApiClientBinding.zangpu_service_user_id)
                .join(
                    ApiClientBinding,
                    ApiClientBinding.api_client_id == ApiCallOperation.api_client_id,
                )
                .where(
                    ApiCallOperation.status == "pending",
                    ApiCallOperation.updated_at <= stale_before,
                    ApiClientBinding.zangpu_service_user_id.is_not(None),
                )
                .order_by(ApiCallOperation.updated_at, ApiCallOperation.id)
                .limit(self._batch_size)
            ).all()
            return [
                _RecoveryCandidate(
                    operation_id=operation.id,
                    service_user_id=service_user_id,
                    model_id=operation.model_id,
                    settlement_id=operation.credit_settlement_id,
                    usage_operation_id=operation.usage_operation_id,
                    provider_usage_recorded=operation.provider_usage_recorded,
                    prompt_tokens=operation.prompt_tokens,
                    completion_tokens=operation.completion_tokens,
                    started_at=operation.started_at,
                )
                for operation, service_user_id in rows
                if service_user_id is not None
            ]

    async def _process(self, candidate: _RecoveryCandidate, *, now: int) -> None:
        try:
            remote = await self._openwebui.get_operation_status(
                operation_id=candidate.operation_id,
                service_user_id=candidate.service_user_id,
            )
        except OpenWebUIUpstreamError as exc:
            if (
                exc.code == "OPENWEBUI_CREDIT_NOT_FOUND"
                and candidate.settlement_id is None
                and not candidate.provider_usage_recorded
            ):
                self._finalize_without_credit(candidate, now=now)
                return
            raise
        self._validate_identity(candidate, remote)
        if remote.status == "settlement_error":
            return
        if remote.status == "pending":
            if candidate.provider_usage_recorded:
                remote = await self._openwebui.settle_operation(
                    operation_id=candidate.operation_id,
                    service_user_id=candidate.service_user_id,
                    prompt_tokens=candidate.prompt_tokens,
                    completion_tokens=candidate.completion_tokens,
                )
                self._validate_identity(candidate, remote)
                if remote.status != "succeeded_charged":
                    raise RuntimeError("recovery settlement is not terminal")
            else:
                remote = await self._openwebui.cancel_operation(
                    operation_id=candidate.operation_id,
                    service_user_id=candidate.service_user_id,
                )
                self._validate_identity(candidate, remote)
                if remote.status not in {"cancelled_charged", "failed_no_charge"}:
                    raise RuntimeError("recovery cancellation is not terminal")
        if remote.status not in {"succeeded_charged", "cancelled_charged", "failed_no_charge"}:
            return
        self._finalize(candidate, remote, now=now)

    @staticmethod
    def _validate_identity(
        candidate: _RecoveryCandidate,
        remote: CreditOperationSnapshot | CreditOperationStatus,
    ) -> None:
        expected_usage_operation_id = f"{candidate.operation_id}:usage"
        if (
            str(remote.operation_id) != candidate.operation_id
            or str(remote.service_user_id) != candidate.service_user_id
            or remote.model_id != candidate.model_id
            or remote.provider != "bifrost"
            or remote.usage_operation_id != expected_usage_operation_id
            or (
                candidate.settlement_id is not None
                and str(remote.settlement_id) != candidate.settlement_id
            )
            or (
                candidate.usage_operation_id is not None
                and remote.usage_operation_id != candidate.usage_operation_id
            )
        ):
            raise RuntimeError("recovery identity evidence conflicts")

    def _finalize(
        self,
        candidate: _RecoveryCandidate,
        remote: CreditOperationSnapshot | CreditOperationStatus,
        *,
        now: int,
    ) -> None:
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (
                    remote.prompt_tokens,
                    remote.completion_tokens,
                    remote.total_tokens,
                    remote.charged_micro,
                )
            )
            or remote.total_tokens != remote.prompt_tokens + remote.completion_tokens
        ):
            raise RuntimeError("recovery terminal evidence is invalid")
        succeeded = remote.status == "succeeded_charged"
        if remote.status == "failed_no_charge" and remote.charged_micro != 0:
            raise RuntimeError("no-charge recovery evidence is invalid")
        completed_at = max(now, candidate.started_at)
        duration_ms = min((completed_at - candidate.started_at) * 1_000, 2**63 - 1)
        with self._session_factory.begin() as session:
            if succeeded or remote.total_tokens > 0 or candidate.provider_usage_recorded:
                record_provider_usage(
                    session,
                    operation_id=candidate.operation_id,
                    prompt_tokens=remote.prompt_tokens,
                    completion_tokens=remote.completion_tokens,
                    now=completed_at,
                )
            finalize_operation(
                session,
                operation_id=candidate.operation_id,
                terminal=OperationTerminal(
                    status="completed" if succeeded else "abandoned",
                    outcome="success" if succeeded else "abandoned",
                    stage="recovery",
                    http_status=200 if succeeded else 499,
                    business_code="OK" if succeeded else "RECOVERED_CANCELLED",
                    retryable=False,
                    duration_ms=duration_ms,
                    charged_micro=remote.charged_micro,
                    qps_observed=0,
                    concurrency_observed=0,
                    server_request_id=f"recovery_{candidate.operation_id}",
                    remote_ip_hash=None,
                    user_agent_family=None,
                    completed_at=completed_at,
                ),
            )

    def _finalize_without_credit(self, candidate: _RecoveryCandidate, *, now: int) -> None:
        completed_at = max(now, candidate.started_at)
        duration_ms = min((completed_at - candidate.started_at) * 1_000, 2**63 - 1)
        with self._session_factory.begin() as session:
            finalize_operation(
                session,
                operation_id=candidate.operation_id,
                terminal=OperationTerminal(
                    status="abandoned",
                    outcome="abandoned",
                    stage="recovery",
                    http_status=499,
                    business_code="RECOVERED_NO_CREDIT",
                    retryable=False,
                    duration_ms=duration_ms,
                    charged_micro=0,
                    qps_observed=0,
                    concurrency_observed=0,
                    server_request_id=f"recovery_{candidate.operation_id}",
                    remote_ip_hash=None,
                    user_agent_family=None,
                    completed_at=completed_at,
                ),
            )
