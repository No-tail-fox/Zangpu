import csv
import io

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base
from backend.app.models.audits import ApiClientAdminAudit
from backend.app.models.clients import ApiClient
from backend.app.models.events import ApiCallEvent
from backend.app.services.observability import (
    AdminEventQuery,
    AdminObservabilityLimitError,
    AdminObservabilityService,
)


def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def caller(client_id: str, name: str) -> ApiClient:
    return ApiClient(
        id=client_id,
        name=name,
        description=None,
        status="active",
        allowed_endpoints=["chat.completions"],
        allowed_models=["model-1"],
        group_ids=[],
        qps_limit=10,
        concurrency_limit=2,
        daily_request_limit=None,
        daily_token_limit=None,
        total_request_limit=None,
        total_token_limit=None,
        max_output_tokens_per_request=128,
        version=1,
        created_by="admin",
        updated_by="admin",
        created_at=1_800_000_000,
        updated_at=1_800_000_000,
    )


def event(
    event_id: str,
    *,
    client_id: str,
    created_at: int,
    duration_ms: int,
    outcome: str = "success",
    stage: str = "response",
    http_status: int = 200,
    business_code: str = "OK",
    model_id: str = "model-1",
    total_tokens: int = 10,
    charged_micro: int = 5,
    stream: bool = False,
    endpoint: str = "chat.completions",
) -> ApiCallEvent:
    prompt_tokens = total_tokens // 2
    completion_tokens = total_tokens - prompt_tokens
    return ApiCallEvent(
        id=event_id,
        server_request_id=f"server-{event_id}",
        client_request_id=f"client-{event_id}",
        operation_id=None,
        api_client_id=client_id,
        credential_id=None,
        endpoint=endpoint,
        method="POST",
        model_id=model_id,
        stream=stream,
        outcome=outcome,
        stage=stage,
        http_status=http_status,
        business_code=business_code,
        retryable=outcome != "success",
        duration_ms=duration_ms,
        quota_overrun=False,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        charged_micro=charged_micro,
        qps_observed=1,
        concurrency_observed=1,
        daily_requests_after=1,
        daily_tokens_after=total_tokens,
        total_requests_after=1,
        total_tokens_after=total_tokens,
        remote_ip_hash="a" * 64,
        user_agent_family="SDK",
        started_at=created_at,
        completed_at=created_at + 1,
        created_at=created_at,
    )


def seeded_service(*, max_export_rows: int = 10_000) -> tuple[AdminObservabilityService, sessionmaker]:
    factory = sessions()
    with factory.begin() as session:
        session.add_all((caller("caller-1", "Caller One"), caller("caller-2", "Caller Two")))
        session.add_all(
            (
                event("event-1", client_id="caller-1", created_at=3_600, duration_ms=10),
                event(
                    "event-2",
                    client_id="caller-1",
                    created_at=3_700,
                    duration_ms=20,
                    total_tokens=20,
                    charged_micro=10,
                    stream=True,
                ),
                event(
                    "event-3",
                    client_id="caller-1",
                    created_at=7_200,
                    duration_ms=100,
                    outcome="provider_error",
                    stage="provider",
                    http_status=503,
                    business_code="MODEL_UNAVAILABLE",
                    total_tokens=0,
                    charged_micro=0,
                ),
                event(
                    "event-4",
                    client_id="caller-2",
                    created_at=7_300,
                    duration_ms=200,
                    outcome="rejected",
                    stage="quota",
                    http_status=429,
                    business_code="DAILY_REQUEST_QUOTA_EXCEEDED",
                    model_id="=SUM(1,1)",
                    total_tokens=0,
                    charged_micro=0,
                    endpoint="models.read",
                ),
            )
        )
    return AdminObservabilityService(factory, max_export_rows=max_export_rows), factory


def test_event_query_filters_orders_and_summarizes_exact_percentiles() -> None:
    service, _factory = seeded_service()

    page = service.list_events(AdminEventQuery(api_client_id="caller-1"), offset=0, limit=2)
    summary = service.summarize(AdminEventQuery(), bucket_seconds=3_600)

    assert page.total == 3
    assert [item.id for item in page.items] == ["event-3", "event-2"]
    assert summary.request_count == 4
    assert (summary.success_count, summary.failure_count) == (2, 2)
    assert (summary.total_tokens, summary.charged_micro) == (30, 15)
    assert (summary.duration_p50_ms, summary.duration_p95_ms, summary.duration_p99_ms) == (20, 200, 200)
    assert [(item.bucket_start, item.request_count) for item in summary.trend] == [(3_600, 2), (7_200, 2)]


@pytest.mark.parametrize(
    ("filters", "expected_ids"),
    (
        ({"api_client_id": "caller-2"}, ["event-4"]),
        ({"created_from": 7_200, "created_to": 7_200}, ["event-3"]),
        ({"outcome": "provider_error"}, ["event-3"]),
        ({"stage": "quota"}, ["event-4"]),
        ({"http_status": 429}, ["event-4"]),
        ({"business_code": "MODEL_UNAVAILABLE"}, ["event-3"]),
        ({"endpoint": "models.read"}, ["event-4"]),
        ({"model_id": "=SUM(1,1)"}, ["event-4"]),
        ({"stream": True}, ["event-2"]),
    ),
)
def test_event_query_applies_each_frozen_filter(filters: dict[str, object], expected_ids: list[str]) -> None:
    service, _factory = seeded_service()

    page = service.list_events(AdminEventQuery.model_validate(filters), limit=20)

    assert [item.id for item in page.items] == expected_ids


def test_event_export_is_bounded_formula_safe_and_audited() -> None:
    service, factory = seeded_service(max_export_rows=4)

    exported = service.export_csv(AdminEventQuery(), actor_id="admin", now=8_000)
    rows = list(csv.DictReader(io.StringIO(exported.content)))

    assert exported.row_count == 4
    assert exported.filename.startswith("zangpu-api-events-")
    assert rows[0]["model_id"] == "'=SUM(1,1)"
    assert "prompt" not in exported.content.lower()
    assert "answer" not in exported.content.lower()
    assert "secret" not in exported.content.lower()
    with factory() as session:
        audit = session.scalar(select(ApiClientAdminAudit).where(ApiClientAdminAudit.action == "events.exported"))
    assert audit is not None and audit.after_summary["row_count"] == 4

    limited, _factory = seeded_service(max_export_rows=3)
    with pytest.raises(AdminObservabilityLimitError, match="narrow"):
        limited.export_csv(AdminEventQuery(), actor_id="admin", now=8_000)

    aggregate_limited = AdminObservabilityService(factory, max_aggregate_rows=3)
    with pytest.raises(AdminObservabilityLimitError, match="narrow"):
        aggregate_limited.summarize(AdminEventQuery())


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("=1", "'=1"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@name", "'@name"),
        ("a\r\nb", "a  b"),
        (" \t=1", "'  =1"),
    ),
)
def test_csv_string_sanitization(value: str, expected: str) -> None:
    assert AdminObservabilityService._csv_value(value) == expected  # noqa: SLF001
