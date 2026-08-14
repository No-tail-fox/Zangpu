from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models.audits import ApiClientAdminAudit
from backend.app.models.base import new_uuid
from backend.app.models.events import EVENT_OUTCOMES, EVENT_STAGES, ApiCallEvent

ALLOWED_TREND_BUCKET_SECONDS = frozenset({300, 3_600, 86_400})
MAX_PAGE_SIZE = 200
MAX_EXPORT_ROWS = 10_000
MAX_AGGREGATE_ROWS = 100_000


class AdminObservabilityLimitError(RuntimeError):
    pass


class AdminEventQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    api_client_id: str | None = Field(default=None, min_length=1, max_length=36)
    created_from: int | None = Field(default=None, ge=0)
    created_to: int | None = Field(default=None, ge=0)
    outcome: str | None = Field(default=None, max_length=24)
    stage: str | None = Field(default=None, max_length=24)
    http_status: int | None = Field(default=None, ge=100, le=599)
    business_code: str | None = Field(default=None, min_length=1, max_length=64)
    endpoint: str | None = Field(default=None, min_length=1, max_length=64)
    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    stream: bool | None = None

    @field_validator("outcome")
    @classmethod
    def validate_outcome(cls, value: str | None) -> str | None:
        if value is not None and value not in EVENT_OUTCOMES:
            raise ValueError("unsupported event outcome")
        return value

    @field_validator("stage")
    @classmethod
    def validate_stage(cls, value: str | None) -> str | None:
        if value is not None and value not in EVENT_STAGES:
            raise ValueError("unsupported event stage")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> AdminEventQuery:
        if self.created_from is not None and self.created_to is not None and self.created_from > self.created_to:
            raise ValueError("created_from must not be after created_to")
        return self


class AdminEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, frozen=True)

    id: str
    server_request_id: str
    client_request_id: str
    operation_id: str | None
    api_client_id: str | None
    credential_id: str | None
    endpoint: str
    method: str
    model_id: str | None
    stream: bool
    outcome: str
    stage: str
    http_status: int
    business_code: str
    retryable: bool
    duration_ms: int
    quota_overrun: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    charged_micro: int
    qps_observed: int
    concurrency_observed: int
    daily_requests_after: int
    daily_tokens_after: int
    total_requests_after: int
    total_tokens_after: int
    remote_ip_hash: str | None
    user_agent_family: str | None
    started_at: int
    completed_at: int
    created_at: int


class AdminEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AdminEventItem]
    total: int
    offset: int
    limit: int


class AdminEventTrendBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket_start: int
    request_count: int
    success_count: int
    failure_count: int
    total_tokens: int
    charged_micro: int


class AdminEventSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_count: int
    success_count: int
    failure_count: int
    total_tokens: int
    charged_micro: int
    average_duration_ms: float | None
    duration_p50_ms: int | None
    duration_p95_ms: int | None
    duration_p99_ms: int | None
    quota_overrun_count: int
    bucket_seconds: int
    trend: list[AdminEventTrendBucket]


@dataclass(frozen=True, slots=True)
class AdminEventExport:
    filename: str
    row_count: int
    content: str


class AdminObservabilityService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        max_export_rows: int = MAX_EXPORT_ROWS,
        max_aggregate_rows: int = MAX_AGGREGATE_ROWS,
    ) -> None:
        if not 1 <= max_export_rows <= MAX_EXPORT_ROWS:
            raise ValueError(f"max_export_rows must be between 1 and {MAX_EXPORT_ROWS}")
        if not 1 <= max_aggregate_rows <= MAX_AGGREGATE_ROWS:
            raise ValueError(f"max_aggregate_rows must be between 1 and {MAX_AGGREGATE_ROWS}")
        self._sessions = sessions
        self._max_export_rows = max_export_rows
        self._max_aggregate_rows = max_aggregate_rows

    def list_events(self, query: AdminEventQuery, *, offset: int = 0, limit: int = 50) -> AdminEventPage:
        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        conditions = self._conditions(query)
        with self._sessions() as session:
            total = self._count(session, conditions)
            statement = (
                select(ApiCallEvent)
                .where(*conditions)
                .order_by(ApiCallEvent.created_at.desc(), ApiCallEvent.id.desc())
                .offset(offset)
                .limit(limit)
            )
            items = [AdminEventItem.model_validate(item) for item in session.scalars(statement)]
        return AdminEventPage(items=items, total=total, offset=offset, limit=limit)

    def summarize(self, query: AdminEventQuery, *, bucket_seconds: int = 3_600) -> AdminEventSummary:
        if bucket_seconds not in ALLOWED_TREND_BUCKET_SECONDS:
            raise ValueError("bucket_seconds must be one of 300, 3600 or 86400")
        conditions = self._conditions(query)
        with self._sessions() as session:
            total = self._count(session, conditions)
            if total > self._max_aggregate_rows:
                raise AdminObservabilityLimitError("aggregate query is too broad; narrow the event filters")
            statement = (
                select(
                    ApiCallEvent.created_at,
                    ApiCallEvent.outcome,
                    ApiCallEvent.duration_ms,
                    ApiCallEvent.total_tokens,
                    ApiCallEvent.charged_micro,
                    ApiCallEvent.quota_overrun,
                )
                .where(*conditions)
                .limit(self._max_aggregate_rows + 1)
            )
            rows = list(session.execute(statement))
        if len(rows) > self._max_aggregate_rows:
            raise AdminObservabilityLimitError("aggregate query is too broad; narrow the event filters")

        durations = sorted(row.duration_ms for row in rows)
        success_count = sum(row.outcome == "success" for row in rows)
        trend_values: dict[int, dict[str, int]] = defaultdict(
            lambda: {"request_count": 0, "success_count": 0, "failure_count": 0, "total_tokens": 0, "charged_micro": 0}
        )
        for row in rows:
            bucket_start = row.created_at - (row.created_at % bucket_seconds)
            bucket = trend_values[bucket_start]
            bucket["request_count"] += 1
            bucket["success_count" if row.outcome == "success" else "failure_count"] += 1
            bucket["total_tokens"] += row.total_tokens
            bucket["charged_micro"] += row.charged_micro

        trend = [
            AdminEventTrendBucket(bucket_start=bucket_start, **trend_values[bucket_start])
            for bucket_start in sorted(trend_values)
        ]
        return AdminEventSummary(
            request_count=len(rows),
            success_count=success_count,
            failure_count=len(rows) - success_count,
            total_tokens=sum(row.total_tokens for row in rows),
            charged_micro=sum(row.charged_micro for row in rows),
            average_duration_ms=(sum(durations) / len(durations)) if durations else None,
            duration_p50_ms=self._nearest_rank(durations, 0.50),
            duration_p95_ms=self._nearest_rank(durations, 0.95),
            duration_p99_ms=self._nearest_rank(durations, 0.99),
            quota_overrun_count=sum(row.quota_overrun for row in rows),
            bucket_seconds=bucket_seconds,
            trend=trend,
        )

    def export_csv(self, query: AdminEventQuery, *, actor_id: str, now: int) -> AdminEventExport:
        conditions = self._conditions(query)
        with self._sessions.begin() as session:
            total = self._count(session, conditions)
            if total > self._max_export_rows:
                raise AdminObservabilityLimitError("export query is too broad; narrow the event filters")
            statement = (
                select(ApiCallEvent)
                .where(*conditions)
                .order_by(ApiCallEvent.created_at.desc(), ApiCallEvent.id.desc())
                .limit(self._max_export_rows + 1)
            )
            events = list(session.scalars(statement))
            if len(events) > self._max_export_rows:
                raise AdminObservabilityLimitError("export query is too broad; narrow the event filters")
            filename = f"zangpu-api-events-{datetime.fromtimestamp(now, tz=UTC):%Y%m%dT%H%M%SZ}.csv"
            session.add(
                ApiClientAdminAudit(
                    id=new_uuid(),
                    actor_user_id=actor_id,
                    api_client_id=query.api_client_id if events and query.api_client_id is not None else None,
                    target_type="export",
                    target_id=filename,
                    action="events.exported",
                    changed_fields=["format", "row_count"],
                    before_summary={},
                    after_summary={"format": "csv", "row_count": len(events)},
                    created_at=now,
                )
            )
            content = self._render_csv(events)
        return AdminEventExport(filename=filename, row_count=len(events), content=content)

    @staticmethod
    def _conditions(query: AdminEventQuery) -> list[object]:
        conditions: list[object] = []
        for field_name in (
            "api_client_id",
            "outcome",
            "stage",
            "http_status",
            "business_code",
            "endpoint",
            "model_id",
            "stream",
        ):
            value = getattr(query, field_name)
            if value is not None:
                conditions.append(getattr(ApiCallEvent, field_name) == value)
        if query.created_from is not None:
            conditions.append(ApiCallEvent.created_at >= query.created_from)
        if query.created_to is not None:
            conditions.append(ApiCallEvent.created_at <= query.created_to)
        return conditions

    @staticmethod
    def _count(session: Session, conditions: list[object]) -> int:
        statement: Select[tuple[int]] = select(func.count()).select_from(ApiCallEvent).where(*conditions)
        return int(session.scalar(statement) or 0)

    @staticmethod
    def _nearest_rank(sorted_values: list[int], percentile: float) -> int | None:
        if not sorted_values:
            return None
        index = min(max(ceil(percentile * len(sorted_values)) - 1, 0), len(sorted_values) - 1)
        return sorted_values[index]

    @classmethod
    def _render_csv(cls, events: list[ApiCallEvent]) -> str:
        fields = (
            "id",
            "server_request_id",
            "client_request_id",
            "operation_id",
            "api_client_id",
            "credential_id",
            "endpoint",
            "method",
            "model_id",
            "stream",
            "outcome",
            "stage",
            "http_status",
            "business_code",
            "retryable",
            "duration_ms",
            "quota_overrun",
            "total_tokens",
            "charged_micro",
            "qps_observed",
            "concurrency_observed",
            "user_agent_family",
            "remote_ip_hash",
            "started_at",
            "completed_at",
            "created_at",
        )
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for event in events:
            writer.writerow({field_name: cls._csv_value(getattr(event, field_name)) for field_name in fields})
        return output.getvalue()

    @staticmethod
    def _csv_value(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        rendered = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if rendered.lstrip().startswith(("=", "+", "-", "@")):
            return f"'{rendered}"
        return rendered
