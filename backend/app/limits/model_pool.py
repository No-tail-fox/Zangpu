from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.limits.redis import (
    AsyncRedisClient,
    ControlPlaneUnavailable,
    build_redis_key,
    fail_closed,
    load_lua_script,
    parse_integer_sequence,
    redis_identifier,
)

DEFAULT_MODEL_POOL_LEASE_SECONDS = 60
ModelPoolStatus = Literal["admitted", "queued", "caller_queue_full", "queue_full"]


class ModelPoolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    pool_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    active_limit: int = Field(ge=1, le=1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPoolDecision:
    status: ModelPoolStatus
    active_count: int
    active_limit: int
    active_remaining: int
    queue_count: int
    queue_limit: int
    queue_remaining: int
    pool_queue_count: int
    queue_position: int
    expires_at_ms: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class ModelPoolSnapshot:
    active_count: int
    active_limit: int
    active_remaining: int
    queue_count: int
    queue_limit: int
    queue_remaining: int
    pool_queue_count: int
    next_active_expires_at_ms: int
    next_queue_expires_at_ms: int
    observed_at_ms: int


class ModelPoolLimiter:
    def __init__(
        self,
        redis: AsyncRedisClient,
        *,
        lease_seconds: int = DEFAULT_MODEL_POOL_LEASE_SECONDS,
    ) -> None:
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 900:
            raise ValueError("model-pool lease must be between 1 and 900 seconds")
        self._redis = redis
        self._lease_milliseconds = lease_seconds * 1_000
        self._admit_script = load_lua_script("model_pool_admit.lua")
        self._cancel_script = load_lua_script("model_pool_cancel.lua")
        self._heartbeat_script = load_lua_script("concurrency_heartbeat.lua")
        self._observe_script = load_lua_script("model_pool_observe.lua")
        self._release_script = load_lua_script("concurrency_release.lua")

    @staticmethod
    def _active_key(pool_id: str) -> str:
        return build_redis_key("model_active", pool_id)

    @staticmethod
    def _queue_keys(pool_id: str, api_client_id: str) -> tuple[str, str, str, str, str]:
        return (
            build_redis_key("model_queue", pool_id),
            build_redis_key("model_queue_expiry", pool_id),
            build_redis_key("model_queue_global", "global"),
            build_redis_key("model_queue_caller", api_client_id),
            build_redis_key("model_queue_seq", pool_id),
        )

    @staticmethod
    def _member(operation_id: str) -> str:
        return redis_identifier(operation_id)

    @staticmethod
    def _validate_limits(
        *,
        active_limit: int,
        queue_limit: int,
        caller_queue_limit: int,
        queue_wait_seconds: int,
    ) -> None:
        if isinstance(active_limit, bool) or not 1 <= active_limit <= 1_000_000:
            raise ValueError("model-pool active limit must be between 1 and 1000000")
        if isinstance(queue_limit, bool) or not 0 <= queue_limit <= 1_000_000:
            raise ValueError("global queue limit must be between 0 and 1000000")
        if isinstance(caller_queue_limit, bool) or not 0 <= caller_queue_limit <= 1_000_000:
            raise ValueError("caller queue limit must be between 0 and 1000000")
        if queue_limit > 0 and caller_queue_limit == 0:
            raise ValueError("caller queue limit must be positive when queueing is enabled")
        if isinstance(queue_wait_seconds, bool) or not 1 <= queue_wait_seconds <= 300:
            raise ValueError("queue wait must be between 1 and 300 seconds")

    async def admit_or_enqueue(
        self,
        *,
        pool_id: str,
        api_client_id: str,
        operation_id: str,
        active_limit: int,
        queue_limit: int,
        caller_queue_limit: int,
        queue_wait_seconds: int,
    ) -> ModelPoolDecision:
        self._validate_limits(
            active_limit=active_limit,
            queue_limit=queue_limit,
            caller_queue_limit=caller_queue_limit,
            queue_wait_seconds=queue_wait_seconds,
        )
        active_key = self._active_key(pool_id)
        queue_key, queue_expiry_key, global_queue_key, caller_queue_key, sequence_key = self._queue_keys(
            pool_id, api_client_id
        )
        member = self._member(operation_id)
        raw_result = await fail_closed(
            self._redis.eval(
                self._admit_script,
                6,
                active_key,
                queue_key,
                queue_expiry_key,
                global_queue_key,
                caller_queue_key,
                sequence_key,
                self._lease_milliseconds,
                active_limit,
                queue_wait_seconds * 1_000,
                queue_limit,
                caller_queue_limit,
                member,
            )
        )
        status_value, active_count, queue_count, pool_queue_count, position, expires_at, observed_at = (
            parse_integer_sequence(raw_result, length=7)
        )
        statuses: dict[int, ModelPoolStatus] = {
            1: "admitted",
            2: "queued",
            3: "caller_queue_full",
            4: "queue_full",
        }
        status = statuses.get(status_value)
        if (
            status is None
            or not 0 <= active_count <= active_limit
            or not 0 <= queue_count <= queue_limit
            or not 0 <= pool_queue_count <= queue_count
            or expires_at < observed_at
            or (status == "admitted" and (active_count < 1 or position != 0))
            or (status == "queued" and not 1 <= position <= pool_queue_count)
            or (status not in {"admitted", "queued"} and position != 0)
        ):
            raise ControlPlaneUnavailable
        return ModelPoolDecision(
            status=status,
            active_count=active_count,
            active_limit=active_limit,
            active_remaining=max(active_limit - active_count, 0),
            queue_count=queue_count,
            queue_limit=queue_limit,
            queue_remaining=max(queue_limit - queue_count, 0),
            pool_queue_count=pool_queue_count,
            queue_position=position,
            expires_at_ms=expires_at,
            observed_at_ms=observed_at,
        )

    async def heartbeat(self, *, pool_id: str, operation_id: str) -> bool:
        key = self._active_key(pool_id)
        member = self._member(operation_id)
        raw_result = await fail_closed(
            self._redis.eval(self._heartbeat_script, 1, key, self._lease_milliseconds, member)
        )
        extended, expires_at_ms, observed_at_ms = parse_integer_sequence(raw_result, length=3)
        if extended not in {0, 1} or expires_at_ms < observed_at_ms:
            raise ControlPlaneUnavailable
        return bool(extended)

    async def release(self, *, pool_id: str, operation_id: str) -> bool:
        key = self._active_key(pool_id)
        member = self._member(operation_id)
        raw_result = await fail_closed(self._redis.eval(self._release_script, 1, key, member))
        try:
            removed = int(raw_result)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneUnavailable from exc
        if removed not in {0, 1}:
            raise ControlPlaneUnavailable
        return bool(removed)

    async def cancel(self, *, pool_id: str, api_client_id: str, operation_id: str) -> bool:
        queue_key, queue_expiry_key, global_queue_key, caller_queue_key, _sequence_key = self._queue_keys(
            pool_id, api_client_id
        )
        member = self._member(operation_id)
        raw_result = await fail_closed(
            self._redis.eval(
                self._cancel_script,
                4,
                queue_key,
                queue_expiry_key,
                global_queue_key,
                caller_queue_key,
                member,
            )
        )
        try:
            removed = int(raw_result)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneUnavailable from exc
        if removed not in {0, 1}:
            raise ControlPlaneUnavailable
        return bool(removed)

    async def observe(self, *, pool_id: str, active_limit: int, queue_limit: int) -> ModelPoolSnapshot:
        if isinstance(active_limit, bool) or not 1 <= active_limit <= 1_000_000:
            raise ValueError("model-pool active limit must be between 1 and 1000000")
        if isinstance(queue_limit, bool) or not 0 <= queue_limit <= 1_000_000:
            raise ValueError("global queue limit must be between 0 and 1000000")
        active_key = self._active_key(pool_id)
        queue_key, queue_expiry_key, global_queue_key, _caller_queue_key, _sequence_key = self._queue_keys(
            pool_id, "observation"
        )
        raw_result = await fail_closed(
            self._redis.eval(
                self._observe_script,
                4,
                active_key,
                queue_key,
                queue_expiry_key,
                global_queue_key,
            )
        )
        active_count, queue_count, pool_queue_count, active_expiry, queue_expiry, observed_at = (
            parse_integer_sequence(raw_result, length=6)
        )
        if (
            not 0 <= active_count <= active_limit
            or not 0 <= queue_count <= queue_limit
            or not 0 <= pool_queue_count <= queue_count
            or active_expiry < observed_at
            or queue_expiry < observed_at
            or (active_count == 0 and active_expiry != observed_at)
            or (pool_queue_count == 0 and queue_expiry != observed_at)
        ):
            raise ControlPlaneUnavailable
        return ModelPoolSnapshot(
            active_count=active_count,
            active_limit=active_limit,
            active_remaining=max(active_limit - active_count, 0),
            queue_count=queue_count,
            queue_limit=queue_limit,
            queue_remaining=max(queue_limit - queue_count, 0),
            pool_queue_count=pool_queue_count,
            next_active_expires_at_ms=active_expiry,
            next_queue_expires_at_ms=queue_expiry,
            observed_at_ms=observed_at,
        )
