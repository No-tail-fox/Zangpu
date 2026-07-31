import asyncio

from fakeredis.aioredis import FakeRedis

from backend.app.limits.qps import SlidingWindowQps


def test_qps_sliding_window_has_zero_boundary_overshoot() -> None:
    async def scenario() -> tuple[list[object], int, list[str]]:
        redis = FakeRedis()
        limiter = SlidingWindowQps(redis, window_milliseconds=1_000)
        decisions = await asyncio.gather(
            *(
                limiter.admit(
                    api_client_id="client-sensitive-name",
                    server_request_id=f"request-sensitive-{index}",
                    limit=5,
                )
                for index in range(20)
            )
        )
        keys = [key.decode("ascii") for key in await redis.keys("*")]
        count = await redis.zcard(keys[0])
        members = [member.decode("ascii") for member in await redis.zrange(keys[0], 0, -1)]
        await redis.aclose()
        return decisions, count, keys + members

    decisions, stored_count, redis_values = asyncio.run(scenario())

    assert sum(decision.allowed for decision in decisions) == 5
    assert stored_count == 5
    assert all(decision.count <= 5 for decision in decisions)
    assert all(decision.count == 5 for decision in decisions if not decision.allowed)
    assert all("sensitive" not in value for value in redis_values)
    assert all(decision.reset_at_ms >= decision.observed_at_ms for decision in decisions)
