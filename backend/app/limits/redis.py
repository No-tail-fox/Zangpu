import re
from collections.abc import Awaitable
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

REDIS_NAMESPACE_RE = re.compile(r"^[a-z_]{1,32}$")
LUA_SCRIPT_RE = re.compile(r"^[a-z_]{1,64}\.lua$")


class AsyncRedisClient(Protocol):
    async def set(self, name: str, value: str, *, ex: int, nx: bool) -> object: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


class ControlPlaneUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("distributed control unavailable")

    def to_response(self, server_request_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "CONTROL_PLANE_UNAVAILABLE",
                    "message": "Control plane is temporarily unavailable.",
                    "request_id": server_request_id,
                    "retryable": True,
                }
            },
            headers={
                "Cache-Control": "no-store",
                "X-Zangpu-Request-Id": server_request_id,
            },
        )


def create_redis_client(
    redis_url: str,
    *,
    socket_timeout_seconds: float = 2.0,
    max_connections: int = 100,
) -> Redis:
    if not redis_url:
        raise ValueError("Redis URL must not be empty")
    if not 0.1 <= socket_timeout_seconds <= 30:
        raise ValueError("Redis socket timeout must be between 0.1 and 30 seconds")
    if not 1 <= max_connections <= 10_000:
        raise ValueError("Redis max connections must be between 1 and 10000")
    return Redis.from_url(
        redis_url,
        decode_responses=False,
        health_check_interval=30,
        max_connections=max_connections,
        socket_connect_timeout=socket_timeout_seconds,
        socket_keepalive=True,
        socket_timeout=socket_timeout_seconds,
    )


def redis_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise ValueError("Redis identifiers must be bounded non-empty text")
    return sha256(value.encode("utf-8")).hexdigest()


def build_redis_key(namespace: str, *identifiers: str) -> str:
    if not REDIS_NAMESPACE_RE.fullmatch(namespace) or not identifiers:
        raise ValueError("invalid Redis key namespace or identifiers")
    suffix = ":".join(redis_identifier(identifier) for identifier in identifiers)
    return f"zangpu:v1:{namespace}:{suffix}"


@lru_cache(maxsize=8)
def load_lua_script(filename: str) -> str:
    if not LUA_SCRIPT_RE.fullmatch(filename):
        raise ValueError("invalid Lua script name")
    return (Path(__file__).with_name("lua") / filename).read_text(encoding="utf-8")


async def fail_closed[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except RedisError as exc:
        raise ControlPlaneUnavailable from exc


def parse_integer_sequence(value: object, *, length: int) -> tuple[int, ...]:
    if not isinstance(value, list | tuple) or len(value) != length:
        raise ControlPlaneUnavailable
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ControlPlaneUnavailable from exc
