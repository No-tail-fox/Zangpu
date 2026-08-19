import asyncio

from fakeredis.aioredis import FakeRedis

from backend.app.limits.model_pool import ModelPoolLimiter, ModelPoolPolicy
from backend.app.services.capacity import AdminCapacityService


def test_admin_capacity_aggregates_unique_pools_and_global_queue_without_side_effects() -> None:
    async def scenario():
        redis = FakeRedis()
        limiter = ModelPoolLimiter(redis, lease_seconds=5)
        policies = {
            "model-a": ModelPoolPolicy(pool_id="pool-a", active_limit=1),
            "model-a-alias": ModelPoolPolicy(pool_id="pool-a", active_limit=1),
            "model-b": ModelPoolPolicy(pool_id="pool-b", active_limit=2),
        }
        service = AdminCapacityService(limiter, policies=policies, global_queue_limit=4)
        await limiter.admit_or_enqueue(
            pool_id="pool-a",
            api_client_id="caller-a",
            operation_id="active-a",
            active_limit=1,
            queue_limit=4,
            caller_queue_limit=2,
            queue_wait_seconds=5,
        )
        await limiter.admit_or_enqueue(
            pool_id="pool-a",
            api_client_id="caller-b",
            operation_id="queued-a",
            active_limit=1,
            queue_limit=4,
            caller_queue_limit=2,
            queue_wait_seconds=5,
        )
        await limiter.admit_or_enqueue(
            pool_id="pool-b",
            api_client_id="caller-c",
            operation_id="active-b",
            active_limit=2,
            queue_limit=4,
            caller_queue_limit=2,
            queue_wait_seconds=5,
        )
        snapshot = await service.snapshot()
        await redis.aclose()
        return snapshot

    snapshot = asyncio.run(scenario())

    assert snapshot.state == "queued"
    assert (snapshot.pool_count, snapshot.active_count, snapshot.active_limit) == (2, 2, 3)
    assert (snapshot.active_remaining, snapshot.global_queue_count, snapshot.global_queue_remaining) == (1, 1, 3)
    assert [pool.pool_id for pool in snapshot.pools] == ["pool-a", "pool-b"]
    assert snapshot.pools[0].model_ids == ["model-a", "model-a-alias"]
    assert snapshot.pools[0].state == "queued"
    assert (snapshot.pools[0].active_count, snapshot.pools[0].pool_queue_count) == (1, 1)
    assert snapshot.pools[1].state == "available"
    assert (snapshot.pools[1].active_count, snapshot.pools[1].active_remaining) == (1, 1)


def test_admin_capacity_handles_an_empty_nonproduction_policy_without_redis_calls() -> None:
    class UnexpectedLimiter:
        async def observe(self, **_kwargs: object):
            raise AssertionError("empty capacity must not read Redis")

    service = AdminCapacityService(
        UnexpectedLimiter(),  # type: ignore[arg-type]
        policies={},
        global_queue_limit=200,
        epoch_milliseconds=lambda: 1_800_000_000_000,
    )

    snapshot = asyncio.run(service.snapshot())

    assert snapshot.model_dump(mode="json") == {
        "state": "idle",
        "pool_count": 0,
        "active_count": 0,
        "active_limit": 0,
        "active_remaining": 0,
        "global_queue_count": 0,
        "global_queue_limit": 200,
        "global_queue_remaining": 200,
        "observed_at_ms": 1_800_000_000_000,
        "pools": [],
    }
