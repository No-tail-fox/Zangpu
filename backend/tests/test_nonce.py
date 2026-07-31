import asyncio

from fakeredis.aioredis import FakeRedis

from backend.app.limits.nonce import NonceGuard


def test_concurrent_nonce_replay_admits_exactly_one_request() -> None:
    async def scenario() -> tuple[list[bool], list[str], int]:
        redis = FakeRedis()
        guard = NonceGuard(redis, ttl_seconds=600)
        results = await asyncio.gather(
            *(
                guard.claim(
                    api_client_id="client-sensitive-name",
                    credential_id="credential-sensitive-id",
                    nonce="nonce-sensitive-value",
                )
                for _ in range(32)
            )
        )
        keys = [key.decode("ascii") for key in await redis.keys("*")]
        ttl = await redis.ttl(keys[0])
        await redis.aclose()
        return results, keys, ttl

    results, keys, ttl = asyncio.run(scenario())

    assert sum(results) == 1
    assert len(keys) == 1
    raw_values = ("client-sensitive-name", "credential-sensitive-id", "nonce-sensitive-value")
    assert all(value not in keys[0] for value in raw_values)
    assert 0 < ttl <= 600
