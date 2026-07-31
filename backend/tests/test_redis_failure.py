import asyncio
import base64
import json

import pytest
from pydantic import ValidationError
from redis.exceptions import TimeoutError as RedisTimeoutError

from backend.app.limits.concurrency import ConcurrencyLimiter
from backend.app.limits.nonce import NonceGuard
from backend.app.limits.qps import SlidingWindowQps
from backend.app.limits.redis import (
    ControlPlaneUnavailable,
    build_redis_key,
    create_redis_client,
    redis_identifier,
)
from backend.app.settings import Settings


class FailingRedis:
    def __init__(self) -> None:
        self.calls = 0

    async def set(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise RedisTimeoutError("redis timeout sentinel")

    async def eval(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise RedisTimeoutError("redis timeout sentinel")


def settings_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment": "test",
        "service_version": "0.1.0",
        "database_url": "postgresql+psycopg://control:control@postgres:5432/control",
        "redis_url": "redis://redis:6379/0",
        "bifrost_base_url": "http://bifrost:8080",
        "bifrost_management_token": "bifrost-management-token-that-is-at-least-32-bytes",
        "bifrost_expected_version": "v1.6.3",
        "openwebui_internal_base_url": "http://openwebui:8080",
        "admin_session_secret": "admin-session-secret-that-is-at-least-32-bytes",
        "api_credential_keys": json.dumps({"v1": base64.b64encode(bytes(32)).decode("ascii")}),
        "api_credential_active_key_id": "v1",
    }
    values.update(overrides)
    return values


def test_distributed_control_settings_enforce_ttl_relationships() -> None:
    settings = Settings(**settings_values())  # type: ignore[arg-type]
    assert (
        settings.contract_api_timestamp_tolerance_seconds,
        settings.contract_api_nonce_ttl_seconds,
        settings.contract_api_concurrency_lease_seconds,
        settings.contract_api_concurrency_heartbeat_seconds,
    ) == (300, 600, 60, 15)

    with pytest.raises(ValidationError):
        Settings(**settings_values(contract_api_nonce_ttl_seconds=599))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(
            **settings_values(
                contract_api_concurrency_lease_seconds=60,
                contract_api_concurrency_heartbeat_seconds=30,
            )
        )  # type: ignore[arg-type]

def test_redis_identifiers_are_hash_only() -> None:
    identifier = redis_identifier("caller-sensitive-value")
    key = build_redis_key("nonce", "caller-sensitive-value", "nonce-sensitive-value")

    assert len(identifier) == 64
    assert set(identifier) <= set("0123456789abcdef")
    assert "sensitive" not in key
    assert key.startswith("zangpu:v1:nonce:")


def test_production_redis_client_has_bounded_timeouts_and_pool() -> None:
    client = create_redis_client("redis://redis:6379/0", socket_timeout_seconds=2.5, max_connections=64)
    options = client.connection_pool.connection_kwargs

    assert options["socket_connect_timeout"] == 2.5
    assert options["socket_timeout"] == 2.5
    assert options["decode_responses"] is False
    assert client.connection_pool.max_connections == 64
    asyncio.run(client.aclose())


@pytest.mark.parametrize("control_name", ["nonce", "qps", "concurrency"])
def test_redis_timeout_fails_closed_without_process_fallback(control_name: str) -> None:
    async def scenario() -> tuple[ControlPlaneUnavailable, int]:
        redis = FailingRedis()
        control = {
            "nonce": lambda: NonceGuard(redis).claim(
                api_client_id="client-1", credential_id="credential-1", nonce="nonce-0123456789"
            ),
            "qps": lambda: SlidingWindowQps(redis).admit(
                api_client_id="client-1", server_request_id="request-1", limit=1
            ),
            "concurrency": lambda: ConcurrencyLimiter(redis).acquire(
                api_client_id="client-1", operation_id="operation-1", limit=1
            ),
        }[control_name]
        with pytest.raises(ControlPlaneUnavailable) as captured:
            await control()
        return captured.value, redis.calls

    failure, calls = asyncio.run(scenario())
    response = failure.to_response("req_server_0123456789")

    assert calls == 1
    assert str(failure) == "distributed control unavailable"
    assert response.status_code == 503
    assert json.loads(response.body) == {
        "error": {
            "code": "CONTROL_PLANE_UNAVAILABLE",
            "message": "Control plane is temporarily unavailable.",
            "request_id": "req_server_0123456789",
            "retryable": True,
        }
    }
