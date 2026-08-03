from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.app.models.base import new_uuid
from backend.app.models.clients import ApiClient
from backend.app.models.credentials import ApiClientCredential
from backend.app.models.events import EVENT_OUTCOMES, EVENT_STAGES, ApiCallEvent
from backend.app.models.operations import ApiCallOperation
from backend.app.models.quotas import ApiClientQuotaUsage

SIGNED_BIGINT_MAX = 2**63 - 1
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class QuotaAdmissionError(RuntimeError):
    __slots__ = ("code", "operation_id")

    def __init__(self, code: str, *, operation_id: str | None = None) -> None:
        self.code = code
        self.operation_id = operation_id
        super().__init__("external request was not admitted")


class QuotaStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OperationReservation:
    operation_id: str
    daily_requests_after: int
    daily_tokens_reserved_after: int
    total_requests_after: int
    total_tokens_reserved_after: int


@dataclass(frozen=True, slots=True)
class OperationTerminal:
    status: Literal["completed", "rejected", "abandoned"]
    outcome: str
    stage: str
    http_status: int
    business_code: str
    retryable: bool
    duration_ms: int
    charged_micro: int
    qps_observed: int
    concurrency_observed: int
    server_request_id: str
    remote_ip_hash: str | None
    user_agent_family: str | None
    completed_at: int

    def __post_init__(self) -> None:
        if self.outcome not in EVENT_OUTCOMES or self.stage not in EVENT_STAGES:
            raise ValueError("terminal event classification is invalid")
        if not 100 <= self.http_status <= 599:
            raise ValueError("terminal HTTP status is invalid")
        if not self.business_code or len(self.business_code) > 64:
            raise ValueError("terminal business code is invalid")
        if not 0 <= self.duration_ms <= SIGNED_BIGINT_MAX:
            raise ValueError("terminal duration is invalid")
        if self.charged_micro < 0 or self.qps_observed < 0 or self.concurrency_observed < 0:
            raise ValueError("terminal counters must be nonnegative")
        if not self.server_request_id or len(self.server_request_id) > 128:
            raise ValueError("server request ID is invalid")
        if self.remote_ip_hash is not None and len(self.remote_ip_hash) > 64:
            raise ValueError("remote IP hash is invalid")
        if self.user_agent_family is not None and len(self.user_agent_family) > 64:
            raise ValueError("user-agent family is invalid")


@dataclass(frozen=True, slots=True)
class OperationTerminalSnapshot:
    operation_id: str
    total_tokens: int
    daily_requests_after: int
    daily_tokens_after: int
    total_requests_after: int
    total_tokens_after: int
    quota_overrun: bool


def utc_day_start(timestamp: int) -> int:
    if timestamp < 0:
        raise ValueError("timestamp must be nonnegative")
    return timestamp - timestamp % 86_400


def _insert_quota_row_if_missing(
    session: Session,
    *,
    api_client_id: str,
    scope: str,
    period_start: int,
    now: int,
) -> None:
    values = {
        "id": new_uuid(),
        "api_client_id": api_client_id,
        "scope": scope,
        "period_start": period_start,
        "request_count": 0,
        "token_reserved": 0,
        "token_consumed": 0,
        "version": 1,
        "updated_at": now,
    }
    dialect_name = session.get_bind().dialect.name
    conflict_columns = ["api_client_id", "scope", "period_start"]
    if dialect_name == "postgresql":
        statement = postgresql_insert(ApiClientQuotaUsage).values(**values)
        session.execute(statement.on_conflict_do_nothing(index_elements=conflict_columns))
        return
    if dialect_name == "sqlite":
        statement = sqlite_insert(ApiClientQuotaUsage).values(**values)
        session.execute(statement.on_conflict_do_nothing(index_elements=conflict_columns))
        return
    if (
        session.scalar(
            select(ApiClientQuotaUsage.id).where(
                ApiClientQuotaUsage.api_client_id == api_client_id,
                ApiClientQuotaUsage.scope == scope,
                ApiClientQuotaUsage.period_start == period_start,
            )
        )
        is None
    ):
        session.add(ApiClientQuotaUsage(**values))
        session.flush()


def _locked_quota_rows(
    session: Session,
    *,
    api_client_id: str,
    started_at: int,
    create: bool,
) -> dict[str, ApiClientQuotaUsage]:
    periods = {"daily": utc_day_start(started_at), "lifetime": 0}
    if create:
        for scope in ("daily", "lifetime"):
            _insert_quota_row_if_missing(
                session,
                api_client_id=api_client_id,
                scope=scope,
                period_start=periods[scope],
                now=started_at,
            )
    rows = session.scalars(
        select(ApiClientQuotaUsage)
        .where(
            ApiClientQuotaUsage.api_client_id == api_client_id,
            ((ApiClientQuotaUsage.scope == "daily") & (ApiClientQuotaUsage.period_start == periods["daily"]))
            | ((ApiClientQuotaUsage.scope == "lifetime") & (ApiClientQuotaUsage.period_start == 0)),
        )
        .order_by(ApiClientQuotaUsage.scope)
        .with_for_update()
    ).all()
    by_scope = {row.scope: row for row in rows}
    if set(by_scope) != {"daily", "lifetime"}:
        raise QuotaStateError("quota rows are unavailable")
    return by_scope


def _raise_for_existing_operation(
    operation: ApiCallOperation,
    *,
    request_fingerprint: str,
) -> None:
    if operation.request_fingerprint != request_fingerprint:
        code = "REQUEST_ID_CONFLICT"
    elif operation.status == "pending":
        code = "REQUEST_IN_PROGRESS"
    else:
        code = "REQUEST_ALREADY_COMPLETED"
    raise QuotaAdmissionError(code, operation_id=operation.id)


def _check_quota_limit(
    *,
    row: ApiClientQuotaUsage,
    request_limit: int | None,
    token_limit: int | None,
    reserved_tokens: int,
    request_code: str,
    usage_limit_code: str,
) -> None:
    if request_limit is not None and row.request_count + 1 > request_limit:
        raise QuotaAdmissionError(request_code)
    projected_tokens = row.token_consumed + row.token_reserved + reserved_tokens
    if token_limit is not None and projected_tokens > token_limit:
        raise QuotaAdmissionError(usage_limit_code)
    if projected_tokens > SIGNED_BIGINT_MAX:
        raise QuotaStateError("quota token counter would overflow")


def reserve_operation(
    session: Session,
    *,
    api_client_id: str,
    credential_id: str,
    operation_id: str,
    client_request_id: str,
    request_fingerprint: str,
    model_id: str,
    reserved_tokens: int,
    now: int,
) -> OperationReservation:
    if not REQUEST_ID_RE.fullmatch(client_request_id):
        raise ValueError("client request ID is invalid")
    if not FINGERPRINT_RE.fullmatch(request_fingerprint):
        raise ValueError("request fingerprint is invalid")
    if not model_id or len(model_id) > 255:
        raise ValueError("model ID is invalid")
    if not 1 <= reserved_tokens <= SIGNED_BIGINT_MAX:
        raise ValueError("reserved tokens are invalid")

    client = session.scalar(select(ApiClient).where(ApiClient.id == api_client_id).with_for_update())
    credential = session.scalar(
        select(ApiClientCredential).where(ApiClientCredential.id == credential_id).with_for_update()
    )
    if client is None or credential is None or credential.api_client_id != api_client_id:
        raise QuotaStateError("caller state is unavailable")
    if client.status != "active":
        raise QuotaAdmissionError("CLIENT_DISABLED")
    if credential.status != "active":
        raise QuotaAdmissionError("CREDENTIAL_REVOKED")
    if credential.expires_at is not None and credential.expires_at <= now:
        raise QuotaAdmissionError("CREDENTIAL_EXPIRED")
    if "chat.completions" not in client.allowed_endpoints:
        raise QuotaAdmissionError("ENDPOINT_FORBIDDEN")
    if model_id not in client.allowed_models:
        raise QuotaAdmissionError("MODEL_FORBIDDEN")

    rows = _locked_quota_rows(
        session,
        api_client_id=api_client_id,
        started_at=now,
        create=True,
    )
    existing = session.scalar(
        select(ApiCallOperation)
        .where(
            ApiCallOperation.api_client_id == api_client_id,
            ApiCallOperation.client_request_id == client_request_id,
        )
        .with_for_update()
    )
    if existing is not None:
        _raise_for_existing_operation(existing, request_fingerprint=request_fingerprint)
    if session.get(ApiCallOperation, operation_id) is not None:
        raise QuotaStateError("operation ID already exists")

    daily = rows["daily"]
    lifetime = rows["lifetime"]
    _check_quota_limit(
        row=daily,
        request_limit=client.daily_request_limit,
        token_limit=client.daily_token_limit,
        reserved_tokens=reserved_tokens,
        request_code="DAILY_REQUEST_QUOTA_EXCEEDED",
        usage_limit_code="DAILY_TOKEN_QUOTA_EXCEEDED",
    )
    _check_quota_limit(
        row=lifetime,
        request_limit=client.total_request_limit,
        token_limit=client.total_token_limit,
        reserved_tokens=reserved_tokens,
        request_code="TOTAL_REQUEST_QUOTA_EXCEEDED",
        usage_limit_code="TOTAL_TOKEN_QUOTA_EXCEEDED",
    )

    for row in (daily, lifetime):
        row.request_count += 1
        row.token_reserved += reserved_tokens
        row.version += 1
        row.updated_at = now

    session.add(
        ApiCallOperation(
            id=operation_id,
            api_client_id=api_client_id,
            credential_id=credential_id,
            client_request_id=client_request_id,
            request_fingerprint=request_fingerprint,
            endpoint="chat.completions",
            method="POST",
            model_id=model_id,
            status="pending",
            reserved_tokens=reserved_tokens,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            started_at=now,
            updated_at=now,
        )
    )
    session.flush()
    return OperationReservation(
        operation_id=operation_id,
        daily_requests_after=daily.request_count,
        daily_tokens_reserved_after=daily.token_consumed + daily.token_reserved,
        total_requests_after=lifetime.request_count,
        total_tokens_reserved_after=lifetime.token_consumed + lifetime.token_reserved,
    )


def _pending_operation(session: Session, operation_id: str) -> ApiCallOperation:
    operation = session.scalar(select(ApiCallOperation).where(ApiCallOperation.id == operation_id).with_for_update())
    if operation is None:
        raise QuotaStateError("operation is unavailable")
    if operation.status != "pending":
        raise QuotaStateError("operation is already terminal")
    return operation


def record_credit_reservation(
    session: Session,
    *,
    operation_id: str,
    settlement_id: str,
    usage_operation_id: str,
    now: int,
) -> None:
    operation = _pending_operation(session, operation_id)
    if operation.credit_settlement_id is not None or operation.usage_operation_id is not None:
        if operation.credit_settlement_id == settlement_id and operation.usage_operation_id == usage_operation_id:
            return
        raise QuotaStateError("operation credit reservation conflicts")
    operation.credit_settlement_id = settlement_id
    operation.usage_operation_id = usage_operation_id
    operation.updated_at = now
    session.flush()


def record_provider_usage(
    session: Session,
    *,
    operation_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    now: int,
) -> None:
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("provider usage must be nonnegative")
    total_tokens = prompt_tokens + completion_tokens
    if total_tokens > SIGNED_BIGINT_MAX:
        raise ValueError("provider usage is too large")
    operation = _pending_operation(session, operation_id)
    existing = (
        operation.prompt_tokens,
        operation.completion_tokens,
        operation.total_tokens,
    )
    incoming = (prompt_tokens, completion_tokens, total_tokens)
    if existing != (0, 0, 0) and existing != incoming:
        raise QuotaStateError("provider usage conflicts")
    operation.prompt_tokens = prompt_tokens
    operation.completion_tokens = completion_tokens
    operation.total_tokens = total_tokens
    operation.updated_at = now
    session.flush()


def _terminal_snapshot(
    operation: ApiCallOperation,
    rows: dict[str, ApiClientQuotaUsage],
) -> OperationTerminalSnapshot:
    daily = rows["daily"]
    lifetime = rows["lifetime"]
    return OperationTerminalSnapshot(
        operation_id=operation.id,
        total_tokens=operation.total_tokens,
        daily_requests_after=daily.request_count,
        daily_tokens_after=daily.token_consumed,
        total_requests_after=lifetime.request_count,
        total_tokens_after=lifetime.token_consumed,
        quota_overrun=operation.total_tokens > operation.reserved_tokens,
    )


def finalize_operation(
    session: Session,
    *,
    operation_id: str,
    terminal: OperationTerminal,
) -> OperationTerminalSnapshot:
    operation = session.scalar(select(ApiCallOperation).where(ApiCallOperation.id == operation_id).with_for_update())
    if operation is None:
        raise QuotaStateError("operation is unavailable")
    rows = _locked_quota_rows(
        session,
        api_client_id=operation.api_client_id,
        started_at=operation.started_at,
        create=False,
    )
    if operation.status != "pending":
        event = session.scalar(select(ApiCallEvent).where(ApiCallEvent.operation_id == operation.id))
        if (
            event is None
            or operation.status != terminal.status
            or operation.terminal_http_status != terminal.http_status
            or operation.terminal_code != terminal.business_code
        ):
            raise QuotaStateError("terminal operation evidence conflicts")
        return _terminal_snapshot(operation, rows)
    if terminal.completed_at < operation.started_at:
        raise ValueError("terminal completion precedes operation start")

    for row in rows.values():
        if row.token_reserved < operation.reserved_tokens:
            raise QuotaStateError("quota reservation is inconsistent")
        if row.token_consumed + operation.total_tokens > SIGNED_BIGINT_MAX:
            raise QuotaStateError("quota token counter would overflow")
        row.token_reserved -= operation.reserved_tokens
        row.token_consumed += operation.total_tokens
        row.version += 1
        row.updated_at = terminal.completed_at

    operation.status = terminal.status
    operation.terminal_http_status = terminal.http_status
    operation.terminal_code = terminal.business_code
    operation.completed_at = terminal.completed_at
    operation.updated_at = terminal.completed_at

    daily = rows["daily"]
    lifetime = rows["lifetime"]
    session.add(
        ApiCallEvent(
            id=new_uuid(),
            server_request_id=terminal.server_request_id,
            client_request_id=operation.client_request_id,
            operation_id=operation.id,
            api_client_id=operation.api_client_id,
            credential_id=operation.credential_id,
            endpoint=operation.endpoint,
            method=operation.method,
            model_id=operation.model_id,
            stream=False,
            outcome=terminal.outcome,
            stage=terminal.stage,
            http_status=terminal.http_status,
            business_code=terminal.business_code,
            retryable=terminal.retryable,
            duration_ms=terminal.duration_ms,
            quota_overrun=operation.total_tokens > operation.reserved_tokens,
            prompt_tokens=operation.prompt_tokens,
            completion_tokens=operation.completion_tokens,
            total_tokens=operation.total_tokens,
            charged_micro=terminal.charged_micro,
            qps_observed=terminal.qps_observed,
            concurrency_observed=terminal.concurrency_observed,
            daily_requests_after=daily.request_count,
            daily_tokens_after=daily.token_consumed,
            total_requests_after=lifetime.request_count,
            total_tokens_after=lifetime.token_consumed,
            remote_ip_hash=terminal.remote_ip_hash,
            user_agent_family=terminal.user_agent_family,
            started_at=operation.started_at,
            completed_at=terminal.completed_at,
            created_at=terminal.completed_at,
        )
    )
    session.flush()
    return _terminal_snapshot(operation, rows)
