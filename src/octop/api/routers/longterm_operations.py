"""Authenticated private component sessions and explicit result acceptance."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field

from octop.api.deps import current_user
from octop.api.routers.longterm import (
    ComponentOperationResponse,
    ResourceResponse,
    StrictBody,
    domain_errors,
    service,
)
from octop.infra.longterm.operation_sessions import operation_session_catalog
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User

router = APIRouter()
_SCOPE = "/tenants/{tenant_id}/spaces/{space_id}/operation-sessions"


class SessionOperationResponse(ComponentOperationResponse):
    task_required: bool
    node_required: bool


class SessionOperationsResponse(ResourceResponse):
    operations: list[SessionOperationResponse]


class OperationSessionResponse(ResourceResponse):
    session_id: str
    tenant_id: str
    space_id: str
    owner_user_id: int
    component_id: str
    operation: str
    input: dict[str, Any]
    task_id: str | None
    node_id: str | None
    snapshot: dict[str, Any]
    status: Literal["created", "ready", "committed", "cancelled"]
    revision: int
    candidate: dict[str, Any] | None
    result: dict[str, Any] | None
    result_digest: str | None
    source: str | None
    created_at: int
    updated_at: int


class OperationSessionsResponse(ResourceResponse):
    sessions: list[OperationSessionResponse]


class CreateSessionBody(StrictBody):
    component_id: str = Field(min_length=1, max_length=60)
    operation: str = Field(min_length=1, max_length=80)
    input: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    node_id: str | None = Field(default=None, min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)


class SessionCommandBody(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ImportSessionBody(SessionCommandBody):
    result: Any


class CommitSessionBody(SessionCommandBody):
    result_digest: str = Field(min_length=64, max_length=64)


@router.get(
    "/operation-session-operations",
    response_model=SessionOperationsResponse,
    summary="List readback operations for seven integrated components",
)
def operations(user: User = Depends(current_user)) -> dict[str, Any]:
    return {"operations": operation_session_catalog()}


@router.post(
    _SCOPE,
    response_model=OperationSessionResponse,
    status_code=201,
    summary="Create a private operation context with frozen component bindings",
)
def create_session(
    tenant_id: str,
    space_id: str,
    body: CreateSessionBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> Any:
    with domain_errors():
        return svc.operation_sessions.create(tenant_id, space_id, user, body.model_dump())


@router.get(
    _SCOPE,
    response_model=OperationSessionsResponse,
    summary="List the current user's private operation history",
)
def list_sessions(
    tenant_id: str,
    space_id: str,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        return {"sessions": svc.operation_sessions.list(tenant_id, space_id, user)}


@router.get(
    _SCOPE + "/{session_id}",
    response_model=OperationSessionResponse,
    summary="Read an operation session and its accepted result",
)
def get_session(
    tenant_id: str,
    space_id: str,
    session_id: str,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> Any:
    with domain_errors():
        return svc.operation_sessions.get(tenant_id, space_id, user, session_id)


@router.post(
    _SCOPE + "/{session_id}/readback",
    response_model=OperationSessionResponse,
    summary="Read current component output through the frozen authenticated adapter",
    description="Rechecks private scope and configuration before and after the external read. Does not execute task nodes.",
)
async def readback_session(
    tenant_id: str,
    space_id: str,
    session_id: str,
    body: SessionCommandBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> Any:
    with domain_errors():
        return await svc.operation_sessions.readback(
            tenant_id, space_id, user, session_id, body.model_dump()
        )


@router.post(
    _SCOPE + "/{session_id}/import",
    response_model=OperationSessionResponse,
    summary="Preview a bounded manually supplied result with unverified provenance",
)
def import_session(
    tenant_id: str,
    space_id: str,
    session_id: str,
    body: ImportSessionBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> Any:
    with domain_errors():
        return svc.operation_sessions.import_result(
            tenant_id, space_id, user, session_id, body.model_dump()
        )


@router.post(
    _SCOPE + "/{session_id}/commit",
    response_model=OperationSessionResponse,
    summary="Accept exactly the previewed digest as a draft-only result",
    description="Persists the accepted result and optional task evidence without modifying the frozen workflow or its execution state.",
)
def commit_session(
    tenant_id: str,
    space_id: str,
    session_id: str,
    body: CommitSessionBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> Any:
    with domain_errors():
        return svc.operation_sessions.commit(
            tenant_id, space_id, user, session_id, body.model_dump()
        )


@router.post(
    _SCOPE + "/{session_id}/cancel",
    response_model=OperationSessionResponse,
    summary="Cancel an unfinished private operation session",
)
def cancel_session(
    tenant_id: str,
    space_id: str,
    session_id: str,
    body: SessionCommandBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> Any:
    with domain_errors():
        return svc.operation_sessions.cancel(
            tenant_id, space_id, user, session_id, body.model_dump()
        )
