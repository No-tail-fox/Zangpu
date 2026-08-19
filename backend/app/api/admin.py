from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from backend.app.limits.concurrency import ConcurrencyLimiter
from backend.app.limits.redis import ControlPlaneUnavailable
from backend.app.security.admin import AdminSessionClaims, AdminSessionError, AdminSessionManager
from backend.app.services.admin import (
    AdminCallerCreateRequest,
    AdminCallerNotFound,
    AdminCallerPatchRequest,
    AdminCallerService,
    AdminCallerStateError,
)
from backend.app.services.capacity import AdminCapacityService
from backend.app.services.observability import (
    AdminEventQuery,
    AdminObservabilityLimitError,
    AdminObservabilityService,
)
from backend.app.services.retention import (
    RetentionConfirmationError,
    RetentionNothingToPurge,
    RetentionPurgeRequest,
    RetentionService,
    RetentionSnapshotConflict,
)

ADMIN_SESSION_COOKIE = "zangpu_admin_session"


@dataclass(frozen=True, slots=True)
class AdminApiError(RuntimeError):
    code: str
    status_code: int
    message: str


def admin_error_response(error: AdminApiError) -> JSONResponse:
    request_id = f"adm_{uuid4().hex}"
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message, "request_id": request_id}},
        headers={"Cache-Control": "no-store", "X-Zangpu-Request-Id": request_id},
    )


def _session_manager(request: Request) -> AdminSessionManager:
    return request.app.state.admin_sessions


def require_admin_session(request: Request) -> AdminSessionClaims:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not token:
        raise AdminApiError("ADMIN_AUTH_REQUIRED", 401, "Administrator authentication is required.")
    try:
        return _session_manager(request).verify(token, now=int(time()))
    except AdminSessionError as exc:
        raise AdminApiError("ADMIN_AUTH_REQUIRED", 401, "Administrator authentication is required.") from exc


def require_admin_write(
    request: Request,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
) -> AdminSessionClaims:
    try:
        _session_manager(request).verify_csrf(claims, request.headers.get("x-zangpu-csrf"))
    except AdminSessionError as exc:
        raise AdminApiError("ADMIN_CSRF_FAILED", 403, "Administrator request validation failed.") from exc
    return claims


def _service(request: Request) -> AdminCallerService:
    return request.app.state.admin_callers


def _observability_service(request: Request) -> AdminObservabilityService:
    return request.app.state.admin_observability


def _retention_service(request: Request) -> RetentionService:
    return request.app.state.admin_retention


def _concurrency_limiter(request: Request) -> ConcurrencyLimiter:
    return request.app.state.concurrency_limiter


def _capacity_service(request: Request) -> AdminCapacityService:
    return request.app.state.admin_capacity


def read_admin_event_query(
    api_client_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    created_from: Annotated[int | None, Query(ge=0)] = None,
    created_to: Annotated[int | None, Query(ge=0)] = None,
    outcome: Annotated[str | None, Query(max_length=24)] = None,
    stage: Annotated[str | None, Query(max_length=24)] = None,
    http_status: Annotated[int | None, Query(ge=100, le=599)] = None,
    business_code: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    endpoint: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    model_id: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    stream: bool | None = None,
) -> AdminEventQuery:
    try:
        return AdminEventQuery(
            api_client_id=api_client_id,
            created_from=created_from,
            created_to=created_to,
            outcome=outcome,
            stage=stage,
            http_status=http_status,
            business_code=business_code,
            endpoint=endpoint,
            model_id=model_id,
            stream=stream,
        )
    except ValidationError as exc:
        raise AdminApiError("ADMIN_INVALID_FILTER", 422, "Administrator event filters are invalid.") from exc


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.post("/session")
async def create_admin_session(request: Request) -> JSONResponse:
    supplied = request.headers.get("x-zangpu-admin-token", "")
    try:
        issued = _session_manager(request).issue(supplied, now=int(time()))
    except AdminSessionError as exc:
        raise AdminApiError("ADMIN_AUTH_FAILED", 401, "Administrator authentication failed.") from exc
    response = JSONResponse(
        content={"actor_id": "admin", "expires_at": issued.expires_at, "csrf_token": issued.csrf_token},
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        issued.token,
        max_age=issued.expires_at - int(time()),
        path="/api/v1/admin",
        secure=request.app.state.settings.environment in {"staging", "production"},
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/session")
async def read_admin_session(
    claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
) -> dict[str, str | int]:
    return {
        "actor_id": claims.actor_id,
        "expires_at": claims.expires_at,
        "csrf_token": claims.csrf_token,
    }


@router.post("/session/logout")
async def delete_admin_session(
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
) -> JSONResponse:
    response = JSONResponse(content={"status": "signed_out"}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/api/v1/admin", httponly=True, samesite="strict")
    return response


@router.get("/callers")
async def list_callers(
    request: Request,
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> JSONResponse:
    items = await asyncio.to_thread(_service(request).list_callers, offset=offset, limit=limit)
    return JSONResponse(
        content={
            "items": [item.client.model_dump(mode="json") for item in items],
            "offset": offset,
            "limit": limit,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/capacity/model-pools")
async def get_model_pool_capacity(
    request: Request,
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
) -> JSONResponse:
    try:
        snapshot = await _capacity_service(request).snapshot()
    except ControlPlaneUnavailable as exc:
        raise AdminApiError(
            "ADMIN_CONTROL_PLANE_UNAVAILABLE",
            503,
            "Model capacity is temporarily unavailable.",
        ) from exc
    return JSONResponse(
        content=snapshot.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/events")
async def list_events(
    request: Request,
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
    event_query: Annotated[AdminEventQuery, Depends(read_admin_event_query)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    page = await asyncio.to_thread(
        _observability_service(request).list_events,
        event_query,
        offset=offset,
        limit=limit,
    )
    return JSONResponse(content=page.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/events/summary")
async def summarize_events(
    request: Request,
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
    event_query: Annotated[AdminEventQuery, Depends(read_admin_event_query)],
    bucket_seconds: Annotated[int, Query(ge=1)] = 3_600,
) -> JSONResponse:
    try:
        summary = await asyncio.to_thread(
            _observability_service(request).summarize,
            event_query,
            bucket_seconds=bucket_seconds,
        )
    except AdminObservabilityLimitError as exc:
        raise AdminApiError(
            "ADMIN_OBSERVABILITY_LIMIT",
            422,
            "Event query is too broad; narrow the filters.",
        ) from exc
    except ValueError as exc:
        raise AdminApiError("ADMIN_INVALID_FILTER", 422, "Administrator event filters are invalid.") from exc
    return JSONResponse(content=summary.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/events/export")
async def export_events(
    request: Request,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
    event_query: Annotated[AdminEventQuery, Depends(read_admin_event_query)],
) -> Response:
    try:
        exported = await asyncio.to_thread(
            _observability_service(request).export_csv,
            event_query,
            actor_id=claims.actor_id,
            now=int(time()),
        )
    except AdminObservabilityLimitError as exc:
        raise AdminApiError(
            "ADMIN_OBSERVABILITY_LIMIT",
            422,
            "Event export is too broad; narrow the filters.",
        ) from exc
    return Response(
        content=exported.content,
        media_type="text/csv",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{exported.filename}"',
            "X-Zangpu-Export-Rows": str(exported.row_count),
        },
    )


@router.get("/retention/preview")
async def preview_retention(
    request: Request,
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
) -> JSONResponse:
    preview = await asyncio.to_thread(_retention_service(request).preview, now=int(time()))
    return JSONResponse(content=preview.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/retention/purge")
async def purge_retention(
    request: Request,
    payload: RetentionPurgeRequest,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
) -> JSONResponse:
    try:
        result = await asyncio.to_thread(
            _retention_service(request).purge,
            actor_id=claims.actor_id,
            now=int(time()),
            expected_event_count=payload.expected_event_count,
            expected_audit_count=payload.expected_audit_count,
            confirmed=payload.confirmed,
        )
    except RetentionConfirmationError as exc:
        raise AdminApiError(
            "ADMIN_RETENTION_CONFIRMATION_REQUIRED",
            422,
            "Retention purge requires explicit confirmation.",
        ) from exc
    except RetentionSnapshotConflict as exc:
        raise AdminApiError(
            "ADMIN_RETENTION_SNAPSHOT_CHANGED",
            409,
            "Retention eligibility changed; preview again before purging.",
        ) from exc
    except RetentionNothingToPurge as exc:
        raise AdminApiError("ADMIN_RETENTION_EMPTY", 409, "No records are eligible for retention purge.") from exc
    return JSONResponse(content=result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/callers", status_code=201)
async def create_caller(
    request: Request,
    payload: AdminCallerCreateRequest,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> JSONResponse:
    try:
        result = await asyncio.to_thread(
            _service(request).create_caller,
            payload,
            actor_id=claims.actor_id,
            idempotency_key=idempotency_key,
        )
    except IntegrityError as exc:
        raise AdminApiError("ADMIN_CONFLICT", 409, "Caller name or binding already exists.") from exc
    return JSONResponse(
        status_code=201,
        content={
            "client": result.client.model_dump(mode="json"),
            "credential": result.credential.model_dump(mode="json"),
            "binding": result.binding.model_dump(mode="json"),
            "outbox_id": result.outbox_id,
            "secret": result.take_secret(),
            "display_once": True,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/callers/{client_id}")
async def get_caller(
    request: Request,
    client_id: str,
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
) -> JSONResponse:
    try:
        detail = await asyncio.to_thread(_service(request).get_caller, client_id)
    except AdminCallerNotFound as exc:
        raise AdminApiError("ADMIN_CALLER_NOT_FOUND", 404, "Caller was not found.") from exc
    return JSONResponse(content=detail.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/callers/{client_id}/concurrency")
async def get_caller_concurrency(
    request: Request,
    client_id: str,
    _claims: Annotated[AdminSessionClaims, Depends(require_admin_session)],
) -> JSONResponse:
    try:
        detail = await asyncio.to_thread(_service(request).get_caller, client_id)
    except AdminCallerNotFound as exc:
        raise AdminApiError("ADMIN_CALLER_NOT_FOUND", 404, "Caller was not found.") from exc
    try:
        snapshot = await _concurrency_limiter(request).observe(
            api_client_id=client_id,
            limit=detail.client.concurrency_limit,
        )
    except ControlPlaneUnavailable as exc:
        raise AdminApiError(
            "ADMIN_CONTROL_PLANE_UNAVAILABLE",
            503,
            "Concurrency state is temporarily unavailable.",
        ) from exc
    state = "idle" if snapshot.occupied == 0 else "saturated" if snapshot.saturated else "available"
    return JSONResponse(
        content={
            "api_client_id": client_id,
            "configured_limit": snapshot.limit,
            "occupied": snapshot.occupied,
            "available": snapshot.remaining,
            "state": state,
            "observed_at_ms": snapshot.observed_at_ms,
            "next_lease_expires_at_ms": snapshot.next_lease_expires_at_ms,
            "last_lease_expires_at_ms": snapshot.last_lease_expires_at_ms,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.patch("/callers/{client_id}")
async def update_caller(
    request: Request,
    client_id: str,
    payload: AdminCallerPatchRequest,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
) -> JSONResponse:
    patch = payload.model_dump(exclude={"expected_version"}, exclude_unset=True)
    try:
        detail = await asyncio.to_thread(
            _service(request).update_caller,
            client_id,
            expected_version=payload.expected_version,
            patch=patch,
            actor_id=claims.actor_id,
        )
    except AdminCallerNotFound as exc:
        raise AdminApiError("ADMIN_CALLER_NOT_FOUND", 404, "Caller was not found.") from exc
    except AdminCallerStateError as exc:
        raise AdminApiError("ADMIN_CALLER_CONFLICT", 409, "Caller state has changed.") from exc
    except IntegrityError as exc:
        raise AdminApiError("ADMIN_CONFLICT", 409, "Caller name already exists.") from exc
    return JSONResponse(content=detail.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/callers/{client_id}/credentials/rotate", status_code=201)
async def rotate_caller_credential(
    request: Request,
    client_id: str,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
) -> JSONResponse:
    try:
        result = await asyncio.to_thread(
            _service(request).rotate_credential,
            client_id,
            actor_id=claims.actor_id,
        )
    except AdminCallerNotFound as exc:
        raise AdminApiError("ADMIN_CALLER_NOT_FOUND", 404, "Caller was not found.") from exc
    except AdminCallerStateError as exc:
        raise AdminApiError("ADMIN_CALLER_CONFLICT", 409, "Caller state does not allow credential rotation.") from exc
    return JSONResponse(
        status_code=201,
        content={
            "credential": result.credential.model_dump(mode="json"),
            "secret": result.take_secret(),
            "display_once": True,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/callers/{client_id}/credentials/{credential_id}/revoke")
async def revoke_caller_credential(
    request: Request,
    client_id: str,
    credential_id: str,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
) -> JSONResponse:
    try:
        credential = await asyncio.to_thread(
            _service(request).revoke_credential,
            client_id,
            credential_id,
            actor_id=claims.actor_id,
        )
    except AdminCallerNotFound as exc:
        raise AdminApiError("ADMIN_CREDENTIAL_NOT_FOUND", 404, "Credential was not found.") from exc
    return JSONResponse(
        content=credential.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/callers/{client_id}/disable")
async def disable_caller(
    request: Request,
    client_id: str,
    claims: Annotated[AdminSessionClaims, Depends(require_admin_write)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> JSONResponse:
    try:
        detail = await asyncio.to_thread(
            _service(request).disable_caller,
            client_id,
            actor_id=claims.actor_id,
            idempotency_key=idempotency_key,
        )
    except AdminCallerNotFound as exc:
        raise AdminApiError("ADMIN_CALLER_NOT_FOUND", 404, "Caller was not found.") from exc
    except AdminCallerStateError as exc:
        raise AdminApiError("ADMIN_CALLER_CONFLICT", 409, "Caller cannot be disabled.") from exc
    return JSONResponse(content=detail.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
