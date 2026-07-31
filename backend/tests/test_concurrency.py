import asyncio

from fakeredis.aioredis import FakeRedis

from backend.app.limits.concurrency import ConcurrencyLimiter


def test_concurrency_lease_ownership_heartbeat_release_and_ttl_recovery() -> None:
    async def scenario() -> tuple[list[object], list[str]]:
        redis = FakeRedis()
        limiter = ConcurrencyLimiter(redis, lease_seconds=1)

        first = await limiter.acquire(api_client_id="client-1", operation_id="operation-sensitive-1", limit=2)
        second = await limiter.acquire(api_client_id="client-1", operation_id="operation-sensitive-2", limit=2)
        rejected = await limiter.acquire(api_client_id="client-1", operation_id="operation-sensitive-3", limit=2)
        wrong_heartbeat = await limiter.heartbeat(api_client_id="client-1", operation_id="operation-sensitive-3")
        owner_heartbeat = await limiter.heartbeat(api_client_id="client-1", operation_id="operation-sensitive-1")
        wrong_release = await limiter.release(api_client_id="client-1", operation_id="operation-sensitive-3")
        owner_release = await limiter.release(api_client_id="client-1", operation_id="operation-sensitive-1")
        admitted_after_release = await limiter.acquire(
            api_client_id="client-1", operation_id="operation-sensitive-3", limit=2
        )

        stale = await limiter.acquire(api_client_id="client-ttl", operation_id="operation-stale", limit=1)
        await asyncio.sleep(1.05)
        recovered = await limiter.acquire(api_client_id="client-ttl", operation_id="operation-fresh", limit=1)

        keys = await redis.keys("*")
        values: list[str] = [key.decode("ascii") for key in keys]
        for key in keys:
            values.extend(member.decode("ascii") for member in await redis.zrange(key, 0, -1))
        await redis.aclose()
        return (
            [
                first,
                second,
                rejected,
                wrong_heartbeat,
                owner_heartbeat,
                wrong_release,
                owner_release,
                admitted_after_release,
                stale,
                recovered,
            ],
            values,
        )

    results, redis_values = asyncio.run(scenario())
    (
        first,
        second,
        rejected,
        wrong_heartbeat,
        owner_heartbeat,
        wrong_release,
        owner_release,
        admitted_after_release,
        stale,
        recovered,
    ) = results

    assert first.allowed and second.allowed
    assert not rejected.allowed
    assert not wrong_heartbeat
    assert owner_heartbeat
    assert not wrong_release
    assert owner_release
    assert admitted_after_release.allowed
    assert stale.allowed and recovered.allowed
    assert all("operation-" not in value and "client-" not in value for value in redis_values)
