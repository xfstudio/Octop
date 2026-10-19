"""Tenant spaces, explicit configuration synchronization and bounded goal execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, RootModel

from octop.api.deps import current_user, get_server
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.execution import WorkflowValidationError
from octop.infra.longterm.service import LongtermService, RevisionConflict
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()


class ResourceResponse(BaseModel):
    """Preserve versioned runtime and upstream artifact extension fields."""

    model_config = ConfigDict(extra="allow")


class IntegrationResponse(ResourceResponse):
    id: str
    name: str
    status: str
    detail: str


class IntegrationsResponse(ResourceResponse):
    integrations: list[IntegrationResponse]


class TenantResponse(ResourceResponse):
    tenant_id: str
    name: str
    role: Literal["owner", "admin", "member", "viewer"]
    revision: int


class TenantsResponse(ResourceResponse):
    tenants: list[TenantResponse]


class MemberResponse(ResourceResponse):
    user_id: int
    role: Literal["owner", "admin", "member", "viewer"]


class MembersResponse(ResourceResponse):
    members: list[MemberResponse]


class MembershipUpdatedResponse(ResourceResponse):
    ok: bool


class SpaceResponse(ResourceResponse):
    tenant_id: str
    space_id: str
    name: str
    revision: int


class SpaceListEntry(SpaceResponse):
    updated_at: int


class SpacesResponse(ResourceResponse):
    spaces: list[SpaceListEntry]


class ConfigResponse(SpaceResponse):
    device_id: str
    updated_at: int
    config: dict[str, Any]


class TaskResponse(ResourceResponse):
    tenant_id: str
    task_id: str
    creator_user_id: int
    space_id: str
    goal: str
    mode: Literal["local", "temporal"]
    workflow: dict[str, Any]
    acceptance: dict[str, Any]
    plan_digest: str
    control: dict[str, Any]
    artifacts: dict[str, Any]
    status: str
    revision: int
    checkpoint: dict[str, Any]
    result: dict[str, Any] | None
    created_at: int
    updated_at: int


class TasksResponse(ResourceResponse):
    tasks: list[TaskResponse]


class EvidenceEventResponse(ResourceResponse):
    tenant_id: str
    task_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    previous_digest: str
    digest: str
    created_at: int


class EvidenceResponse(ResourceResponse):
    events: list[EvidenceEventResponse]
    valid: bool
    head_digest: str
    last_sequence: int
    next_cursor: int


class ImportedWorkflowResponse(ResourceResponse):
    workflow: dict[str, Any]


class ArtifactResponse(ResourceResponse):
    task: TaskResponse
    record: dict[str, Any]


class RnaBlockedResponse(ResourceResponse):
    source: str
    status: Literal["blocked"]
    reason: str


class EvolutionResponse(RootModel[ArtifactResponse | RnaBlockedResponse]):
    """A persisted inert candidate or an explicit unavailable SDK prerequisite."""


class MemoryResponse(ResourceResponse):
    source: str
    bank_id: str
    ttl_enforcement: str


class ProvenanceResponse(ResourceResponse):
    source: str
    records: list[dict[str, Any]]
    count: int
    head_digest: str
    complete: bool


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NamedBody(StrictBody):
    name: str = Field(min_length=1, max_length=120)


class MemberBody(StrictBody):
    user_id: int = Field(gt=0)
    role: Literal["admin", "member", "viewer"] | None = None


class ConfigBody(StrictBody):
    expected_revision: int = Field(ge=0)
    device_id: str = Field(default="", max_length=120)
    config: dict[str, Any]


class TaskBody(StrictBody):
    space_id: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=8000)
    workflow: dict[str, Any]
    mode: Literal["local", "temporal"] = "local"
    max_cycles: int | None = Field(default=None, ge=1, le=32)
    acceptance: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)


class CommandBody(StrictBody):
    command: Literal["start", "pause", "resume", "cancel", "approve", "reject"]
    expected_revision: int = Field(ge=1)
    node_id: str | None = Field(default=None, max_length=160)
    plan_digest: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)


class EvaluationBody(StrictBody):
    expected_revision: int = Field(ge=1)
    run_dir: str = Field(min_length=1, max_length=400)
    benchmark_task_id: str | None = Field(default=None, max_length=200)


class MemoryBody(StrictBody):
    action: Literal["recall", "retain"]
    query: str = Field(default="", max_length=8000)
    content: str = Field(default="", max_length=16000)
    max_tokens: int = Field(default=2048, ge=128, le=4096)
    ttl_seconds: int = Field(default=2592000, ge=60, le=31536000)
    consent: bool = False


class EvolutionBody(StrictBody):
    expected_revision: int = Field(ge=1)
    action: Literal[
        "propose", "rna_propose", "assess", "shadow", "canary", "promoted", "rolled_back"
    ]
    candidate_id: str | None = Field(default=None, max_length=100)
    proposal: dict[str, Any] = Field(default_factory=dict)
    benchmark_revision: str = Field(default="", max_length=200)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class ConsentBody(StrictBody):
    consent: bool = False


class ImportWorkflowBody(StrictBody):
    document: dict[str, Any]


def service(server: OctopServer = Depends(get_server)) -> LongtermService:
    if server.longterm_service is None:
        raise OctopError(ErrorCode.LONGTERM_UNAVAILABLE, "longterm service unavailable")
    return server.longterm_service


@contextmanager
def domain_errors() -> Iterator[None]:
    try:
        yield
    except RevisionConflict as exc:
        raise OctopError(
            ErrorCode.LONGTERM_CONFLICT, "revision conflict", details={"reason": str(exc)}
        ) from exc
    except WorkflowValidationError as exc:
        raise OctopError(
            ErrorCode.LONGTERM_INVALID, "invalid workflow", details={"reason": str(exc)}
        ) from exc


@router.get(
    "/integrations",
    response_model=IntegrationsResponse,
    summary="List integration configuration without disclosing credentials",
)
async def integrations(
    user: User = Depends(current_user), svc: LongtermService = Depends(service)
) -> dict[str, Any]:
    return {"integrations": svc.integrations()}


@router.get(
    "/tenants",
    response_model=TenantsResponse,
    summary="List tenants in which the current user is a member",
)
async def tenants(
    user: User = Depends(current_user), svc: LongtermService = Depends(service)
) -> dict[str, Any]:
    return {"tenants": svc.tenants(user)}


@router.post(
    "/tenants",
    status_code=201,
    response_model=TenantResponse,
    summary="Create a tenant and its owner membership",
)
async def create_tenant(
    body: NamedBody, user: User = Depends(current_user), svc: LongtermService = Depends(service)
) -> dict[str, Any]:
    return svc.create_tenant(user, body.name)


@router.get(
    "/tenants/{tenant_id}/members",
    response_model=MembersResponse,
    summary="List tenant members for a tenant administrator",
)
async def members(
    tenant_id: str, user: User = Depends(current_user), svc: LongtermService = Depends(service)
) -> dict[str, Any]:
    return {"members": svc.members(tenant_id, user)}


@router.put(
    "/tenants/{tenant_id}/members",
    response_model=MembershipUpdatedResponse,
    summary="Assign or revoke an existing Octop user's tenant membership",
)
async def set_member(
    tenant_id: str,
    body: MemberBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    svc.set_member(tenant_id, user, body.user_id, body.role)
    return {"ok": True}


@router.get(
    "/tenants/{tenant_id}/spaces",
    response_model=SpacesResponse,
    summary="List the current user's private configuration spaces",
)
async def spaces(
    tenant_id: str, user: User = Depends(current_user), svc: LongtermService = Depends(service)
) -> dict[str, Any]:
    return {"spaces": svc.spaces(tenant_id, user)}


@router.post(
    "/tenants/{tenant_id}/spaces",
    status_code=201,
    response_model=SpaceResponse,
    summary="Create a private cloud configuration space",
)
async def create_space(
    tenant_id: str,
    body: NamedBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return svc.create_space(tenant_id, user, body.name)


@router.post(
    "/tenants/{tenant_id}/workflow/import",
    response_model=ImportedWorkflowResponse,
    summary="Compile explicitly declared BeatDesign Canvas execution bindings",
)
async def import_workflow(
    tenant_id: str,
    body: ImportWorkflowBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        return svc.import_workflow(tenant_id, user, body.document)


@router.get(
    "/tenants/{tenant_id}/spaces/{space_id}/config",
    response_model=ConfigResponse,
    summary="Download a private configuration snapshot",
)
async def config(
    tenant_id: str,
    space_id: str,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return svc.config(tenant_id, space_id, user)


@router.put(
    "/tenants/{tenant_id}/spaces/{space_id}/config",
    response_model=ConfigResponse,
    summary="Upload selected configuration using optimistic concurrency",
)
async def save_config(
    tenant_id: str,
    space_id: str,
    body: ConfigBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        return svc.save_config(tenant_id, space_id, user, **body.model_dump())


@router.get(
    "/tenants/{tenant_id}/tasks",
    response_model=TasksResponse,
    summary="List the current user's persisted long-running tasks",
)
async def tasks(
    tenant_id: str, user: User = Depends(current_user), svc: LongtermService = Depends(service)
) -> dict[str, Any]:
    return {"tasks": svc.tasks(tenant_id, user)}


@router.post(
    "/tenants/{tenant_id}/tasks",
    status_code=201,
    response_model=TaskResponse,
    summary="Validate and freeze a bounded multi-agent workflow",
)
async def create_task(
    tenant_id: str,
    body: TaskBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        snapshots = await svc.freeze_skills(tenant_id, user, body.workflow)
        return svc.create_task(
            tenant_id, user, body.model_dump(exclude_none=True), skill_snapshots=snapshots
        )


@router.get(
    "/tenants/{tenant_id}/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Read a task's control state, checkpoint and outcomes",
)
async def task(
    tenant_id: str,
    task_id: str,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return svc.task(tenant_id, task_id, user)


@router.post(
    "/tenants/{tenant_id}/tasks/{task_id}/commands",
    status_code=202,
    response_model=TaskResponse,
    summary="Persist an idempotent task command or exact-plan approval",
)
async def command(
    tenant_id: str,
    task_id: str,
    body: CommandBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        return svc.command(tenant_id, task_id, user, body.model_dump(exclude_none=True))


@router.get(
    "/tenants/{tenant_id}/tasks/{task_id}/evidence",
    response_model=EvidenceResponse,
    summary="Read execution evidence and verify its complete hash chain",
)
async def evidence(
    tenant_id: str,
    task_id: str,
    after: int = Query(0, ge=0),
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return svc.evidence(tenant_id, task_id, user, after=after)


@router.get(
    "/tenants/{tenant_id}/tasks/{task_id}/events",
    summary="Stream replayable evidence with Last-Event-ID recovery",
)
async def events(
    tenant_id: str,
    task_id: str,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None),
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> StreamingResponse:
    svc.task(tenant_id, task_id, user)
    try:
        cursor = max(after, int(last_event_id or 0))
    except ValueError as exc:
        raise OctopError(ErrorCode.LONGTERM_INVALID, "invalid event cursor") from exc

    async def stream() -> AsyncIterator[str]:
        current = cursor
        while True:
            task = svc.task(tenant_id, task_id, user)
            rows = svc.repo.events(tenant_id, task_id, current, 100)
            for row in rows:
                current = row["sequence"]
                yield f"id: {current}\nevent: evidence\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
            if (
                task["status"] in {"completed", "failed", "cancelled", "uncertain"}
                and len(rows) < 100
            ):
                return
            if not rows:
                yield ": keepalive\n\n"
                await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/tenants/{tenant_id}/tasks/{task_id}/evaluate",
    response_model=ArtifactResponse,
    summary="Import a real CommerceAgentBench result bundle",
)
async def evaluate(
    tenant_id: str,
    task_id: str,
    body: EvaluationBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        from functools import partial

        return await asyncio.get_running_loop().run_in_executor(
            None, partial(svc.evaluate, tenant_id, task_id, user, **body.model_dump())
        )


@router.post(
    "/tenants/{tenant_id}/tasks/{task_id}/memory",
    response_model=MemoryResponse,
    summary="Recall governed memory or retain a verified summary with consent",
)
async def memory(
    tenant_id: str,
    task_id: str,
    body: MemoryBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        return await svc.memory(tenant_id, task_id, user, body.model_dump())


@router.post(
    "/tenants/{tenant_id}/tasks/{task_id}/evolution",
    response_model=EvolutionResponse,
    summary="Record RNA candidates and apply evidence-based release gates",
)
async def evolution(
    tenant_id: str,
    task_id: str,
    body: EvolutionBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    with domain_errors():
        return await svc.evolve(tenant_id, task_id, user, body.model_dump(exclude_none=True))


@router.post(
    "/tenants/{tenant_id}/tasks/{task_id}/provenance",
    response_model=ProvenanceResponse,
    summary="Export verified execution records through Semantica provenance",
)
async def provenance(
    tenant_id: str,
    task_id: str,
    body: ConsentBody,
    user: User = Depends(current_user),
    svc: LongtermService = Depends(service),
) -> dict[str, Any]:
    return await svc.provenance(tenant_id, task_id, user, consent=body.consent)
