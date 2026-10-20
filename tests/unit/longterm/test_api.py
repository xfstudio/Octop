"""Real HTTP/JWT/SQL contracts without booting unrelated Octop gateways."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from octop.api.app import _install_exception_handlers
from octop.api.deps import sign_token
from octop.api.middleware import jwt_auth
from octop.api.openapi_meta import configure_openapi
from octop.api.routers.longterm import router
from octop.api.routers.longterm_projects import router as projects_router
from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.longterm import digest
from octop.infra.db.services import SharedServices, build_shared_services
from octop.infra.longterm.execution import compile_workflow
from octop.infra.longterm.service import LongtermService
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout

_BASE = "/api/longterm"


@dataclass
class HttpControlPlane:
    client: httpx.AsyncClient
    service: LongtermService
    shared: SharedServices
    owner_id: int
    foreign_id: int
    owner_auth: dict[str, str]
    foreign_auth: dict[str, str]


@pytest.fixture
async def http_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[HttpControlPlane]:
    # Test outcomes cannot depend on a developer's optional remote integrations.
    for name in ("OCTOP_FACT_REPORTS_ROOT", "OCTOP_TEMPORAL_ADDRESS", "OCTOP_LONGTERM_WORKER"):
        monkeypatch.delenv(name, raising=False)
    paths = PathLayout(tmp_path / "http-control")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    shared = build_shared_services(db=db, paths=paths, config=OctopConfig())
    owner_id = shared.user_repo.create(username="owner", role="user")
    foreign_id = shared.user_repo.create(username="foreign-admin", role="admin")
    manager = UserManager(shared)
    await manager.boot()
    service = LongtermService(shared)
    secret = shared.secret_repo.get_or_create(
        "jwt", lambda: b"synthetic-longterm-http-jwt-secret-32-bytes"
    )
    server = SimpleNamespace(
        _started=True, services=shared, user_manager=manager, longterm_service=service
    )
    app = FastAPI(openapi_url="/api/openapi.json")
    app.state.octop_server = server
    _install_exception_handlers(app)
    app.include_router(router, prefix=_BASE, tags=["longterm"])
    app.include_router(projects_router, prefix=_BASE, tags=["longterm"])
    jwt_auth.install(app, server)
    configure_openapi(app)
    owner_auth = {
        "Authorization": "Bearer "
        + sign_token(secret, sub=owner_id, uname="owner", role="user", ttl_seconds=86400)
    }
    foreign_auth = {
        "Authorization": "Bearer "
        + sign_token(secret, sub=foreign_id, uname="foreign-admin", role="admin", ttl_seconds=86400)
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            yield HttpControlPlane(
                client, service, shared, owner_id, foreign_id, owner_auth, foreign_auth
            )
    finally:
        await service.close()
        await manager.shutdown_all()
        db.close()


async def _space(plane: HttpControlPlane) -> tuple[str, str]:
    tenant = await plane.client.post(
        f"{_BASE}/tenants", headers=plane.owner_auth, json={"name": "HTTP tenant"}
    )
    assert tenant.status_code == 201, tenant.text
    tenant_id = tenant.json()["tenant_id"]
    space = await plane.client.post(
        f"{_BASE}/tenants/{tenant_id}/spaces", headers=plane.owner_auth, json={"name": "Device A"}
    )
    assert space.status_code == 201, space.text
    return tenant_id, space.json()["space_id"]


async def _echo_task(plane: HttpControlPlane, tenant_id: str, space_id: str) -> dict[str, Any]:
    response = await plane.client.post(
        f"{_BASE}/tenants/{tenant_id}/tasks",
        headers=plane.owner_auth,
        json={
            "space_id": space_id,
            "goal": "检查本机调度和证据",
            "idempotency_key": "create-echo-once",
            "workflow": {
                "nodes": [
                    {"id": "first", "skill": "local.echo", "input": {"value": "来自设备的输入"}},
                    {
                        "id": "second",
                        "skill": "local.echo",
                        "depends_on": ["first"],
                        "input": {"value": 2},
                    },
                ]
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_real_jwt_middleware_rejects_missing_or_invalid_credentials(
    http_plane: HttpControlPlane,
) -> None:
    missing = await http_plane.client.get(f"{_BASE}/tenants")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTH_FAILED"
    invalid = await http_plane.client.get(
        f"{_BASE}/tenants", headers={"Authorization": "Bearer invalid-synthetic-token"}
    )
    assert invalid.status_code == 401
    authenticated = await http_plane.client.get(f"{_BASE}/tenants", headers=http_plane.owner_auth)
    assert authenticated.status_code == 200
    assert authenticated.json() == {"tenants": []}


async def test_http_empty_draft_sync_uses_revision_conflicts(http_plane: HttpControlPlane) -> None:
    tenant_id, space_id = await _space(http_plane)
    url = f"{_BASE}/tenants/{tenant_id}/spaces/{space_id}/config"
    draft = {"schema_version": 1, "workspace_name": "尚未编排的草稿", "workflow": {"nodes": []}}
    upload = {"expected_revision": 0, "device_id": "device-a", "config": draft}
    saved = await http_plane.client.put(url, headers=http_plane.owner_auth, json=upload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    stale = await http_plane.client.put(url, headers=http_plane.owner_auth, json=upload)
    assert stale.status_code == 409
    downloaded = await http_plane.client.get(url, headers=http_plane.owner_auth)
    assert downloaded.status_code == 200
    assert downloaded.json()["config"] == draft
    assert downloaded.json()["revision"] == 1
    credentials = await http_plane.client.put(
        url,
        headers=http_plane.owner_auth,
        json={
            **upload,
            "expected_revision": 1,
            "config": {"schema_version": 1, "defaults": {"api_key": "synthetic-must-stay-local"}},
        },
    )
    assert credentials.status_code == 422


async def test_http_async_skill_freeze_binds_prompt_model_and_skill_bytes(
    http_plane: HttpControlPlane,
) -> None:
    tenant_id, space_id = await _space(http_plane)
    http_plane.shared.provider_repo.create(
        name="test-provider", kind="openai", base_url="https://model.example.test/v1"
    )
    http_plane.shared.agent_repo.create(
        agent_id="frozen-agent",
        user_id=http_plane.owner_id,
        name="Freezable",
        default_model="test-provider/model-a",
        system_prompt="原始系统指令",
    )
    reads: list[str] = []
    skill = {"text": "# SKILL\n核实原始来源。"}

    async def read_text(path: str) -> str:
        await asyncio.sleep(0)
        reads.append(path)
        return skill["text"]

    http_plane.service.agent_registry = SimpleNamespace(
        get_agent=lambda _agent_id: SimpleNamespace(workspace=SimpleNamespace(aread_text=read_text))
    )
    response = await http_plane.client.post(
        f"{_BASE}/tenants/{tenant_id}/tasks",
        headers=http_plane.owner_auth,
        json={
            "space_id": space_id,
            "goal": "冻结行为版本",
            "workflow": {
                "nodes": [
                    {
                        "id": "analyze",
                        "agent_id": "frozen-agent",
                        "skill": "verify",
                        "risk": "R0",
                        "max_output_tokens": 512,
                    }
                ]
            },
        },
    )
    assert response.status_code == 201, response.text
    frozen = response.json()
    behavior = frozen["workflow"]["nodes"][0]["behavior_snapshot"]
    assert reads == ["skills/verify/SKILL.md"]
    assert behavior["system_prompt"] == "原始系统指令"
    assert behavior["model_ref"] == "test-provider/model-a"
    assert behavior["skill_text"] == skill["text"]
    assert behavior["skill_digest"] == digest(skill["text"])
    http_plane.shared.agent_repo.update_config(
        "frozen-agent", system_prompt="修改后的指令", default_model="test-provider/model-b"
    )
    skill["text"] = "# SKILL\n修改后的技能。"
    persisted = await http_plane.client.get(
        f"{_BASE}/tenants/{tenant_id}/tasks/{frozen['task_id']}", headers=http_plane.owner_auth
    )
    assert persisted.status_code == 200
    assert persisted.json()["workflow"]["nodes"][0]["behavior_snapshot"] == behavior
    assert compile_workflow(persisted.json())["digest"] == frozen["plan_digest"]


async def test_http_start_poll_echo_and_verify_replayable_evidence(
    http_plane: HttpControlPlane,
) -> None:
    tenant_id, space_id = await _space(http_plane)
    created = await _echo_task(http_plane, tenant_id, space_id)
    assert created["status"] == "draft"
    task_url = f"{_BASE}/tenants/{tenant_id}/tasks/{created['task_id']}"
    command = {
        "command": "start",
        "expected_revision": created["revision"],
        "idempotency_key": "start-once",
    }
    started = await http_plane.client.post(
        task_url + "/commands", headers=http_plane.owner_auth, json=command
    )
    assert started.status_code == 202, started.text
    replayed = await http_plane.client.post(
        task_url + "/commands", headers=http_plane.owner_auth, json=command
    )
    assert replayed.status_code == 202
    assert replayed.json() == started.json()
    async with asyncio.timeout(5):
        while True:
            response = await http_plane.client.get(task_url, headers=http_plane.owner_auth)
            assert response.status_code == 200, response.text
            finished = response.json()
            if finished["status"] in {"completed", "failed", "uncertain"}:
                break
            await asyncio.sleep(0.01)
    assert finished["status"] == "completed", finished
    assert finished["result"]["execution_health"] == "succeeded"
    assert finished["result"]["goal_outcome"] == "inconclusive"
    assert finished["checkpoint"]["node_results"]["first"]["result"]["output"] == {
        "value": "来自设备的输入"
    }
    evidence = await http_plane.client.get(task_url + "/evidence", headers=http_plane.owner_auth)
    assert evidence.status_code == 200
    chain = evidence.json()
    assert chain["valid"] is True
    assert len(chain["head_digest"]) == 64
    assert chain["last_sequence"] >= 8
    kinds = [event["event_type"] for event in chain["events"]]
    assert kinds.count("action_intent") == kinds.count("action_completed") == 2
    cursor = chain["events"][1]["sequence"]
    tail = await http_plane.client.get(
        task_url + f"/evidence?after={cursor}", headers=http_plane.owner_auth
    )
    assert tail.json()["valid"] is True
    assert all(event["sequence"] > cursor for event in tail.json()["events"])


async def test_http_foreign_user_and_same_tenant_private_space_are_isolated(
    http_plane: HttpControlPlane,
) -> None:
    tenant_id, space_id = await _space(http_plane)
    task = await _echo_task(http_plane, tenant_id, space_id)
    task_url = f"{_BASE}/tenants/{tenant_id}/tasks/{task['task_id']}"
    foreign = await http_plane.client.get(task_url, headers=http_plane.foreign_auth)
    assert foreign.status_code == 403
    member = await http_plane.client.put(
        f"{_BASE}/tenants/{tenant_id}/members",
        headers=http_plane.owner_auth,
        json={"user_id": http_plane.foreign_id, "role": "admin"},
    )
    assert member.status_code == 200, member.text
    private = await http_plane.client.get(task_url, headers=http_plane.foreign_auth)
    assert private.status_code == 404
    config = await http_plane.client.get(
        f"{_BASE}/tenants/{tenant_id}/spaces/{space_id}/config", headers=http_plane.foreign_auth
    )
    assert config.status_code == 404
    listings = await http_plane.client.get(
        f"{_BASE}/tenants/{tenant_id}/spaces", headers=http_plane.foreign_auth
    )
    assert listings.json() == {"spaces": []}


@pytest.mark.parametrize(
    "workflow",
    [
        {"nodes": None},
        {"nodes": []},
        {"nodes": [{"id": "x", "skill": "local.echo", "depends_on": ["missing"]}]},
    ],
)
async def test_http_malformed_execution_graph_returns_422(
    http_plane: HttpControlPlane, workflow: dict[str, Any]
) -> None:
    tenant_id, space_id = await _space(http_plane)
    result = await http_plane.client.post(
        f"{_BASE}/tenants/{tenant_id}/tasks",
        headers=http_plane.owner_auth,
        json={"space_id": space_id, "goal": "验证输入", "workflow": workflow},
    )
    assert result.status_code == 422, result.text


async def test_http_openapi_exposes_named_request_and_response_contracts(
    http_plane: HttpControlPlane,
) -> None:
    response = await http_plane.client.get("/api/openapi.json")
    assert response.status_code == 200
    specification = response.json()
    paths = specification["paths"]
    task_path = paths[f"{_BASE}/tenants/{{tenant_id}}/tasks"]["post"]
    assert "freeze" in task_path["summary"].lower()
    assert task_path["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskBody"
    )
    schemas = specification["components"]["schemas"]
    assert {"space_id", "goal", "workflow"}.issubset(schemas["TaskBody"]["required"])
    assert schemas["TaskBody"]["additionalProperties"] is False
    assert "approve" in schemas["CommandBody"]["properties"]["command"]["enum"]
    assert schemas["CommandBody"]["properties"]["node_id"]["anyOf"][0]["maxLength"] == 160
    assert schemas["TaskResponse"]["additionalProperties"] is True
    assert schemas["TaskResponse"]["properties"]["created_at"]["type"] == "integer"
    assert {"task_id", "checkpoint", "result", "plan_digest"}.issubset(
        schemas["TaskResponse"]["required"]
    )
    assert "private" in next(
        tag["description"] for tag in specification["tags"] if tag["name"] == "longterm"
    )
    named_responses = {
        ("/tenants", "get", "200"): "TenantsResponse",
        ("/tenants", "post", "201"): "TenantResponse",
        ("/tenants/{tenant_id}/members", "get", "200"): "MembersResponse",
        ("/tenants/{tenant_id}/spaces", "get", "200"): "SpacesResponse",
        ("/tenants/{tenant_id}/spaces", "post", "201"): "SpaceResponse",
        ("/tenants/{tenant_id}/spaces/{space_id}/config", "put", "200"): "ConfigResponse",
        ("/tenants/{tenant_id}/tasks", "get", "200"): "TasksResponse",
        ("/tenants/{tenant_id}/tasks", "post", "201"): "TaskResponse",
        ("/tenants/{tenant_id}/tasks/{task_id}/commands", "post", "202"): "TaskResponse",
        ("/tenants/{tenant_id}/tasks/{task_id}/evidence", "get", "200"): "EvidenceResponse",
        ("/tenants/{tenant_id}/workflow/import", "post", "200"): "ImportedWorkflowResponse",
    }
    for (path, method, status), name in named_responses.items():
        schema = paths[_BASE + path][method]["responses"][status]["content"]["application/json"][
            "schema"
        ]
        assert schema["$ref"] == "#/components/schemas/" + name
    for path, operations in paths.items():
        if path.startswith(_BASE):
            for method, operation in operations.items():
                if method in {"get", "post", "put"}:
                    assert operation.get("summary"), (path, method)
                    assert operation["tags"] == ["longterm"]
                    assert operation["security"] == [{"BearerAuth": []}]
                    if not path.endswith("/events"):
                        status = "201" if "201" in operation["responses"] else "200"
                        if "202" in operation["responses"]:
                            status = "202"
                        assert (
                            "$ref"
                            in operation["responses"][status]["content"]["application/json"][
                                "schema"
                            ]
                        )


async def test_http_response_models_preserve_dynamic_candidate_and_task_fields(
    http_plane: HttpControlPlane,
) -> None:
    tenant_id, space_id = await _space(http_plane)
    task = await _echo_task(http_plane, tenant_id, space_id)
    task_url = f"{_BASE}/tenants/{tenant_id}/tasks/{task['task_id']}"
    proposed = await http_plane.client.post(
        task_url + "/evolution",
        headers=http_plane.owner_auth,
        json={
            "expected_revision": task["revision"],
            "action": "propose",
            "benchmark_revision": "fixed-suite-v1",
            "proposal": {
                "summary": "验证回读",
                "hypothesis": "先校验可减少遗漏",
                "strategy": "先校验再执行",
                "business_extension": {"version": 2, "review_notes": ["需要独立评估"]},
            },
        },
    )
    assert proposed.status_code == 200, proposed.text
    payload = proposed.json()
    record = payload["record"]
    assert record["proposal"]["business_extension"]["review_notes"] == ["需要独立评估"]
    assert record["candidate_digest"]
    assert payload["task"]["artifacts"]["evolution"][-1] == record
    updated = payload["task"]
    updated["active_behavior_revision"] = "test-extension-version"
    http_plane.service.repo.command(
        updated,
        expected_revision=updated["revision"],
        command_key="test:http-extension",
        request_digest="synthetic-digest",
        command="test",
    )
    fetched = await http_plane.client.get(task_url, headers=http_plane.owner_auth)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["active_behavior_revision"] == "test-extension-version"
    assert fetched.json()["artifacts"]["evolution"][-1] == record


async def test_http_workflow_import_runs_authorized_domain_validation(
    http_plane: HttpControlPlane,
) -> None:
    tenant_id, _space_id = await _space(http_plane)
    endpoint = f"{_BASE}/tenants/{tenant_id}/workflow/import"
    document = {
        "document": {"cards": [{"id": "card-a"}]},
        "octop_execution": {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "review",
                    "card_id": "card-a",
                    "skill": "local.echo",
                    "depends_on": [],
                }
            ],
        },
    }
    imported = await http_plane.client.post(
        endpoint, headers=http_plane.owner_auth, json={"document": document}
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["workflow"]["nodes"][0]["beatdesign_card_id"] == "card-a"
    denied = await http_plane.client.post(
        endpoint, headers=http_plane.foreign_auth, json={"document": document}
    )
    assert denied.status_code == 403
    document["octop_execution"]["max_cycles"] = 33
    invalid_budget = await http_plane.client.post(
        endpoint, headers=http_plane.owner_auth, json={"document": document}
    )
    assert invalid_budget.status_code == 422
    secret = await http_plane.client.post(
        endpoint, headers=http_plane.owner_auth, json={"document": {"api_key": "private"}}
    )
    assert secret.status_code == 422
    oversize = await http_plane.client.post(
        endpoint, headers=http_plane.owner_auth, json={"document": {"notes": "大" * 360000}}
    )
    assert oversize.status_code == 422
    nonfinite = await http_plane.client.post(
        endpoint,
        headers={**http_plane.owner_auth, "Content-Type": "application/json"},
        content=b'{"document":{"unexpected":NaN}}',
    )
    assert nonfinite.status_code == 422
