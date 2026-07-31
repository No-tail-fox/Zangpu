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

DEFAULT_QPS_WINDOW_MILLISECONDS = 1_000


@dataclass(frozen=True, slots=True)
class QpsDecision:
    allowed: bool
    count: int
    limit: int
    remaining: int
    reset_at_ms: int
    observed_at_ms: int


class SlidingWindowQps:
    def __init__(
        self,
        redis: AsyncRedisClient,
        *,
        window_milliseconds: int = DEFAULT_QPS_WINDOW_MILLISECONDS,
    ) -> None:
        if not 100 <= window_milliseconds <= 60_000:
            raise ValueError("QPS window must be between 100 and 60000 milliseconds")
        self._redis = redis
        self._window_milliseconds = window_milliseconds
        self._script = load_lua_script("qps_sliding_window.lua")

    async def admit(self, *, api_client_id: str, server_request_id: str, limit: int) -> QpsDecision:
        if not 1 <= limit <= 1_000_000:
            raise ValueError("QPS limit must be between 1 and 1000000")
        key = build_redis_key("qps", api_client_id)
        member = redis_identifier(server_request_id)
        raw_result = await fail_closed(
            self._redis.eval(self._script, 1, key, self._window_milliseconds, limit, member)
        )
        allowed_value, count, reset_at_ms, observed_at_ms = parse_integer_sequence(raw_result, length=4)
        if allowed_value not in {0, 1} or count < 0 or reset_at_ms < observed_at_ms:
            raise ControlPlaneUnavailable
        return QpsDecision(
            allowed=bool(allowed_value),
            count=count,
            limit=limit,
            remaining=max(limit - count, 0),
            reset_at_ms=reset_at_ms,
            observed_at_ms=observed_at_ms,
        )
