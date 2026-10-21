"""Component nodes execute bounded upstream contracts with frozen tenant scopes."""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from octop.infra.db.repos.longterm import digest
from octop.infra.errors import OctopError
from octop.infra.longterm.adapters import (
    BeatDesignAdapter,
    HindsightAdapter,
    RnaAdapter,
    SemanticaAdapter,
    tenant_namespace,
)
from octop.infra.longterm.component_calls import (
    execute_component_call,
    freeze_component_call,
    operation_catalog,
    validate_component_call,
)
from octop.infra.longterm.execution import WorkflowValidationError, compile_workflow
from octop.infra.longterm.project_tools import ProjectToolsService
from octop.infra.utils.ulid import new_ulid
from tests.unit.longterm.test_evaluation import _bundle
from tests.unit.longterm.test_service import platform as platform


def _node(component: str = "rna", operation: str = "genes", **changes: Any) -> dict[str, Any]:
    return {
        "id": "component",
        "skill": "component.call",
        "component_id": component,
        "operation": operation,
        "risk": "R1" if component == "semantica" else "R0",
        "depends_on": [],
        "input": {},
        **changes,
    }


def _bind(platform: Any, component: str) -> None:
    service, user, admin, tenant, _ = platform
    service.set_member(tenant, user, admin.id, "admin")
    ProjectToolsService(service).save_binding(component, tenant, admin, "project-a", 0)


def _task(platform: Any, node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    service, user, _, tenant, space = platform
    frozen = copy.deepcopy(node)
    frozen["component_snapshot"] = freeze_component_call(service, tenant, user, frozen)
    task = {
        "tenant_id": tenant,
        "task_id": new_ulid(),
        "space_id": space,
        "creator_user_id": user.id,
        "goal": "验证组件调用",
        "mode": "local",
        "workflow": {"nodes": [frozen]},
        "acceptance": {},
        "control": {},
        "artifacts": {},
    }
    task["plan_digest"] = compile_workflow(task)["digest"]
    return frozen, service.repo.create_task(task, new_ulid(), digest(task))


def _configured(platform: Any, monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    manager = platform[0].components
    resolve = manager.resolve
    monkeypatch.setattr(manager, "resolve", lambda component: {**resolve(component), **values})


def test_catalog_is_closed_and_returns_independent_examples() -> None:
    catalog = operation_catalog()
    assert len(catalog) == 13
    assert {row["component_id"] for row in catalog} == {
        "beatdesign",
        "rna",
        "hindsight",
        "commerceagentbench",
        "semantica",
    }
    for item in catalog:
        validate_component_call(
            _node(item["component_id"], item["operation"], input=item["input_example"])
        )
        assert item["risk"] == ("R1" if item["component_id"] == "semantica" else "R0")
    catalog[-1]["input_example"]["consent"] = False
    assert operation_catalog()[-1]["input_example"] == {"consent": True}


@pytest.mark.parametrize(
    "changes",
    [
        {"skill": "agent.analyze"},
        {"component_id": "unknown"},
        {"component_id": []},
        {"operation": "review_gene"},
        {"operation": "bdesign_generation_submit"},
        {"risk": "R1"},
        {"agent_id": None},
        {"behavior_snapshot": {}},
        {"url": "http://arbitrary.test"},
        {"tool": "shell"},
        {"input": []},
        {"input": {"projectId": "other"}},
        {"input": {"command": "run"}},
    ],
)
def test_node_cannot_select_arbitrary_tools_endpoints_or_agent_harnesses(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(WorkflowValidationError):
        validate_component_call(_node(**changes))


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"consent": False},
        {"consent": 1},
        {"consent": "true"},
        {"consent": {"$node": "seed", "pointer": "/output"}},
        {"consent": True, "storage_root": "private"},
    ],
)
def test_indexing_requires_literal_consent_and_cannot_be_downgraded(data: dict[str, Any]) -> None:
    with pytest.raises(WorkflowValidationError):
        validate_component_call(_node("semantica", "index", input=data))
    with pytest.raises(WorkflowValidationError):
        validate_component_call(_node("semantica", "index", input={"consent": True}, risk="R0"))


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"query": ""},
        {"query": "x", "max_tokens": True},
        {"query": "x", "max_tokens": 127},
        {"query": "x", "max_tokens": 4097},
        {"query": "x", "base_url": "http://arbitrary.test"},
        {"query": {"$node": "undeclared", "pointer": "/result"}},
        {"query": {"$node": "seed", "pointer": "result"}},
        {"query": {"$node": "seed", "pointer": "/result", "fallback": "x"}},
    ],
)
def test_memory_arguments_and_dependency_references_are_bounded(data: dict[str, Any]) -> None:
    with pytest.raises(WorkflowValidationError):
        validate_component_call(_node("hindsight", "recall", depends_on=["seed"], input=data))


@pytest.mark.parametrize(
    "run",
    ["../foreign", "/results/foreign", "C:/foreign", "a/../foreign", "a\\foreign", ".", "a//b"],
)
def test_report_paths_cannot_escape_private_result_scope(run: str) -> None:
    with pytest.raises(WorkflowValidationError):
        validate_component_call(_node("commerceagentbench", "report", input={"run_dir": run}))


def test_valid_input_references_are_admitted_before_runtime_resolution() -> None:
    for component, operation, key in (
        ("hindsight", "recall", "query"),
        ("commerceagentbench", "report", "run_dir"),
    ):
        validate_component_call(
            _node(
                component,
                operation,
                depends_on=["seed"],
                input={
                    key: {"$node": "seed", "pointer": "/result/output/query"},
                },
            )
        )
    validate_component_call(
        _node("hindsight", "recall", depends_on=["seed"], input={"query": {"$node": "seed"}})
    )
    with pytest.raises(WorkflowValidationError):
        validate_component_call(
            _node(
                "hindsight",
                "recall",
                depends_on=["seed"],
                input={"query": {"$node": "seed", "pointer": "/bad~escape"}},
            )
        )


def test_freeze_rejects_client_snapshots_and_requires_an_existing_binding(platform: Any) -> None:
    service, user, stranger, tenant, _ = platform
    node = _node()
    with pytest.raises(WorkflowValidationError, match="binding required"):
        freeze_component_call(service, tenant, user, node)
    _bind(platform, "rna")
    snapshot = freeze_component_call(service, tenant, user, node)
    assert snapshot == {
        "version": 1,
        "component_id": "rna",
        "operation": "genes",
        "project_id": "project-a",
        "binding_revision": 1,
        "deployment_digest": snapshot["deployment_digest"],
    }
    assert len(snapshot["deployment_digest"]) == 64
    with pytest.raises(WorkflowValidationError, match="server generated"):
        freeze_component_call(service, tenant, user, {**node, "component_snapshot": snapshot})
    other_tenant = service.create_tenant(stranger, "other")["tenant_id"]
    with pytest.raises(OctopError) as error:
        freeze_component_call(service, other_tenant, user, node)
    assert error.value.status == 403


@pytest.mark.parametrize(
    "component,operation", [("beatdesign", "canvas"), ("rna", "genes"), ("rna", "observability")]
)
async def test_project_calls_use_only_the_frozen_project_and_zero_new_model_usage(
    platform: Any,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    operation: str,
) -> None:
    _bind(platform, component)
    _configured(
        platform, monkeypatch, endpoint="http://local.test/mcp", base_url="http://local.test"
    )
    node, task = _task(platform, _node(component, operation))
    calls: list[tuple[str, str]] = []

    async def beat(
        self: BeatDesignAdapter, tenant: str, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert self.tenant_projects == {tenant: "project-a"}
        assert args == {}
        assert self._authorization_check is not None
        self._authorization_check()
        calls.append((tenant, tool))
        return {"cards": []}

    async def rna(self: RnaAdapter, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        assert method == "GET" and payload is None
        assert self._authorization_check is not None
        self._authorization_check()
        calls.append((method, path))
        return {"genes": []}

    monkeypatch.setattr(BeatDesignAdapter, "call_tool", beat)
    monkeypatch.setattr(RnaAdapter, "_request", rna)
    result = await execute_component_call(platform[0], node, task)
    assert result["component_id"] == component and result["operation"] == operation
    assert result["usage"] == {"total_tokens": 0}
    assert result["output"] in ({"cards": []}, {"genes": []})
    expected = (
        "bdesign_canvas_get"
        if component == "beatdesign"
        else "/api/v2/projects/project-a/genes"
        + ("/observability" if operation == "observability" else "")
    )
    assert calls[0][1] == expected


async def test_binding_changed_after_freeze_prevents_dispatch(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(platform, "rna")
    service, _, admin, tenant, _ = platform
    node, task = _task(platform, _node())
    ProjectToolsService(service).save_binding("rna", tenant, admin, "project-b", 1)
    calls: list[str] = []

    async def genes(*_: Any) -> dict[str, Any]:
        calls.append("sent")
        return {}

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    with pytest.raises(OctopError) as error:
        await execute_component_call(service, node, task)
    assert error.value.status == 409 and calls == []


@pytest.mark.parametrize("when", ["before", "during"])
async def test_changed_deployment_address_invalidates_the_frozen_call(
    platform: Any,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    _bind(platform, "rna")
    service = platform[0]
    original_resolve = service.components.resolve
    settings = {"base_url": "http://original.test"}
    monkeypatch.setattr(
        service.components, "resolve", lambda component: {**original_resolve(component), **settings}
    )
    node, task = _task(platform, _node())
    assert "original.test" not in json.dumps(node["component_snapshot"])
    calls: list[str] = []
    if when == "before":
        settings["base_url"] = "http://changed.test"

    async def genes(self: RnaAdapter, _: str) -> dict[str, Any]:
        calls.append(self.base_url)
        settings["base_url"] = "http://changed.test"
        return {"genes": []}

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    with pytest.raises(OctopError) as error:
        await execute_component_call(service, node, task)
    assert error.value.status == 409
    assert calls == ([] if when == "before" else ["http://original.test"])


async def test_credentials_rotate_without_leaking_into_the_frozen_target(
    platform: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(platform, "rna")
    service = platform[0]
    original_resolve = service.components.resolve
    settings = {"base_url": "http://original.test", "token": "original-private-credential"}
    monkeypatch.setattr(
        service.components, "resolve", lambda component: {**original_resolve(component), **settings}
    )
    node, task = _task(platform, _node())
    settings["token"] = "rotated-private-credential"

    async def genes(self: RnaAdapter, _: str) -> dict[str, Any]:
        assert self.token == "rotated-private-credential"
        return {"genes": []}

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    result = await execute_component_call(service, node, task)
    assert result["output"] == {"genes": []}
    assert "credential" not in json.dumps(node["component_snapshot"])


async def test_binding_changed_during_mcp_initialize_prevents_tool_dispatch(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(platform, "beatdesign")
    service, _, admin, tenant, _ = platform
    _configured(platform, monkeypatch, endpoint="http://local.test/mcp")
    node, task = _task(platform, _node("beatdesign", "canvas"))
    calls: list[str] = []

    class Session:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def initialize(self) -> None:
            ProjectToolsService(service).save_binding("beatdesign", tenant, admin, "project-b", 1)

        async def call_tool(self, *_: Any, **__: Any) -> dict[str, Any]:
            calls.append("sent")
            return {}

    @asynccontextmanager
    async def transport(*_: Any, **__: Any) -> AsyncIterator[tuple[None, None, None]]:
        yield None, None, None

    mcp, http_transport = ModuleType("mcp"), ModuleType("mcp.client.streamable_http")
    mcp.ClientSession = Session  # type: ignore[attr-defined]
    http_transport.streamablehttp_client = transport  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", http_transport)
    with pytest.raises(OctopError) as error:
        await execute_component_call(service, node, task)
    assert error.value.status == 409 and calls == []


@pytest.mark.parametrize("revocation", ["membership", "disabled", "component", "space"])
async def test_live_authority_is_checked_after_the_external_read(
    platform: Any,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    _bind(platform, "rna")
    service, user, _, tenant, space = platform
    _configured(platform, monkeypatch, base_url="http://local.test")
    node, task = _task(platform, _node())

    async def genes(*_: Any) -> dict[str, Any]:
        if revocation == "membership":
            with service.services.db.transaction() as connection:
                connection.execute(
                    "DELETE FROM bm_memberships WHERE tenant_id=? AND user_id=?", (tenant, user.id)
                )
        elif revocation == "disabled":
            with service.services.db.transaction() as connection:
                connection.execute("UPDATE users SET disabled=1 WHERE id=?", (user.id,))
        elif revocation == "component":
            service.services.settings_repo.set(
                "longterm.component.rna", json.dumps({"enabled": False})
            )
        else:
            with service.services.db.transaction() as connection:
                connection.execute(
                    "UPDATE bm_spaces SET owner_user_id=? WHERE space_id=?", (platform[2].id, space)
                )
        return {"genes": [{"text": "private result"}]}

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    with pytest.raises(OctopError):
        await execute_component_call(service, node, task)


@pytest.mark.parametrize(
    "output",
    [
        {"access_token": "private"},
        {"result": "Bearer private"},
        {"result": "configured-private-token"},
        {"result": "x" * (2 * 1024 * 1024)},
    ],
)
async def test_sensitive_or_oversized_results_are_not_returned(
    platform: Any,
    monkeypatch: pytest.MonkeyPatch,
    output: dict[str, Any],
) -> None:
    _bind(platform, "rna")
    _configured(
        platform, monkeypatch, base_url="http://local.test", token="configured-private-token"
    )
    node, task = _task(platform, _node())

    async def genes(*_: Any) -> dict[str, Any]:
        return output

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    with pytest.raises((OctopError, WorkflowValidationError)):
        await execute_component_call(platform[0], node, task)


async def test_upstream_errors_do_not_expose_private_bodies(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(platform, "rna")
    _configured(platform, monkeypatch, base_url="http://local.test")
    node, task = _task(platform, _node())

    async def genes(*_: Any) -> dict[str, Any]:
        raise RuntimeError("private server body and token")

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    with pytest.raises(OctopError) as error:
        await execute_component_call(platform[0], node, task)
    assert error.value.status == 503
    assert "private server body" not in str(error.value)


async def test_memory_call_uses_existing_private_space_scoping(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, tenant, space = platform
    _configured(platform, monkeypatch, base_url="http://local.test")
    node, task = _task(platform, _node("hindsight", "recall", input={"query": "依赖结果"}))
    called: list[Any] = []

    async def memory(
        self: HindsightAdapter, *, tenant_id: str, query: str, max_tokens: int
    ) -> dict[str, Any]:
        assert self._authorization_check is not None
        self._authorization_check()
        called.append((tenant_id, query, max_tokens))
        return {"bank_id": "octop-" + tenant_namespace(tenant + ":space:" + space), "results": []}

    monkeypatch.setattr(HindsightAdapter, "recall", memory)
    result = await execute_component_call(service, node, task)
    assert called == [(tenant + ":space:" + space, "依赖结果", 2048)]
    assert result["output"]["results"] == []
    assert result["usage"] == {"total_tokens": 0}


async def test_hindsight_rechecks_the_target_before_issuing_the_sdk_request(
    platform: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = platform[0]
    resolve = service.components.resolve
    settings = {"base_url": "http://original.test"}
    monkeypatch.setattr(
        service.components, "resolve", lambda component: {**resolve(component), **settings}
    )
    node, task = _task(platform, _node("hindsight", "recall", input={"query": "经验"}))
    calls: list[str] = []

    class Client:
        async def arecall(self, **_: Any) -> dict[str, Any]:
            calls.append("requested")
            return {"results": []}

        async def aclose(self) -> None:
            pass

    def get_client(adapter: HindsightAdapter) -> Client:
        settings["base_url"] = "http://changed.test"
        return Client()

    monkeypatch.setattr(HindsightAdapter, "_get_client", get_client)
    with pytest.raises(OctopError) as error:
        await execute_component_call(service, node, task)
    assert error.value.status == 409 and calls == []


async def test_unresolved_references_are_never_sent_to_upstreams(platform: Any) -> None:
    service, user, _, tenant, _ = platform
    node = _node(
        "hindsight",
        "recall",
        depends_on=["seed"],
        input={"query": {"$node": "seed", "pointer": "/result/output/query"}},
    )
    node["component_snapshot"] = freeze_component_call(service, tenant, user, node)
    with pytest.raises(WorkflowValidationError, match="must be resolved"):
        await execute_component_call(service, node, {})


async def test_report_reads_only_recorded_private_results_without_mutating_task_revision(
    platform: Any,
) -> None:
    service, user, _, tenant, space = platform
    node, task = _task(
        platform,
        _node(
            "commerceagentbench",
            "report",
            input={"run_dir": "run-1", "expected_task_id": "commerce-task"},
        ),
    )
    root = service.components.directory("commerceagentbench", "results_subdirectory")
    _bundle(root / tenant_namespace(tenant) / space)
    result = await execute_component_call(service, node, task)
    assert result["source"] == "CommerceAgentBench"
    assert result["output"]["score"] == 1.0
    assert len(result["output"]["provenance"]) == 3
    assert result["usage"] == {"total_tokens": 0}
    assert service.task(tenant, task["task_id"], user)["revision"] == task["revision"]
    with pytest.raises(OctopError):
        await execute_component_call(service, node, {**task, "space_id": "foreign-space"})


async def test_missing_private_report_cannot_read_a_different_space(platform: Any) -> None:
    service, user, _, tenant, _ = platform
    node, task = _task(platform, _node("commerceagentbench", "report", input={"run_dir": "run-1"}))
    other_space = service.create_space(tenant, user, "other")["space_id"]
    root = service.components.directory("commerceagentbench", "results_subdirectory")
    _bundle(root / tenant_namespace(tenant) / other_space)
    with pytest.raises(WorkflowValidationError, match="private report directory unavailable"):
        await execute_component_call(service, node, task)


async def test_private_report_root_cannot_be_a_symlink_to_another_space(platform: Any) -> None:
    service, user, _, tenant, space = platform
    node, task = _task(platform, _node("commerceagentbench", "report", input={"run_dir": "run-1"}))
    other_space = service.create_space(tenant, user, "other")["space_id"]
    root = service.components.directory("commerceagentbench", "results_subdirectory")
    parent = root / tenant_namespace(tenant)
    _bundle(parent / other_space)
    try:
        (parent / space).symlink_to(parent / other_space, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks is unavailable on this platform")
    with pytest.raises(WorkflowValidationError, match="private report directory unavailable"):
        await execute_component_call(service, node, task)


async def test_indexing_requires_live_approval_and_exports_the_real_evidence_cut(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, user, _, tenant, _ = platform
    node, task = _task(platform, _node("semantica", "index", input={"consent": True}))
    task["execution"] = {"plan_digest": task["plan_digest"], "cycle": 1}
    with pytest.raises(OctopError) as error:
        await execute_component_call(service, node, task)
    assert error.value.status == 403
    task["control"] = {
        "approvals": [
            {
                "tenant_id": tenant,
                "task_id": task["task_id"],
                "node_id": node["id"],
                "plan_digest": task["plan_digest"],
                "cycle": 1,
                "scope": "local",
                "approved": True,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        ]
    }
    task = service.repo.command(
        task,
        expected_revision=1,
        command_key=new_ulid(),
        request_digest=digest(task),
        command="approve",
        actor_user_id=user.id,
    )
    records: list[str] = []

    async def record(
        self: SemanticaAdapter, event: dict[str, Any], *, evidence_ref: str
    ) -> dict[str, Any]:
        assert self._authorization_check is not None
        self._authorization_check()
        assert event["tenant_id"] == tenant and event["task_id"] == task["task_id"]
        records.append(evidence_ref)
        return {"source": "semantica", "indexed": True, "evidence_ref": evidence_ref}

    monkeypatch.setattr(SemanticaAdapter, "record", record)
    before = service.evidence(tenant, task["task_id"], user)
    result = await execute_component_call(service, node, task)
    assert result["output"]["head_digest"] == before["head_digest"]
    assert result["output"]["complete"] is True
    assert len(records) == before["last_sequence"]
    assert service.evidence(tenant, task["task_id"], user)["valid"] is True
    assert result["usage"] == {"total_tokens": 0}


@pytest.mark.parametrize(
    "snapshot", [None, {"version": True}, {"version": 2}, {"version": 1, "project_id": "other"}]
)
def test_forged_or_malformed_snapshots_are_rejected(snapshot: Any) -> None:
    with pytest.raises(WorkflowValidationError):
        validate_component_call(_node(component_snapshot=snapshot))
