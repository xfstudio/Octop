"""Project binding and bounded read operations for the integrated workbench."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import Field

from octop.api.deps import current_user
from octop.api.routers.longterm import StrictBody, service
from octop.infra.longterm.project_tools import ProjectToolsService
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User

router = APIRouter()
ComponentId = Literal["beatdesign", "rna"]


class ProjectBindingResponse(StrictBody):
    component_id: ComponentId
    tenant_id: str
    project_id: str | None
    revision: int
    can_manage: bool


class ProjectBindingBody(StrictBody):
    project_id: str | None = Field(max_length=160)
    expected_revision: int = Field(ge=0)


class ProjectReadBody(StrictBody):
    operation: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProjectReadResponse(StrictBody):
    source: str
    operation: str
    project_id: str
    result: dict[str, Any]


@router.get(
    "/tenants/{tenant_id}/integrations/{component_id}/project",
    response_model=ProjectBindingResponse,
    summary="Read the current tenant's bound upstream project",
)
async def binding(
    tenant_id: str,
    component_id: ComponentId,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return await asyncio.to_thread(ProjectToolsService(svc).binding, component_id, tenant_id, user)


@router.put(
    "/tenants/{tenant_id}/integrations/{component_id}/project",
    response_model=ProjectBindingResponse,
    summary="Bind an exclusive upstream project as host and tenant administrator",
    description="Requires both host administrator and tenant owner/admin roles. Uses optimistic revision checks.",
)
async def save_binding(
    tenant_id: str,
    component_id: ComponentId,
    body: ProjectBindingBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        ProjectToolsService(svc).save_binding,
        component_id,
        tenant_id,
        user,
        body.project_id,
        body.expected_revision,
    )


@router.post(
    "/tenants/{tenant_id}/spaces/{space_id}/integrations/{component_id}/read",
    response_model=ProjectReadResponse,
    summary="Read an allowlisted resource in the tenant's bound project",
    description="Checks private-space ownership and tenant membership before and after the call. Does not generate media, review genes, or dispatch tools that mutate upstream data.",
)
async def read(
    tenant_id: str,
    space_id: str,
    component_id: ComponentId,
    body: ProjectReadBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return await ProjectToolsService(svc).read(
        component_id, tenant_id, space_id, user, body.operation, body.arguments
    )
