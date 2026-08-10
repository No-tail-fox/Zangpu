from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from backend.app.security.admin import AdminSessionClaims, AdminSessionError, AdminSessionManager
from backend.app.services.admin import (
    AdminCallerCreateRequest,
    AdminCallerNotFound,
    AdminCallerPatchRequest,
    AdminCallerService,
    AdminCallerStateError,
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
        content={"items": [item.model_dump(mode="json") for item in items], "offset": offset, "limit": limit},
        headers={"Cache-Control": "no-store"},
    )


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
