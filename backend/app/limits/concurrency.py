from dataclasses import dataclass

from backend.app.limits.redis import (
    AsyncRedisClient,
    ControlPlaneUnavailable,
    build_redis_key,
    fail_closed,
    load_lua_script,
    parse_integer_sequence,
    redis_identifier,
)

DEFAULT_CONCURRENCY_LEASE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ConcurrencyDecision:
    allowed: bool
    count: int
    limit: int
    remaining: int
    lease_expires_at_ms: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class ConcurrencySnapshot:
    occupied: int
    limit: int
    remaining: int
    saturated: bool
    next_lease_expires_at_ms: int
    last_lease_expires_at_ms: int
    observed_at_ms: int


class ConcurrencyLimiter:
    def __init__(
        self,
        redis: AsyncRedisClient,
        *,
        lease_seconds: int = DEFAULT_CONCURRENCY_LEASE_SECONDS,
    ) -> None:
        if not 1 <= lease_seconds <= 900:
            raise ValueError("concurrency lease must be between 1 and 900 seconds")
        self._redis = redis
        self._lease_milliseconds = lease_seconds * 1_000
        self._acquire_script = load_lua_script("concurrency_acquire.lua")
        self._heartbeat_script = load_lua_script("concurrency_heartbeat.lua")
        self._observe_script = load_lua_script("concurrency_observe.lua")
        self._release_script = load_lua_script("concurrency_release.lua")

    def _key_and_member(self, api_client_id: str, operation_id: str) -> tuple[str, str]:
        return build_redis_key("concurrency", api_client_id), redis_identifier(operation_id)

    async def acquire(self, *, api_client_id: str, operation_id: str, limit: int) -> ConcurrencyDecision:
        if not 1 <= limit <= 1_000_000:
            raise ValueError("concurrency limit must be between 1 and 1000000")
        key, member = self._key_and_member(api_client_id, operation_id)
        raw_result = await fail_closed(
            self._redis.eval(self._acquire_script, 1, key, self._lease_milliseconds, limit, member)
        )
        allowed_value, count, lease_expires_at_ms, observed_at_ms = parse_integer_sequence(raw_result, length=4)
        if allowed_value not in {0, 1} or count < 0 or lease_expires_at_ms < observed_at_ms:
            raise ControlPlaneUnavailable
        return ConcurrencyDecision(
            allowed=bool(allowed_value),
            count=count,
            limit=limit,
            remaining=max(limit - count, 0),
            lease_expires_at_ms=lease_expires_at_ms,
            observed_at_ms=observed_at_ms,
        )

    async def heartbeat(self, *, api_client_id: str, operation_id: str) -> bool:
        key, member = self._key_and_member(api_client_id, operation_id)
        raw_result = await fail_closed(
            self._redis.eval(self._heartbeat_script, 1, key, self._lease_milliseconds, member)
        )
        extended, expires_at_ms, observed_at_ms = parse_integer_sequence(raw_result, length=3)
        if extended not in {0, 1} or expires_at_ms < observed_at_ms:
            raise ControlPlaneUnavailable
        return bool(extended)

    async def observe(self, *, api_client_id: str, limit: int) -> ConcurrencySnapshot:
        if not 1 <= limit <= 1_000_000:
            raise ValueError("concurrency limit must be between 1 and 1000000")
        key, _member = self._key_and_member(api_client_id, "observation")
        raw_result = await fail_closed(self._redis.eval(self._observe_script, 1, key))
        occupied, next_expiry, last_expiry, observed_at = parse_integer_sequence(raw_result, length=4)
        if (
            occupied < 0
            or next_expiry < observed_at
            or last_expiry < next_expiry
            or (occupied == 0 and (next_expiry != observed_at or last_expiry != observed_at))
        ):
            raise ControlPlaneUnavailable
        return ConcurrencySnapshot(
            occupied=occupied,
            limit=limit,
            remaining=max(limit - occupied, 0),
            saturated=occupied >= limit,
            next_lease_expires_at_ms=next_expiry,
            last_lease_expires_at_ms=last_expiry,
            observed_at_ms=observed_at,
        )

    async def release(self, *, api_client_id: str, operation_id: str) -> bool:
        key, member = self._key_and_member(api_client_id, operation_id)
        raw_result = await fail_closed(self._redis.eval(self._release_script, 1, key, member))
        try:
            removed = int(raw_result)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneUnavailable from exc
        if removed not in {0, 1}:
            raise ControlPlaneUnavailable
        return bool(removed)
