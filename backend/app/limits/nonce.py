from backend.app.limits.redis import AsyncRedisClient, ControlPlaneUnavailable, build_redis_key, fail_closed

DEFAULT_NONCE_TTL_SECONDS = 600


class NonceGuard:
    def __init__(self, redis: AsyncRedisClient, *, ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS) -> None:
        if not 60 <= ttl_seconds <= 86_400:
            raise ValueError("nonce TTL must be between 60 and 86400 seconds")
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def claim(self, *, api_client_id: str, credential_id: str, nonce: str) -> bool:
        key = build_redis_key("nonce", api_client_id, credential_id, nonce)
        result = await fail_closed(self._redis.set(key, "1", ex=self._ttl_seconds, nx=True))
        if result is True:
            return True
        if result is None or result is False:
            return False
        raise ControlPlaneUnavailable
