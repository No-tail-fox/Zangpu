from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import time
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.app.api.errors import ExternalApiError
from backend.app.api.metadata_models import (
    ExternalModel,
    ExternalModelList,
    ExternalUsage,
    ExternalUsageScope,
)
from backend.app.limits.nonce import NonceGuard
from backend.app.limits.qps import QpsDecision, SlidingWindowQps
from backend.app.models.quotas import ApiClientQuotaUsage
from backend.app.security.dependencies import AuthenticatedCaller
from backend.app.services.callers import (
    CallerMetadataPolicy,
    CallerPolicyError,
    load_caller_metadata_policy,
)
from backend.app.services.quota import utc_day_start


@dataclass(frozen=True, slots=True)
class ExternalModelsResult:
    response: ExternalModelList
    rate_limit_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ExternalUsageResult:
    response: ExternalUsage
    rate_limit_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class _UsageSnapshot:
    request_count: int
    token_reserved: int
    token_consumed: int
    updated_at: int | None


def _rate_limit_headers(decision: QpsDecision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str((decision.reset_at_ms + 999) // 1_000),
    }


def _caller_error_code(error: CallerPolicyError) -> str:
    if error.code == "CLIENT_DISABLED":
        return "CLIENT_DISABLED"
    if error.code in {"CREDENTIAL_REVOKED", "CREDENTIAL_EXPIRED"}:
        return "AUTH_FAILED"
    return "CONTROL_PLANE_UNAVAILABLE"


def _remaining(limit: int | None, used: int) -> int | None:
    if limit is None:
        return None
    return max(limit - used, 0)


class ExternalMetadataService:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        nonce_guard: NonceGuard,
        qps_limiter: SlidingWindowQps,
        clock: Callable[[], float] = time,
    ) -> None:
        if not callable(clock):
            raise ValueError("metadata clock is invalid")
        self._sessions = sessions
        self._nonce_guard = nonce_guard
        self._qps_limiter = qps_limiter
        self._clock = clock

    def _load_policy(self, caller: AuthenticatedCaller, now: int) -> CallerMetadataPolicy:
        with self._sessions() as session:
            return load_caller_metadata_policy(session, caller=caller, now=now)

    def _load_usage(self, *, api_client_id: str, now: int) -> dict[str, _UsageSnapshot]:
        day_start = utc_day_start(now)
        with self._sessions() as session:
            rows = session.scalars(
                select(ApiClientQuotaUsage).where(
                    ApiClientQuotaUsage.api_client_id == api_client_id,
                    (
                        (ApiClientQuotaUsage.scope == "daily")
                        & (ApiClientQuotaUsage.period_start == day_start)
                    )
                    | (
                        (ApiClientQuotaUsage.scope == "lifetime")
                        & (ApiClientQuotaUsage.period_start == 0)
                    ),
                )
            ).all()
            return {
                row.scope: _UsageSnapshot(
                    request_count=row.request_count,
                    token_reserved=row.token_reserved,
                    token_consumed=row.token_consumed,
                    updated_at=row.updated_at,
                )
                for row in rows
            }

    async def _admit(
        self,
        *,
        caller: AuthenticatedCaller,
        server_request_id: str,
        permission: Literal["models.read", "usage.read"],
        now: int,
    ) -> tuple[CallerMetadataPolicy, dict[str, str]]:
        claimed = await self._nonce_guard.claim(
            api_client_id=caller.api_client_id,
            credential_id=caller.credential_id,
            nonce=caller.nonce,
        )
        if not claimed:
            raise ExternalApiError("AUTH_FAILED")
        try:
            policy = await run_in_threadpool(self._load_policy, caller, now)
        except CallerPolicyError as exc:
            raise ExternalApiError(_caller_error_code(exc)) from exc
        if permission not in policy.allowed_endpoints:
            raise ExternalApiError("ENDPOINT_FORBIDDEN")
        qps = await self._qps_limiter.admit(
            api_client_id=caller.api_client_id,
            server_request_id=server_request_id,
            limit=policy.qps_limit,
        )
        headers = _rate_limit_headers(qps)
        if not qps.allowed:
            raise ExternalApiError("QPS_LIMITED", headers=headers)
        return policy, headers

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or value < 0:
            raise ValueError("metadata clock returned an invalid value")
        return int(value)

    async def list_models(
        self,
        *,
        caller: AuthenticatedCaller,
        server_request_id: str,
    ) -> ExternalModelsResult:
        now = self._now()
        policy, headers = await self._admit(
            caller=caller,
            server_request_id=server_request_id,
            permission="models.read",
            now=now,
        )
        return ExternalModelsResult(
            response=ExternalModelList(
                data=[ExternalModel(id=model_id) for model_id in sorted(policy.allowed_models)]
            ),
            rate_limit_headers=headers,
        )

    async def get_usage(
        self,
        *,
        caller: AuthenticatedCaller,
        server_request_id: str,
    ) -> ExternalUsageResult:
        now = self._now()
        policy, headers = await self._admit(
            caller=caller,
            server_request_id=server_request_id,
            permission="usage.read",
            now=now,
        )
        rows = await run_in_threadpool(self._load_usage, api_client_id=policy.api_client_id, now=now)
        empty = _UsageSnapshot(0, 0, 0, None)
        daily = rows.get("daily", empty)
        lifetime = rows.get("lifetime", empty)
        day_start = utc_day_start(now)
        return ExternalUsageResult(
            response=ExternalUsage(
                as_of=now,
                daily=ExternalUsageScope(
                    scope="daily",
                    period_start=day_start,
                    period_end=day_start + 86_400,
                    request_count=daily.request_count,
                    request_limit=policy.daily_request_limit,
                    request_remaining=_remaining(policy.daily_request_limit, daily.request_count),
                    token_consumed=daily.token_consumed,
                    token_reserved=daily.token_reserved,
                    token_limit=policy.daily_token_limit,
                    token_remaining=_remaining(
                        policy.daily_token_limit,
                        daily.token_consumed + daily.token_reserved,
                    ),
                    updated_at=daily.updated_at,
                ),
                lifetime=ExternalUsageScope(
                    scope="lifetime",
                    period_start=0,
                    period_end=None,
                    request_count=lifetime.request_count,
                    request_limit=policy.total_request_limit,
                    request_remaining=_remaining(policy.total_request_limit, lifetime.request_count),
                    token_consumed=lifetime.token_consumed,
                    token_reserved=lifetime.token_reserved,
                    token_limit=policy.total_token_limit,
                    token_remaining=_remaining(
                        policy.total_token_limit,
                        lifetime.token_consumed + lifetime.token_reserved,
                    ),
                    updated_at=lifetime.updated_at,
                ),
            ),
            rate_limit_headers=headers,
        )
