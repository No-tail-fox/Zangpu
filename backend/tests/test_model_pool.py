import asyncio

from fakeredis.aioredis import FakeRedis

from backend.app.limits.model_pool import ModelPoolLimiter


async def _admit(
    limiter: ModelPoolLimiter,
    *,
    pool_id: str,
    api_client_id: str,
    operation_id: str,
    active_limit: int = 1,
    queue_limit: int = 2,
    caller_queue_limit: int = 1,
    queue_wait_seconds: int = 5,
):
    return await limiter.admit_or_enqueue(
        pool_id=pool_id,
        api_client_id=api_client_id,
        operation_id=operation_id,
        active_limit=active_limit,
        queue_limit=queue_limit,
        caller_queue_limit=caller_queue_limit,
        queue_wait_seconds=queue_wait_seconds,
    )


def test_model_pool_enforces_active_global_queue_and_caller_queue_limits() -> None:
    async def scenario():
        redis = FakeRedis()
        limiter = ModelPoolLimiter(redis, lease_seconds=5)

        active = await _admit(
            limiter,
            pool_id="pool-sensitive-a",
            api_client_id="caller-sensitive-a",
            operation_id="operation-sensitive-active",
        )
        caller_queued = await _admit(
            limiter,
            pool_id="pool-sensitive-a",
            api_client_id="caller-sensitive-a",
            operation_id="operation-sensitive-queued-a",
        )
        caller_full = await _admit(
            limiter,
            pool_id="pool-sensitive-a",
            api_client_id="caller-sensitive-a",
            operation_id="operation-sensitive-caller-full",
        )
        globally_queued = await _admit(
            limiter,
            pool_id="pool-sensitive-a",
            api_client_id="caller-sensitive-b",
            operation_id="operation-sensitive-queued-b",
        )
        global_full = await _admit(
            limiter,
            pool_id="pool-sensitive-a",
            api_client_id="caller-sensitive-c",
            operation_id="operation-sensitive-global-full",
        )
        snapshot = await limiter.observe(pool_id="pool-sensitive-a", active_limit=1, queue_limit=2)
        await redis.aclose()
        return active, caller_queued, caller_full, globally_queued, global_full, snapshot

    active, caller_queued, caller_full, globally_queued, global_full, snapshot = asyncio.run(scenario())

    assert active.status == "admitted"
    assert (active.active_count, active.active_remaining, active.queue_count) == (1, 0, 0)
    assert caller_queued.status == "queued" and caller_queued.queue_position == 1
    assert caller_full.status == "caller_queue_full"
    assert globally_queued.status == "queued" and globally_queued.queue_position == 2
    assert global_full.status == "queue_full"
    assert (snapshot.active_count, snapshot.active_remaining) == (1, 0)
    assert (snapshot.queue_count, snapshot.queue_remaining, snapshot.pool_queue_count) == (2, 0, 2)


def test_model_pool_queue_is_fifo_idempotent_and_cannot_be_bypassed() -> None:
    async def scenario():
        redis = FakeRedis()
        limiter = ModelPoolLimiter(redis, lease_seconds=5)
        active = await _admit(
            limiter, pool_id="pool-a", api_client_id="caller-a", operation_id="operation-active"
        )
        first = await _admit(
            limiter, pool_id="pool-a", api_client_id="caller-b", operation_id="operation-first"
        )
        repeated = await _admit(
            limiter, pool_id="pool-a", api_client_id="caller-b", operation_id="operation-first"
        )
        second = await _admit(
            limiter, pool_id="pool-a", api_client_id="caller-c", operation_id="operation-second"
        )
        await limiter.release(pool_id="pool-a", operation_id="operation-active")
        bypass = await _admit(
            limiter, pool_id="pool-a", api_client_id="caller-c", operation_id="operation-second"
        )
        promoted_first = await _admit(
            limiter, pool_id="pool-a", api_client_id="caller-b", operation_id="operation-first"
        )
        await limiter.release(pool_id="pool-a", operation_id="operation-first")
        promoted_second = await _admit(
            limiter, pool_id="pool-a", api_client_id="caller-c", operation_id="operation-second"
        )
        await redis.aclose()
        return active, first, repeated, second, bypass, promoted_first, promoted_second

    active, first, repeated, second, bypass, promoted_first, promoted_second = asyncio.run(scenario())

    assert active.status == "admitted"
    assert first.status == repeated.status == "queued"
    assert first.queue_position == repeated.queue_position == 1
    assert first.expires_at_ms == repeated.expires_at_ms
    assert second.status == "queued" and second.queue_position == 2
    assert bypass.status == "queued" and bypass.queue_position == 2
    assert promoted_first.status == "admitted"
    assert promoted_second.status == "admitted"


def test_model_pool_global_queue_limit_spans_distinct_pools() -> None:
    async def scenario():
        redis = FakeRedis()
        limiter = ModelPoolLimiter(redis, lease_seconds=5)
        await _admit(limiter, pool_id="pool-a", api_client_id="caller-a", operation_id="active-a")
        await _admit(limiter, pool_id="pool-b", api_client_id="caller-b", operation_id="active-b")
        first = await _admit(limiter, pool_id="pool-a", api_client_id="caller-c", operation_id="queued-a")
        second = await _admit(limiter, pool_id="pool-b", api_client_id="caller-d", operation_id="queued-b")
        rejected = await _admit(limiter, pool_id="pool-b", api_client_id="caller-e", operation_id="queued-c")
        pool_a = await limiter.observe(pool_id="pool-a", active_limit=1, queue_limit=2)
        pool_b = await limiter.observe(pool_id="pool-b", active_limit=1, queue_limit=2)
        await redis.aclose()
        return first, second, rejected, pool_a, pool_b

    first, second, rejected, pool_a, pool_b = asyncio.run(scenario())

    assert first.status == second.status == "queued"
    assert rejected.status == "queue_full"
    assert pool_a.queue_count == pool_b.queue_count == 2
    assert pool_a.pool_queue_count == pool_b.pool_queue_count == 1


def test_model_pool_cancel_timeout_heartbeat_release_and_identifier_redaction() -> None:
    async def scenario():
        redis = FakeRedis()
        limiter = ModelPoolLimiter(redis, lease_seconds=1)
        active = await _admit(
            limiter,
            pool_id="pool-sensitive",
            api_client_id="caller-sensitive",
            operation_id="operation-sensitive-active",
            queue_wait_seconds=1,
        )
        queued = await _admit(
            limiter,
            pool_id="pool-sensitive",
            api_client_id="caller-sensitive",
            operation_id="operation-sensitive-queued",
            queue_wait_seconds=1,
        )
        wrong_heartbeat = await limiter.heartbeat(
            pool_id="pool-sensitive", operation_id="operation-sensitive-wrong"
        )
        owner_heartbeat = await limiter.heartbeat(
            pool_id="pool-sensitive", operation_id="operation-sensitive-active"
        )
        wrong_cancel = await limiter.cancel(
            pool_id="pool-sensitive",
            api_client_id="caller-sensitive",
            operation_id="operation-sensitive-wrong",
        )
        owner_cancel = await limiter.cancel(
            pool_id="pool-sensitive",
            api_client_id="caller-sensitive",
            operation_id="operation-sensitive-queued",
        )
        queued_again = await _admit(
            limiter,
            pool_id="pool-sensitive",
            api_client_id="caller-sensitive",
            operation_id="operation-sensitive-timeout",
            queue_wait_seconds=1,
        )
        await asyncio.sleep(1.05)
        recovered = await _admit(
            limiter,
            pool_id="pool-sensitive",
            api_client_id="caller-sensitive-new",
            operation_id="operation-sensitive-recovered",
            queue_wait_seconds=1,
        )
        snapshot = await limiter.observe(pool_id="pool-sensitive", active_limit=1, queue_limit=2)

        stored: list[str] = []
        for key in await redis.keys("*"):
            stored.append(key.decode("ascii"))
            kind = await redis.type(key)
            if kind == b"zset":
                stored.extend(member.decode("ascii") for member in await redis.zrange(key, 0, -1))
            elif kind == b"string":
                value = await redis.get(key)
                if value is not None:
                    stored.append(value.decode("ascii"))
        await redis.aclose()
        return (
            active,
            queued,
            wrong_heartbeat,
            owner_heartbeat,
            wrong_cancel,
            owner_cancel,
            queued_again,
            recovered,
            snapshot,
            stored,
        )

    (
        active,
        queued,
        wrong_heartbeat,
        owner_heartbeat,
        wrong_cancel,
        owner_cancel,
        queued_again,
        recovered,
        snapshot,
        stored,
    ) = asyncio.run(scenario())

    assert active.status == "admitted" and queued.status == "queued"
    assert not wrong_heartbeat and owner_heartbeat
    assert not wrong_cancel and owner_cancel
    assert queued_again.status == "queued"
    assert recovered.status == "admitted"
    assert (snapshot.active_count, snapshot.queue_count, snapshot.pool_queue_count) == (1, 0, 0)
    assert all(
        marker not in value
        for value in stored
        for marker in ("pool-sensitive", "caller-sensitive", "operation-sensitive")
    )


def test_concurrent_model_pool_promotion_never_overshoots_active_limit() -> None:
    async def scenario() -> tuple[int, int, int]:
        redis = FakeRedis()
        limiter = ModelPoolLimiter(redis, lease_seconds=5)
        for index in range(3):
            decision = await _admit(
                limiter,
                pool_id="pool-race",
                api_client_id=f"caller-active-{index}",
                operation_id=f"active-{index}",
                active_limit=3,
                queue_limit=32,
                caller_queue_limit=2,
            )
            assert decision.status == "admitted"
        queued_operations = [f"queued-{index}" for index in range(29)]
        for index, operation_id in enumerate(queued_operations):
            decision = await _admit(
                limiter,
                pool_id="pool-race",
                api_client_id=f"caller-queued-{index}",
                operation_id=operation_id,
                active_limit=3,
                queue_limit=32,
                caller_queue_limit=2,
            )
            assert decision.status == "queued"
        for index in range(3):
            await limiter.release(pool_id="pool-race", operation_id=f"active-{index}")
        decisions = await asyncio.gather(
            *(
                _admit(
                    limiter,
                    pool_id="pool-race",
                    api_client_id=f"caller-queued-{index}",
                    operation_id=operation_id,
                    active_limit=3,
                    queue_limit=32,
                    caller_queue_limit=2,
                )
                for index, operation_id in enumerate(queued_operations)
            )
        )
        snapshot = await limiter.observe(pool_id="pool-race", active_limit=3, queue_limit=32)
        await redis.aclose()
        return sum(item.status == "admitted" for item in decisions), snapshot.active_count, snapshot.pool_queue_count

    promoted, active_count, queued_count = asyncio.run(scenario())

    assert (promoted, active_count, queued_count) == (3, 3, 26)
