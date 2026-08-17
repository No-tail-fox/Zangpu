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


def test_concurrency_observation_is_atomic_aggregate_and_prunes_expired_leases() -> None:
    async def scenario() -> tuple[object, object, object]:
        redis = FakeRedis()
        limiter = ConcurrencyLimiter(redis, lease_seconds=1)
        idle = await limiter.observe(api_client_id="client-observe", limit=2)
        await limiter.acquire(api_client_id="client-observe", operation_id="operation-a", limit=2)
        await limiter.acquire(api_client_id="client-observe", operation_id="operation-b", limit=2)
        saturated = await limiter.observe(api_client_id="client-observe", limit=2)
        await asyncio.sleep(1.05)
        recovered = await limiter.observe(api_client_id="client-observe", limit=2)
        await redis.aclose()
        return idle, saturated, recovered

    idle, saturated, recovered = asyncio.run(scenario())

    assert (idle.occupied, idle.remaining, idle.saturated) == (0, 2, False)
    assert idle.next_lease_expires_at_ms == idle.last_lease_expires_at_ms == idle.observed_at_ms
    assert (saturated.occupied, saturated.remaining, saturated.saturated) == (2, 0, True)
    assert saturated.next_lease_expires_at_ms >= saturated.observed_at_ms
    assert saturated.last_lease_expires_at_ms >= saturated.next_lease_expires_at_ms
    assert (recovered.occupied, recovered.remaining, recovered.saturated) == (0, 2, False)


def test_concurrent_acquire_never_overshoots_configured_limit() -> None:
    async def scenario() -> tuple[int, int]:
        redis = FakeRedis()
        limiter = ConcurrencyLimiter(redis, lease_seconds=5)
        decisions = await asyncio.gather(
            *(
                limiter.acquire(
                    api_client_id="client-race",
                    operation_id=f"operation-{index}",
                    limit=3,
                )
                for index in range(32)
            )
        )
        snapshot = await limiter.observe(api_client_id="client-race", limit=3)
        await redis.aclose()
        return sum(item.allowed for item in decisions), snapshot.occupied

    allowed, occupied = asyncio.run(scenario())

    assert (allowed, occupied) == (3, 3)
