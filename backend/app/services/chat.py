from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic, monotonic_ns, time
from typing import Literal, TypeVar

from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.app.api.errors import ERROR_SPECS, ExternalApiError
from backend.app.api.external_models import (
    ChatCompletionRequest,
    ExternalPayloadError,
    estimate_prompt_tokens,
)
from backend.app.integrations.bifrost.client import BifrostClient
from backend.app.integrations.bifrost.models import BifrostUpstreamError, ChatCompletionResponse, ChatUsage
from backend.app.integrations.openwebui.client import OpenWebUIClient
from backend.app.integrations.openwebui.models import OpenWebUIUpstreamError
from backend.app.limits.concurrency import ConcurrencyDecision, ConcurrencyLimiter
from backend.app.limits.model_pool import ModelPoolDecision, ModelPoolLimiter, ModelPoolPolicy
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
    touch_pending_operation,
)
from backend.app.services.streaming import OpenAIStreamDecoder

SSE_HEARTBEAT = b": heartbeat\n\n"
SSE_DONE = b"data: [DONE]\n\n"
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class ExternalChatResult:
    response: ChatCompletionResponse
    rate_limit_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ExternalChatStreamResult:
    stream: AsyncIterator[bytes]
    rate_limit_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ChatAdmission:
    request: ChatCompletionRequest
    caller: AuthenticatedCaller
    policy: CallerPolicy
    operation_id: str
    operation_started_at: int
    started_ns: int
    server_request_id: str
    qps: QpsDecision
    concurrency: ConcurrencyDecision
    lease: _ConcurrencyLeaseGuard
    model_pool_id: str
    headers: dict[str, str]
    settlement_id: str
    usage_operation_id: str


def rate_limit_headers(decision: QpsDecision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str((decision.reset_at_ms + 999) // 1_000),
    }


def concurrency_limit_headers(decision: ConcurrencyDecision) -> dict[str, str]:
    headers = {
        "X-Concurrency-Limit": str(decision.limit),
        "X-Concurrency-Remaining": str(decision.remaining),
        "X-Concurrency-Reset": str((decision.lease_expires_at_ms + 999) // 1_000),
    }
    if not decision.allowed:
        headers["Retry-After"] = str(
            max((decision.lease_expires_at_ms - decision.observed_at_ms + 999) // 1_000, 1)
        )
    return headers


def model_pool_limit_headers(decision: ModelPoolDecision) -> dict[str, str]:
    headers = {
        "X-Model-Pool-Limit": str(decision.active_limit),
        "X-Model-Pool-Remaining": str(decision.active_remaining),
        "X-Queue-Limit": str(decision.queue_limit),
        "X-Queue-Remaining": str(decision.queue_remaining),
        "X-Model-Pool-Reset": str((decision.expires_at_ms + 999) // 1_000),
    }
    if decision.status != "admitted":
        headers["Retry-After"] = str(
            max((decision.expires_at_ms - decision.observed_at_ms + 999) // 1_000, 1)
        )
    return headers


@dataclass(slots=True)
class _ConcurrencyLeaseGuard:
    limiter: ConcurrencyLimiter
    model_pool_limiter: ModelPoolLimiter
    model_pool_id: str
    api_client_id: str
    operation_id: str
    heartbeat_interval_seconds: float
    monotonic_seconds: Callable[[], float]
    _next_heartbeat_at: float = field(init=False)

    def __post_init__(self) -> None:
        self._next_heartbeat_at = self.monotonic_seconds() + self.heartbeat_interval_seconds

    def seconds_until_heartbeat(self, *, observed_at: float | None = None) -> float:
        now = self.monotonic_seconds() if observed_at is None else observed_at
        return max(self._next_heartbeat_at - now, 0.0)

    def heartbeat_due(self, *, observed_at: float | None = None) -> bool:
        now = self.monotonic_seconds() if observed_at is None else observed_at
        return now >= self._next_heartbeat_at

    async def heartbeat(self) -> None:
        renewed = await self.limiter.heartbeat(
            api_client_id=self.api_client_id,
            operation_id=self.operation_id,
        )
        model_renewed = await self.model_pool_limiter.heartbeat(
            pool_id=self.model_pool_id,
            operation_id=self.operation_id,
        )
        if not renewed or not model_renewed:
            raise ControlPlaneUnavailable
        self._next_heartbeat_at = self.monotonic_seconds() + self.heartbeat_interval_seconds

    async def run(self, awaitable: Awaitable[_ResultT]) -> _ResultT:
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                completed, _ = await asyncio.wait(
                    {task},
                    timeout=self.seconds_until_heartbeat(),
                )
                if completed:
                    return task.result()
                await self.heartbeat()
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise


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


def _stream_error_frame(
    code: str,
    *,
    server_request_id: str,
    operation_id: str,
) -> bytes:
    spec = ERROR_SPECS[code]
    payload = {
        "error": {
            "code": code,
            "message": spec.message,
            "request_id": server_request_id,
            "retryable": spec.retryable,
            "operation_id": operation_id,
        }
    }
    return b"data: " + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii") + b"\n\n"


async def _close_stream_iterator(iterator: AsyncIterator[bytes]) -> bool:
    try:
        await iterator.aclose()  # type: ignore[attr-defined]
    except AttributeError:
        return True
    except Exception:
        return False
    return True


class ExternalChatService:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        keyring: CredentialKeyring,
        nonce_guard: NonceGuard,
        qps_limiter: SlidingWindowQps,
        concurrency_limiter: ConcurrencyLimiter,
        model_pool_limiter: ModelPoolLimiter,
        model_pool_policies: Mapping[str, ModelPoolPolicy],
        bifrost: BifrostClient,
        openwebui: OpenWebUIClient,
        global_max_output_tokens: int,
        heartbeat_interval_seconds: float = 15.0,
        monotonic_seconds: Callable[[], float] = monotonic,
        global_queue_limit: int = 200,
        caller_queue_limit: int = 8,
        queue_wait_seconds: int = 30,
        queue_poll_milliseconds: int = 250,
        queue_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 1 <= global_max_output_tokens <= 1_000_000:
            raise ValueError("global output Token limit is invalid")
        if (
            isinstance(heartbeat_interval_seconds, bool)
            or not 0 < heartbeat_interval_seconds <= 300
        ):
            raise ValueError("heartbeat interval is invalid")
        if not callable(monotonic_seconds):
            raise ValueError("monotonic clock is invalid")
        if not 0 <= global_queue_limit <= 10_000:
            raise ValueError("global queue limit is invalid")
        if not 0 <= caller_queue_limit <= 1_000:
            raise ValueError("caller queue limit is invalid")
        if global_queue_limit > 0 and caller_queue_limit == 0:
            raise ValueError("caller queue limit must be positive when queueing is enabled")
        if not 1 <= queue_wait_seconds <= 300:
            raise ValueError("queue wait is invalid")
        if not 50 <= queue_poll_milliseconds <= 2_000:
            raise ValueError("queue poll interval is invalid")
        if not callable(queue_sleep):
            raise ValueError("queue sleep is invalid")
        self._sessions = sessions
        self._keyring = keyring
        self._nonce_guard = nonce_guard
        self._qps_limiter = qps_limiter
        self._concurrency_limiter = concurrency_limiter
        self._model_pool_limiter = model_pool_limiter
        self._model_pool_policies = dict(model_pool_policies)
        self._bifrost = bifrost
        self._openwebui = openwebui
        self._global_max_output_tokens = global_max_output_tokens
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._monotonic_seconds = monotonic_seconds
        self._global_queue_limit = global_queue_limit
        self._caller_queue_limit = caller_queue_limit
        self._queue_wait_seconds = queue_wait_seconds
        self._queue_poll_seconds = queue_poll_milliseconds / 1_000
        self._queue_sleep = queue_sleep

    def _load_policy(self, caller: AuthenticatedCaller, now: int) -> CallerPolicy:
        with self._sessions() as session:
            return load_caller_policy(
                session,
                caller=caller,
                keyring=self._keyring,
                now=now,
            )

    async def _acquire_model_pool(
        self,
        *,
        model_id: str,
        api_client_id: str,
        operation_id: str,
        headers: dict[str, str],
    ) -> tuple[ModelPoolPolicy, ModelPoolDecision]:
        policy = self._model_pool_policies.get(model_id)
        if policy is None:
            raise ExternalApiError("MODEL_UNAVAILABLE", headers=headers)
        decision = await self._model_pool_limiter.admit_or_enqueue(
            pool_id=policy.pool_id,
            api_client_id=api_client_id,
            operation_id=operation_id,
            active_limit=policy.active_limit,
            queue_limit=self._global_queue_limit,
            caller_queue_limit=self._caller_queue_limit,
            queue_wait_seconds=self._queue_wait_seconds,
        )
        headers.update(model_pool_limit_headers(decision))
        if decision.status == "admitted":
            return policy, decision
        if decision.status != "queued":
            raise ExternalApiError("MODEL_CAPACITY_LIMITED", headers=headers)

        queue_deadline = self._monotonic_seconds() + max(
            (decision.expires_at_ms - decision.observed_at_ms) / 1_000,
            0.0,
        )
        try:
            while True:
                remaining = queue_deadline - self._monotonic_seconds()
                if remaining <= 0:
                    headers["Retry-After"] = "1"
                    raise ExternalApiError("MODEL_CAPACITY_LIMITED", headers=headers)
                await self._queue_sleep(min(self._queue_poll_seconds, remaining))
                if self._monotonic_seconds() >= queue_deadline:
                    headers["Retry-After"] = "1"
                    raise ExternalApiError("MODEL_CAPACITY_LIMITED", headers=headers)
                decision = await self._model_pool_limiter.admit_or_enqueue(
                    pool_id=policy.pool_id,
                    api_client_id=api_client_id,
                    operation_id=operation_id,
                    active_limit=policy.active_limit,
                    queue_limit=self._global_queue_limit,
                    caller_queue_limit=self._caller_queue_limit,
                    queue_wait_seconds=self._queue_wait_seconds,
                )
                headers.update(model_pool_limit_headers(decision))
                if decision.status == "admitted":
                    return policy, decision
                if decision.status != "queued":
                    raise ExternalApiError("MODEL_CAPACITY_LIMITED", headers=headers)
        except (asyncio.CancelledError, GeneratorExit):
            with suppress(ControlPlaneUnavailable):
                await self._model_pool_limiter.cancel(
                    pool_id=policy.pool_id,
                    api_client_id=api_client_id,
                    operation_id=operation_id,
                )
            raise
        except BaseException:
            if decision.status == "queued":
                await self._model_pool_limiter.cancel(
                    pool_id=policy.pool_id,
                    api_client_id=api_client_id,
                    operation_id=operation_id,
                )
            raise

    async def _release_active_leases(
        self,
        *,
        api_client_id: str,
        pool_id: str,
        operation_id: str,
    ) -> bool:
        released_cleanly = True
        try:
            await self._concurrency_limiter.release(
                api_client_id=api_client_id,
                operation_id=operation_id,
            )
        except ControlPlaneUnavailable:
            released_cleanly = False
        try:
            await self._model_pool_limiter.release(
                pool_id=pool_id,
                operation_id=operation_id,
            )
        except ControlPlaneUnavailable:
            released_cleanly = False
        return released_cleanly

    def _reserve_quota(
        self,
        *,
        caller: AuthenticatedCaller,
        operation_id: str,
        request_fingerprint: str,
        model_id: str,
        stream: bool,
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
                stream=stream,
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

    def _touch_operation(self, *, operation_id: str, now: int) -> bool:
        with self._sessions() as session, session.begin():
            return touch_pending_operation(
                session,
                operation_id=operation_id,
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

        model_pool_policy, _model_pool = await self._acquire_model_pool(
            model_id=request.model,
            api_client_id=caller.api_client_id,
            operation_id=operation_id,
            headers=headers,
        )
        try:
            concurrency = await self._concurrency_limiter.acquire(
                api_client_id=caller.api_client_id,
                operation_id=operation_id,
                limit=policy.concurrency_limit,
            )
        except BaseException:
            await self._model_pool_limiter.release(
                pool_id=model_pool_policy.pool_id,
                operation_id=operation_id,
            )
            raise
        headers.update(concurrency_limit_headers(concurrency))
        if not concurrency.allowed:
            await self._model_pool_limiter.release(
                pool_id=model_pool_policy.pool_id,
                operation_id=operation_id,
            )
            raise ExternalApiError("CONCURRENCY_LIMITED", headers=headers)
        lease = _ConcurrencyLeaseGuard(
            limiter=self._concurrency_limiter,
            model_pool_limiter=self._model_pool_limiter,
            model_pool_id=model_pool_policy.pool_id,
            api_client_id=caller.api_client_id,
            operation_id=operation_id,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            monotonic_seconds=self._monotonic_seconds,
        )

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
                await lease.run(
                    run_in_threadpool(
                        self._reserve_quota,
                        caller=caller,
                        operation_id=operation_id,
                        request_fingerprint=request_fingerprint,
                        model_id=request.model,
                        stream=False,
                        reserved_tokens=reserved_tokens,
                        now=operation_started_at,
                    )
                )
            except QuotaAdmissionError as exc:
                raise ExternalApiError(
                    _quota_error(exc),
                    operation_id=exc.operation_id,
                    headers=headers,
                ) from exc

            try:
                credit = await lease.run(
                    self._openwebui.reserve_operation(
                        operation_id=operation_id,
                        service_user_id=policy.service_user_id,
                        model_id=request.model,
                        provider="bifrost",
                    )
                )
            except OpenWebUIUpstreamError as exc:
                public_code = _credit_error(exc)
                if public_code in {"CREDIT_BALANCE_EXHAUSTED", "CREDIT_ACCOUNT_FROZEN"}:
                    await lease.run(
                        run_in_threadpool(
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
            await lease.run(
                run_in_threadpool(
                    self._record_credit,
                    operation_id=operation_id,
                    settlement_id=settlement_id,
                    usage_operation_id=str(usage_operation_id),
                    now=max(int(time()), operation_started_at),
                )
            )

            try:
                response = await lease.run(
                    self._bifrost.forward_chat_completion(
                        request.bifrost_payload(),
                        virtual_key=policy.bifrost_virtual_key,
                    )
                )
            except BifrostUpstreamError as exc:
                public_code = _provider_error(exc)
                try:
                    cancelled = await lease.run(
                        self._openwebui.cancel_operation(
                            operation_id=operation_id,
                            service_user_id=policy.service_user_id,
                        )
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
                    or cancelled.model_id != request.model
                    or cancelled.provider != "bifrost"
                    or cancelled.usage_operation_id != f"{operation_id}:usage"
                    or cancelled.prompt_tokens != 0
                    or cancelled.completion_tokens != 0
                    or cancelled.total_tokens != 0
                    or cancelled.status not in {"cancelled_charged", "failed_no_charge"}
                    or cancelled.charged_micro != 0
                ):
                    raise ExternalApiError(
                        "CONTROL_PLANE_UNAVAILABLE",
                        operation_id=operation_id,
                        headers=headers,
                    ) from exc
                await lease.run(
                    run_in_threadpool(
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
                )
                terminal_committed = True
                raise ExternalApiError(public_code, operation_id=operation_id, headers=headers) from exc

            usage = response.usage
            await lease.run(
                run_in_threadpool(
                    self._record_usage,
                    operation_id=operation_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    now=max(int(time()), operation_started_at),
                )
            )
            try:
                settled = await lease.run(
                    self._openwebui.settle_operation(
                        operation_id=operation_id,
                        service_user_id=policy.service_user_id,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                    )
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

            await lease.run(
                run_in_threadpool(
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
            )
            terminal_committed = True
            return ExternalChatResult(response=response, rate_limit_headers=headers)
        finally:
            released_cleanly = await self._release_active_leases(
                api_client_id=caller.api_client_id,
                pool_id=model_pool_policy.pool_id,
                operation_id=operation_id,
            )
            if not released_cleanly and not terminal_committed:
                raise ControlPlaneUnavailable from None

    async def prepare_stream(
        self,
        *,
        request: ChatCompletionRequest,
        caller: AuthenticatedCaller,
        server_request_id: str,
        request_fingerprint: str,
    ) -> ExternalChatStreamResult:
        if not request.stream:
            raise ExternalApiError("INVALID_REQUEST")

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

        model_pool_policy, _model_pool = await self._acquire_model_pool(
            model_id=request.model,
            api_client_id=caller.api_client_id,
            operation_id=operation_id,
            headers=headers,
        )
        try:
            concurrency = await self._concurrency_limiter.acquire(
                api_client_id=caller.api_client_id,
                operation_id=operation_id,
                limit=policy.concurrency_limit,
            )
        except BaseException:
            await self._model_pool_limiter.release(
                pool_id=model_pool_policy.pool_id,
                operation_id=operation_id,
            )
            raise
        headers.update(concurrency_limit_headers(concurrency))
        if not concurrency.allowed:
            await self._model_pool_limiter.release(
                pool_id=model_pool_policy.pool_id,
                operation_id=operation_id,
            )
            raise ExternalApiError("CONCURRENCY_LIMITED", headers=headers)
        lease = _ConcurrencyLeaseGuard(
            limiter=self._concurrency_limiter,
            model_pool_limiter=self._model_pool_limiter,
            model_pool_id=model_pool_policy.pool_id,
            api_client_id=caller.api_client_id,
            operation_id=operation_id,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            monotonic_seconds=self._monotonic_seconds,
        )

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
                await lease.run(
                    run_in_threadpool(
                        self._reserve_quota,
                        caller=caller,
                        operation_id=operation_id,
                        request_fingerprint=request_fingerprint,
                        model_id=request.model,
                        stream=True,
                        reserved_tokens=reserved_tokens,
                        now=operation_started_at,
                    )
                )
            except QuotaAdmissionError as exc:
                raise ExternalApiError(
                    _quota_error(exc),
                    operation_id=exc.operation_id,
                    headers=headers,
                ) from exc

            try:
                credit = await lease.run(
                    self._openwebui.reserve_operation(
                        operation_id=operation_id,
                        service_user_id=policy.service_user_id,
                        model_id=request.model,
                        provider="bifrost",
                    )
                )
            except OpenWebUIUpstreamError as exc:
                public_code = _credit_error(exc)
                if public_code in {"CREDIT_BALANCE_EXHAUSTED", "CREDIT_ACCOUNT_FROZEN"}:
                    await lease.run(
                        run_in_threadpool(
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
            await lease.run(
                run_in_threadpool(
                    self._record_credit,
                    operation_id=operation_id,
                    settlement_id=settlement_id,
                    usage_operation_id=str(usage_operation_id),
                    now=max(int(time()), operation_started_at),
                )
            )
            admission = _ChatAdmission(
                request=request,
                caller=caller,
                policy=policy,
                operation_id=operation_id,
                operation_started_at=operation_started_at,
                started_ns=started_ns,
                server_request_id=server_request_id,
                qps=qps,
                concurrency=concurrency,
                lease=lease,
                model_pool_id=model_pool_policy.pool_id,
                headers=headers,
                settlement_id=settlement_id,
                usage_operation_id=expected_usage_operation_id,
            )
            return ExternalChatStreamResult(
                stream=self._stream_response(admission),
                rate_limit_headers=headers,
            )
        except BaseException:
            released_cleanly = await self._release_active_leases(
                api_client_id=caller.api_client_id,
                pool_id=model_pool_policy.pool_id,
                operation_id=operation_id,
            )
            if not released_cleanly and not terminal_committed:
                raise ControlPlaneUnavailable from None
            raise

    async def _settle_stream(self, admission: _ChatAdmission, usage: ChatUsage) -> int:
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        total_tokens = usage.total_tokens
        await run_in_threadpool(
            self._record_usage,
            operation_id=admission.operation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            now=max(int(time()), admission.operation_started_at),
        )
        settled = await self._openwebui.settle_operation(
            operation_id=admission.operation_id,
            service_user_id=admission.policy.service_user_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        if (
            str(settled.operation_id) != admission.operation_id
            or str(settled.service_user_id) != admission.policy.service_user_id
            or str(settled.settlement_id) != admission.settlement_id
            or settled.usage_operation_id != admission.usage_operation_id
            or settled.model_id != admission.request.model
            or settled.provider != "bifrost"
            or settled.status != "succeeded_charged"
            or settled.prompt_tokens != prompt_tokens
            or settled.completion_tokens != completion_tokens
            or settled.total_tokens != total_tokens
        ):
            raise ExternalApiError(
                "CONTROL_PLANE_UNAVAILABLE",
                operation_id=admission.operation_id,
                headers=admission.headers,
            )
        return settled.charged_micro

    async def _cancel_stream(self, admission: _ChatAdmission) -> None:
        cancelled = await self._openwebui.cancel_operation(
            operation_id=admission.operation_id,
            service_user_id=admission.policy.service_user_id,
        )
        if (
            str(cancelled.operation_id) != admission.operation_id
            or str(cancelled.service_user_id) != admission.policy.service_user_id
            or str(cancelled.settlement_id) != admission.settlement_id
            or cancelled.model_id != admission.request.model
            or cancelled.provider != "bifrost"
            or cancelled.usage_operation_id != admission.usage_operation_id
            or cancelled.prompt_tokens != 0
            or cancelled.completion_tokens != 0
            or cancelled.total_tokens != 0
            or cancelled.status not in {"cancelled_charged", "failed_no_charge"}
            or cancelled.charged_micro != 0
        ):
            raise ExternalApiError(
                "CONTROL_PLANE_UNAVAILABLE",
                operation_id=admission.operation_id,
                headers=admission.headers,
            )

    async def _finish_stream_failure(
        self,
        admission: _ChatAdmission,
        decoder: OpenAIStreamDecoder,
        *,
        code: str,
        status: Literal["rejected", "abandoned"],
        outcome: str,
        stage: str,
    ) -> None:
        if decoder.usage is None:
            await self._cancel_stream(admission)
            charged_micro = 0
        else:
            charged_micro = await self._settle_stream(admission, decoder.usage)
        await run_in_threadpool(
            self._terminalize,
            operation_id=admission.operation_id,
            operation_started_at=admission.operation_started_at,
            started_ns=admission.started_ns,
            server_request_id=admission.server_request_id,
            qps=admission.qps,
            concurrency=admission.concurrency,
            status=status,
            outcome=outcome,
            stage=stage,
            code=code,
            charged_micro=charged_micro,
        )

    async def _try_finish_stream_failure(
        self,
        admission: _ChatAdmission,
        decoder: OpenAIStreamDecoder,
        *,
        code: str,
        status: Literal["rejected", "abandoned"],
        outcome: str,
        stage: str,
    ) -> bool:
        try:
            await self._finish_stream_failure(
                admission,
                decoder,
                code=code,
                status=status,
                outcome=outcome,
                stage=stage,
            )
        except Exception:
            return False
        return True

    async def _heartbeat_stream(self, admission: _ChatAdmission) -> None:
        await admission.lease.heartbeat()
        touched = await run_in_threadpool(
            self._touch_operation,
            operation_id=admission.operation_id,
            now=max(int(time()), admission.operation_started_at),
        )
        if not touched:
            raise RuntimeError("stream operation is already terminal")

    async def _stream_response(self, admission: _ChatAdmission) -> AsyncIterator[bytes]:
        decoder = OpenAIStreamDecoder()
        upstream = self._bifrost.stream_chat_completion(
            admission.request.bifrost_payload(),
            virtual_key=admission.policy.bifrost_virtual_key,
        ).__aiter__()
        pending: asyncio.Task[bytes] | None = None
        terminal_committed = False
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(upstream))
                heartbeat_wait = admission.lease.seconds_until_heartbeat()
                completed, _ = await asyncio.wait(
                    {pending},
                    timeout=heartbeat_wait,
                )
                observed_at = self._monotonic_seconds()
                if not completed:
                    await self._heartbeat_stream(admission)
                    yield SSE_HEARTBEAT
                    continue
                if admission.lease.heartbeat_due(observed_at=observed_at):
                    await self._heartbeat_stream(admission)
                    yield SSE_HEARTBEAT
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    pending = None
                    decoder.finish()
                    return
                pending = None
                for frame in decoder.feed(chunk):
                    if frame != SSE_DONE:
                        yield frame
                        continue
                    usage = decoder.usage
                    if usage is None:
                        raise AssertionError("decoder returned DONE without usage")
                    charged_micro = await self._settle_stream(admission, usage)
                    await run_in_threadpool(
                        self._terminalize,
                        operation_id=admission.operation_id,
                        operation_started_at=admission.operation_started_at,
                        started_ns=admission.started_ns,
                        server_request_id=admission.server_request_id,
                        qps=admission.qps,
                        concurrency=admission.concurrency,
                        status="completed",
                        outcome="success",
                        stage="response",
                        code="OK",
                        charged_micro=charged_micro,
                    )
                    terminal_committed = True
                    yield SSE_DONE
                    return
        except (GeneratorExit, asyncio.CancelledError):
            terminal_committed = await self._try_finish_stream_failure(
                admission,
                decoder,
                code="CLIENT_DISCONNECTED",
                status="abandoned",
                outcome="cancelled",
                stage="response",
            )
            raise
        except BifrostUpstreamError as exc:
            public_code = _provider_error(exc)
            try:
                await self._finish_stream_failure(
                    admission,
                    decoder,
                    code=public_code,
                    status="rejected",
                    outcome="provider_error",
                    stage="provider",
                )
                terminal_committed = True
            except Exception:
                public_code = "CONTROL_PLANE_UNAVAILABLE"
            yield _stream_error_frame(
                public_code,
                server_request_id=admission.server_request_id,
                operation_id=admission.operation_id,
            )
        except ControlPlaneUnavailable:
            terminal_committed = await self._try_finish_stream_failure(
                admission,
                decoder,
                code="CONTROL_PLANE_UNAVAILABLE",
                status="abandoned",
                outcome="system_error",
                stage="recovery",
            )
            yield _stream_error_frame(
                "CONTROL_PLANE_UNAVAILABLE",
                server_request_id=admission.server_request_id,
                operation_id=admission.operation_id,
            )
        except Exception:
            terminal_committed = await self._try_finish_stream_failure(
                admission,
                decoder,
                code="INTERNAL_ERROR",
                status="abandoned",
                outcome="system_error",
                stage="recovery",
            )
            yield _stream_error_frame(
                "INTERNAL_ERROR",
                server_request_id=admission.server_request_id,
                operation_id=admission.operation_id,
            )
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            try:
                await _close_stream_iterator(upstream)
            finally:
                released_cleanly = await self._release_active_leases(
                    api_client_id=admission.caller.api_client_id,
                    pool_id=admission.model_pool_id,
                    operation_id=admission.operation_id,
                )
                if not released_cleanly and not terminal_committed:
                    raise ControlPlaneUnavailable from None
