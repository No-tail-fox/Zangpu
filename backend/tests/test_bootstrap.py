import base64
import json
from importlib import import_module
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.security.keyring import KeyringConfigurationError

ROOT = Path(__file__).resolve().parents[2]


def configure_required_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "ZANGPU_ENVIRONMENT": "test",
        "ZANGPU_SERVICE_VERSION": "0.1.0",
        "ZANGPU_DATABASE_URL": "postgresql+psycopg://control:control@postgres:5432/control",
        "ZANGPU_REDIS_URL": "redis://redis:6379/0",
        "ZANGPU_BIFROST_BASE_URL": "http://bifrost:8080",
        "ZANGPU_OPENWEBUI_INTERNAL_BASE_URL": "http://openwebui:8080",
        "ZANGPU_ADMIN_SESSION_SECRET": "admin-session-secret-that-is-at-least-32-bytes",
        "ZANGPU_API_CREDENTIAL_KEYS": json.dumps(
            {"v1": base64.b64encode(bytes(32)).decode("ascii")}, separators=(",", ":")
        ),
        "ZANGPU_API_CREDENTIAL_ACTIVE_KEY_ID": "v1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_settings_fail_closed_when_required_values_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("ZANGPU_"):
            monkeypatch.delenv(key, raising=False)

    settings_module = import_module("backend.app.settings")
    with pytest.raises(ValidationError):
        settings_module.Settings(_env_file=None)


def test_secret_settings_are_redacted_from_repr_and_model_dumps(monkeypatch: pytest.MonkeyPatch) -> None:
    values = configure_required_environment(monkeypatch)
    settings_module = import_module("backend.app.settings")
    settings = settings_module.Settings(_env_file=None)

    rendered = "\n".join((repr(settings), repr(settings.model_dump()), settings.model_dump_json()))
    assert values["ZANGPU_ADMIN_SESSION_SECRET"] not in rendered
    assert values["ZANGPU_API_CREDENTIAL_KEYS"] not in rendered


def test_invalid_credential_keyring_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("ZANGPU_API_CREDENTIAL_KEYS", "not-json")
    settings_module = import_module("backend.app.settings")
    main_module = import_module("backend.app.main")
    settings = settings_module.Settings(_env_file=None)

    with pytest.raises(KeyringConfigurationError), TestClient(main_module.create_app(settings)):
        pass


def test_versioned_health_starts_with_bounded_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_required_environment(monkeypatch)
    main_module = import_module("backend.app.main")

    with TestClient(main_module.create_app()) as client:
        response = client.get("/api/v1/external/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "zangpu-api-control-plane",
        "version": "0.1.0",
        "api_version": "v1",
    }


def test_compose_publishes_only_gateway_ports() -> None:
    compose_path = ROOT / "deploy" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]

    published_services = {name for name, service in services.items() if service.get("ports")}
    assert published_services == {"gateway"}
    assert {"backend", "web", "bifrost", "postgres", "redis"}.issubset(services)
    for name in ("backend", "web", "bifrost", "postgres", "redis"):
        assert "ports" not in services[name]

    published_targets = {port["target"] for port in services["gateway"]["ports"]}
    assert published_targets == {9000, 9001}
    assert all(port["host_ip"] == "127.0.0.1" for port in services["gateway"]["ports"])


def test_docker_context_excludes_local_dependency_and_build_artifacts() -> None:
    patterns = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert {
        ".bootstrap-uv",
        ".venv",
        "web/node_modules",
        "web/.svelte-kit",
        "web/build",
    }.issubset(patterns)
