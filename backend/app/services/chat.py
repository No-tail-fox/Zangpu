from __future__ import annotations

from dataclasses import dataclass
from time import monotonic_ns, time
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.app.api.errors import ERROR_SPECS, ExternalApiError
from backend.app.api.external_models import (
    ChatCompletionRequest,
    ExternalPayloadError,
    estimate_prompt_tokens,
)
from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.bifrost.models import BifrostUpstreamError, ChatCompletionResponse
from backend.app.integrations.openwebui.client import OpenWebUIClient
from backend.app.integrations.openwebui.models import OpenWebUIUpstreamError
from backend.app.limits.concurrency import ConcurrencyDecision, ConcurrencyLimiter
from backend.app.limits.nonce import NonceGuard
from backend.app.limits.qps import QpsDecision, SlidingWindowQps
from backend.app.limits.redis import ControlPlaneUnavailable
from backend.app.models.base import new_uuid
from backend.app.security.dependencies import AuthenticatedCaller
from backend.app.security.keyring import CredentialKeyring
from backend.app.services.callers import CallerPolicy, CallerPolicyError, load_caller_policy
from backend.app.services.quota import (
    OperationTerminal,
    QuotaAdmissionError,
    finalize_operation,
    record_credit_reservation,
    record_provider_usage,
    reserve_operation,
)


@dataclass(frozen=True, slots=True)
class ExternalChatResult:
    response: ChatCompletionResponse
    rate_limit_headers: dict[str, str]


def rate_limit_headers(decision: QpsDecision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str((decision.reset_at_ms + 999) // 1_000),
    }


def _caller_policy_error(error: CallerPolicyError) -> str:
    if error.code == "CLIENT_DISABLED":
        return "CLIENT_DISABLED"
    if error.code in {"CREDENTIAL_REVOKED", "CREDENTIAL_EXPIRED"}:
        return "AUTH_FAILED"
    return "CONTROL_PLANE_UNAVAILABLE"


def _quota_error(error: QuotaAdmissionError) -> str:
    if error.code in {"CREDENTIAL_REVOKED", "CREDENTIAL_EXPIRED"}:
        return "AUTH_FAILED"
    if error.code in ERROR_SPECS:
        return error.code
    return "INTERNAL_ERROR"


def _credit_error(error: OpenWebUIUpstreamError) -> str:
    if error.code in {"CREDIT_BALANCE_EXHAUSTED", "CREDIT_ACCOUNT_FROZEN"}:
        return error.code
    if error.code == "CREDIT_PRICING_UNAVAILABLE":
        return "MODEL_UNAVAILABLE"
    return "CONTROL_PLANE_UNAVAILABLE"


def _provider_error(error: BifrostUpstreamError) -> str:
    if error.status_code == 404:
        return "MODEL_NOT_FOUND"
    if error.status_code == 504 or "TIMEOUT" in error.code:
        return "MODEL_TIMEOUT"
    return "MODEL_UNAVAILABLE"


class ExternalChatService:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        keyring: CredentialKeyring,
        nonce_guard: NonceGuard,
        qps_limiter: SlidingWindowQps,
        concurrency_limiter: ConcurrencyLimiter,
        bifrost: BifrostClient,
        openwebui: OpenWebUIClient,
        global_max_output_tokens: int,
    ) -> None:
        if not 1 <= global_max_output_tokens <= 1_000_000:
            raise ValueError("global output Token limit is invalid")
        self._sessions = sessions
        self._keyring = keyring
        self._nonce_guard = nonce_guard
        self._qps_limiter = qps_limiter
        self._concurrency_limiter = concurrency_limiter
        self._bifrost = bifrost
        self._openwebui = openwebui
        self._global_max_output_tokens = global_max_output_tokens

    def _load_policy(self, caller: AuthenticatedCaller, now: int) -> CallerPolicy:
        with self._sessions() as session:
            return load_caller_policy(
                session,
                caller=caller,
                keyring=self._keyring,
                now=now,
            )

    def _reserve_quota(
        self,
        *,
        caller: AuthenticatedCaller,
        operation_id: str,
        request_fingerprint: str,
        model_id: str,
        reserved_tokens: int,
        now: int,
    ) -> None:
        with self._sessions() as session, session.begin():
            reserve_operation(
                session,
                api_client_id=caller.api_client_id,
                credential_id=caller.credential_id,
                operation_id=operation_id,
                client_request_id=caller.request_id,
                request_fingerprint=request_fingerprint,
                model_id=model_id,
                reserved_tokens=reserved_tokens,
                now=now,
            )

    def _record_credit(
        self,
        *,
        operation_id: str,
        settlement_id: str,
        usage_operation_id: str,
        now: int,
    ) -> None:
        with self._sessions() as session, session.begin():
            record_credit_reservation(
                session,
                operation_id=operation_id,
                settlement_id=settlement_id,
                usage_operation_id=usage_operation_id,
                now=now,
            )

    def _record_usage(
        self,
        *,
        operation_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        now: int,
    ) -> None:
        with self._sessions() as session, session.begin():
            record_provider_usage(
                session,
                operation_id=operation_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                now=now,
            )

    def _terminalize(
        self,
        *,
        operation_id: str,
        operation_started_at: int,
        started_ns: int,
        server_request_id: str,
        qps: QpsDecision,
        concurrency: ConcurrencyDecision,
        status: Literal["completed", "rejected", "abandoned"],
        outcome: str,
        stage: str,
        code: str,
        charged_micro: int,
    ) -> None:
        spec = ERROR_SPECS[code] if code != "OK" else None
        completed_at = max(int(time()), operation_started_at)
        duration_ms = max((monotonic_ns() - started_ns) // 1_000_000, 0)
        with self._sessions() as session, session.begin():
            finalize_operation(
                session,
                operation_id=operation_id,
                terminal=OperationTerminal(
                    status=status,
                    outcome=outcome,
                    stage=stage,
                    http_status=200 if spec is None else spec.status_code,
                    business_code=code,
                    retryable=False if spec is None else spec.retryable,
                    duration_ms=duration_ms,
                    charged_micro=charged_micro,
                    qps_observed=qps.count,
                    concurrency_observed=concurrency.count,
                    server_request_id=server_request_id,
                    remote_ip_hash=None,
                    user_agent_family=None,
                    completed_at=completed_at,
                ),
            )

    async def execute(
        self,
        *,
        request: ChatCompletionRequest,
        caller: AuthenticatedCaller,
        server_request_id: str,
        request_fingerprint: str,
    ) -> ExternalChatResult:
        try:
            request.require_non_streaming()
        except ExternalPayloadError as exc:
            raise ExternalApiError(exc.code) from exc

        operation_id = new_uuid()
        operation_started_at = int(time())
        started_ns = monotonic_ns()
        claimed = await self._nonce_guard.claim(
            api_client_id=caller.api_client_id,
            credential_id=caller.credential_id,
            nonce=caller.nonce,
        )
        if not claimed:
            raise ExternalApiError("AUTH_FAILED")

        try:
            policy = await run_in_threadpool(self._load_policy, caller, operation_started_at)
        except CallerPolicyError as exc:
            raise ExternalApiError(_caller_policy_error(exc)) from exc
        if "chat.completions" not in policy.allowed_endpoints:
            raise ExternalApiError("ENDPOINT_FORBIDDEN")
        if request.model not in policy.allowed_models:
            raise ExternalApiError("MODEL_FORBIDDEN")

        qps = await self._qps_limiter.admit(
            api_client_id=caller.api_client_id,
            server_request_id=server_request_id,
            limit=policy.qps_limit,
        )
        headers = rate_limit_headers(qps)
        if not qps.allowed:
            raise ExternalApiError("QPS_LIMITED", headers=headers)

        concurrency = await self._concurrency_limiter.acquire(
            api_client_id=caller.api_client_id,
            operation_id=operation_id,
            limit=policy.concurrency_limit,
        )
        if not concurrency.allowed:
            raise ExternalApiError("CONCURRENCY_LIMITED", headers=headers)

        terminal_committed = False
        try:
            try:
                admitted_output_tokens = request.admitted_output_tokens(
                    client_limit=policy.max_output_tokens_per_request,
                    global_limit=self._global_max_output_tokens,
                )
            except ExternalPayloadError as exc:
                raise ExternalApiError(exc.code, headers=headers) from exc
            reserved_tokens = estimate_prompt_tokens(request) + admitted_output_tokens
            try:
                await run_in_threadpool(
                    self._reserve_quota,
                    caller=caller,
                    operation_id=operation_id,
                    request_fingerprint=request_fingerprint,
                    model_id=request.model,
                    reserved_tokens=reserved_tokens,
                    now=operation_started_at,
                )
            except QuotaAdmissionError as exc:
                raise ExternalApiError(
                    _quota_error(exc),
                    operation_id=exc.operation_id,
                    headers=headers,
                ) from exc

            try:
                credit = await self._openwebui.reserve_operation(
                    operation_id=operation_id,
                    service_user_id=policy.service_user_id,
                    model_id=request.model,
                    provider="bifrost",
                )
            except OpenWebUIUpstreamError as exc:
                public_code = _credit_error(exc)
                if public_code in {"CREDIT_BALANCE_EXHAUSTED", "CREDIT_ACCOUNT_FROZEN"}:
                    await run_in_threadpool(
                        self._terminalize,
                        operation_id=operation_id,
                        operation_started_at=operation_started_at,
                        started_ns=started_ns,
                        server_request_id=server_request_id,
                        qps=qps,
                        concurrency=concurrency,
                        status="rejected",
                        outcome="rejected",
                        stage="credit",
                        code=public_code,
                        charged_micro=0,
                    )
                    terminal_committed = True
                raise ExternalApiError(public_code, operation_id=operation_id, headers=headers) from exc

            usage_operation_id = credit.usage_operation_id
            expected_usage_operation_id = f"{operation_id}:usage"
            if (
                str(credit.operation_id) != operation_id
                or str(credit.service_user_id) != policy.service_user_id
                or credit.model_id != request.model
                or credit.provider != "bifrost"
                or credit.status != "pending"
                or usage_operation_id != expected_usage_operation_id
            ):
                raise ExternalApiError("CONTROL_PLANE_UNAVAILABLE", operation_id=operation_id, headers=headers)
            settlement_id = str(credit.settlement_id)
            await run_in_threadpool(
                self._record_credit,
                operation_id=operation_id,
                settlement_id=settlement_id,
                usage_operation_id=str(usage_operation_id),
                now=max(int(time()), operation_started_at),
            )

            try:
                response = await self._bifrost.forward_chat_completion(
                    request.bifrost_payload(),
                    virtual_key=policy.bifrost_virtual_key,
                )
            except BifrostUpstreamError as exc:
                public_code = _provider_error(exc)
                try:
                    cancelled = await self._openwebui.cancel_operation(
                        operation_id=operation_id,
                        service_user_id=policy.service_user_id,
                    )
                except OpenWebUIUpstreamError as credit_exc:
                    raise ExternalApiError(
                        "CONTROL_PLANE_UNAVAILABLE",
                        operation_id=operation_id,
                        headers=headers,
                    ) from credit_exc
                if (
                    str(cancelled.operation_id) != operation_id
                    or str(cancelled.service_user_id) != policy.service_user_id
                    or str(cancelled.settlement_id) != settlement_id
                    or cancelled.status not in {"cancelled_charged", "failed_no_charge"}
                    or cancelled.charged_micro != 0
                ):
                    raise ExternalApiError(
                        "CONTROL_PLANE_UNAVAILABLE",
                        operation_id=operation_id,
                        headers=headers,
                    ) from exc
                await run_in_threadpool(
                    self._terminalize,
                    operation_id=operation_id,
                    operation_started_at=operation_started_at,
                    started_ns=started_ns,
                    server_request_id=server_request_id,
                    qps=qps,
                    concurrency=concurrency,
                    status="rejected",
                    outcome="provider_error",
                    stage="provider",
                    code=public_code,
                    charged_micro=0,
                )
                terminal_committed = True
                raise ExternalApiError(public_code, operation_id=operation_id, headers=headers) from exc

            usage = response.usage
            await run_in_threadpool(
                self._record_usage,
                operation_id=operation_id,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                now=max(int(time()), operation_started_at),
            )
            try:
                settled = await self._openwebui.settle_operation(
                    operation_id=operation_id,
                    service_user_id=policy.service_user_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )
            except OpenWebUIUpstreamError as exc:
                raise ExternalApiError(
                    _credit_error(exc),
                    operation_id=operation_id,
                    headers=headers,
                ) from exc
            if (
                str(settled.operation_id) != operation_id
                or str(settled.service_user_id) != policy.service_user_id
                or str(settled.settlement_id) != settlement_id
                or settled.usage_operation_id != expected_usage_operation_id
                or settled.model_id != request.model
                or settled.provider != "bifrost"
                or settled.status != "succeeded_charged"
                or settled.prompt_tokens != usage.prompt_tokens
                or settled.completion_tokens != usage.completion_tokens
                or settled.total_tokens != usage.total_tokens
            ):
                raise ExternalApiError("CONTROL_PLANE_UNAVAILABLE", operation_id=operation_id, headers=headers)

            await run_in_threadpool(
                self._terminalize,
                operation_id=operation_id,
                operation_started_at=operation_started_at,
                started_ns=started_ns,
                server_request_id=server_request_id,
                qps=qps,
                concurrency=concurrency,
                status="completed",
                outcome="success",
                stage="response",
                code="OK",
                charged_micro=settled.charged_micro,
            )
            terminal_committed = True
            return ExternalChatResult(response=response, rate_limit_headers=headers)
        finally:
            try:
                await self._concurrency_limiter.release(
                    api_client_id=caller.api_client_id,
                    operation_id=operation_id,
                )
            except ControlPlaneUnavailable:
                if not terminal_committed:
                    raise
