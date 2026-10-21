"""Private operation sessions persist bounded readbacks without advancing tasks."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.db.migrate import _discover, run_migrations
from octop.infra.db.repos.longterm import canonical, digest
from octop.infra.errors import OctopError
from octop.infra.longterm.adapters import (
    BeatDesignAdapter,
    HindsightAdapter,
    RnaAdapter,
    tenant_namespace,
)
from octop.infra.longterm.operation_sessions import (
    OperationSessionService,
    operation_session_catalog,
)
from octop.infra.longterm.project_tools import ProjectToolsService
from octop.infra.longterm.temporal_runtime import workflow_id
from tests.unit.longterm.test_evaluation import _bundle
from tests.unit.longterm.test_service import platform as platform
from tests.unit.longterm.test_service import task_body


def _create(
    platform: Any, component: str = "octop", operation: str = "task", **changes: Any
) -> tuple[OperationSessionService, dict[str, Any], dict[str, Any]]:
    service, user, _, tenant, space = platform
    task = service.create_task(tenant, user, task_body(space))
    sessions = OperationSessionService(service)
    body = {
        "component_id": component,
        "operation": operation,
        "input": {},
        "task_id": task["task_id"],
        "idempotency_key": "open-one",
        **changes,
    }
    return sessions, sessions.create(tenant, space, user, body), task


def _action(session: dict[str, Any], key: str, **values: Any) -> dict[str, Any]:
    return {"expected_revision": session["revision"], "idempotency_key": key, **values}


def _bind(platform: Any, component: str) -> None:
    service, user, admin, tenant, _ = platform
    service.set_member(tenant, user, admin.id, "admin")
    ProjectToolsService(service).save_binding(component, tenant, admin, "project-a", 0)


def test_catalog_covers_seven_components_without_write_operations() -> None:
    catalog = operation_session_catalog()
    assert {item["component_id"] for item in catalog} == {
        "octop",
        "beatdesign",
        "rna",
        "hindsight",
        "semantica",
        "commerceagentbench",
        "temporal",
    }
    assert all(item["risk"] == "R0" for item in catalog)
    assert not any(
        item["operation"] in {"index", "retain", "publish", "generate"} for item in catalog
    )
    assert next(item for item in catalog if item["operation"] == "node_result")["node_required"]


def test_manual_import_commit_is_durable_idempotent_and_does_not_advance_task(
    platform: Any,
) -> None:
    service, user, _, tenant, space = platform
    sessions, session, task = _create(platform)
    assert session["status"] == "created" and session["revision"] == 1
    body = _action(session, "manual-one", result={"message": "外部结果", "verified": True})
    ready = sessions.import_result(tenant, space, user, session["session_id"], body)
    assert ready["candidate"]["source"] == "manual_unverified"
    assert ready["candidate"]["verified"] is False
    assert ready["candidate"]["promotion_allowed"] is False
    assert ready["candidate"]["draft_only"] is True
    assert sessions.import_result(tenant, space, user, session["session_id"], body) == ready
    assert ready["result_digest"] == digest(ready["candidate"])
    with pytest.raises(OctopError) as error:
        sessions.commit(
            tenant,
            space,
            user,
            session["session_id"],
            _action(ready, "wrong-digest", result_digest="0" * 64),
        )
    assert error.value.status == 409
    command = _action(ready, "commit-one", result_digest=ready["result_digest"])
    committed = sessions.commit(tenant, space, user, session["session_id"], command)
    assert committed["status"] == "committed" and committed["revision"] == 3
    assert committed["candidate"] is None and committed["result"]["verified"] is False
    assert sessions.commit(tenant, space, user, session["session_id"], command) == committed
    assert (
        OperationSessionService(service).get(tenant, space, user, session["session_id"])
        == committed
    )
    assert sessions.list(tenant, space, user) == [committed]
    assert service.task(tenant, task["task_id"], user)["revision"] == task["revision"]
    evidence = service.evidence(tenant, task["task_id"], user)
    assert evidence["valid"]
    events = [
        event for event in evidence["events"] if event["event_type"] == "component.result.committed"
    ]
    assert len(events) == 1 and events[0]["payload"]["verified"] is False
    assert events[0]["payload"]["result_digest"] == ready["result_digest"]
    with pytest.raises(OctopError) as error:
        sessions.import_result(
            tenant, space, user, session["session_id"], _action(committed, "late-import", result={})
        )
    assert error.value.status == 409


def test_commit_and_task_event_rollback_together(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, user, _, tenant, space = platform
    sessions, session, task = _create(platform)
    ready = sessions.import_result(
        tenant, space, user, session["session_id"], _action(session, "import", result={"output": 1})
    )
    before = service.evidence(tenant, task["task_id"], user)

    def fail(*_: Any) -> None:
        raise RuntimeError("injected evidence write failure")

    monkeypatch.setattr(sessions.repo._tasks, "_append", fail)
    with pytest.raises(RuntimeError, match="injected"):
        sessions.commit(
            tenant,
            space,
            user,
            session["session_id"],
            _action(ready, "commit", result_digest=ready["result_digest"]),
        )
    assert sessions.get(tenant, space, user, session["session_id"]) == ready
    assert service.evidence(tenant, task["task_id"], user) == before


def test_create_idempotency_and_private_space_authority(platform: Any) -> None:
    service, user, admin, tenant, space = platform
    sessions, session, task = _create(platform)
    payload = {
        "component_id": "octop",
        "operation": "task",
        "input": {},
        "task_id": task["task_id"],
        "idempotency_key": "open-one",
    }
    assert sessions.create(tenant, space, user, payload) == session
    with pytest.raises(OctopError) as error:
        sessions.create(tenant, space, user, {**payload, "node_id": "observe"})
    assert error.value.status == 409
    service.set_member(tenant, user, admin.id, "admin")
    for action in (
        lambda: sessions.get(tenant, space, admin, session["session_id"]),
        lambda: sessions.list(tenant, space, admin),
        lambda: sessions.create(tenant, space, admin, payload),
    ):
        with pytest.raises(OctopError) as error:
            action()
        assert error.value.status == 404
    private = service.create_space(tenant, admin, "管理员私有空间")["space_id"]
    with pytest.raises(OctopError) as error:
        sessions.get(tenant, private, admin, session["session_id"])
    assert error.value.status == 404


@pytest.mark.parametrize("case", ["disabled", "membership", "viewer"])
def test_live_authority_is_rechecked_for_replay_and_import(platform: Any, case: str) -> None:
    service, user, _, tenant, space = platform
    sessions, session, _ = _create(platform)
    body = _action(session, "manual", result={"value": 1})
    sessions.import_result(tenant, space, user, session["session_id"], body)
    with service.services.db.transaction() as conn:
        if case == "disabled":
            conn.execute("UPDATE users SET disabled=1 WHERE id=?", (user.id,))
        elif case == "membership":
            conn.execute(
                "DELETE FROM bm_memberships WHERE tenant_id=? AND user_id=?", (tenant, user.id)
            )
        else:
            conn.execute(
                "UPDATE bm_memberships SET role='viewer' WHERE tenant_id=? AND user_id=?",
                (tenant, user.id),
            )
    with pytest.raises(OctopError) as error:
        sessions.import_result(tenant, space, user, session["session_id"], body)
    assert error.value.status == 403


@pytest.mark.parametrize(
    "data",
    [
        {"password": "x"},
        {"message": "Bearer private-test-value"},
        {"n": float("nan")},
        {"large": "x" * (2 * 1024 * 1024)},
    ],
)
def test_import_rejects_credentials_nonfinite_and_oversized_data(platform: Any, data: Any) -> None:
    _, user, _, tenant, space = platform
    sessions, session, _ = _create(platform)
    with pytest.raises(OctopError) as error:
        sessions.import_result(
            tenant, space, user, session["session_id"], _action(session, "bad", result=data)
        )
    assert error.value.status == 422
    assert sessions.get(tenant, space, user, session["session_id"])["status"] == "created"


def test_depth_known_secret_and_unsupported_operations_are_rejected(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, user, _, tenant, space = platform
    sessions, session, _ = _create(platform)
    data: Any = 1
    for _ in range(26):
        data = [data]
    with pytest.raises(OctopError):
        sessions.import_result(
            tenant, space, user, session["session_id"], _action(session, "deep", result=data)
        )
    monkeypatch.setenv("OCTOP_HINDSIGHT_API_KEY", "synthetic-known-credential")
    with pytest.raises(OctopError):
        sessions.import_result(
            tenant,
            space,
            user,
            session["session_id"],
            _action(session, "secret", result={"value": "synthetic-known-credential"}),
        )
    with pytest.raises(OctopError):
        sessions.create(
            tenant,
            space,
            user,
            {
                "component_id": "semantica",
                "operation": "index",
                "input": {"consent": True},
                "idempotency_key": "write",
            },
        )
    with pytest.raises(OctopError):
        sessions.create(
            tenant,
            space,
            user,
            {
                "component_id": "rna",
                "operation": "genes",
                "input": {"url": "http://foreign.test"},
                "idempotency_key": "arbitrary",
            },
        )


async def test_octop_readback_replay_and_cancellation(platform: Any) -> None:
    service, user, _, tenant, space = platform
    sessions, session, task = _create(platform)
    body = _action(session, "read")
    ready = await sessions.readback(tenant, space, user, session["session_id"], body)
    assert ready["candidate"]["output"]["task_id"] == task["task_id"]
    assert ready["candidate"]["source"] == "octop_task"
    assert await sessions.readback(tenant, space, user, session["session_id"], body) == ready
    cancel = _action(ready, "cancel")
    cancelled = sessions.cancel(tenant, space, user, session["session_id"], cancel)
    assert cancelled["status"] == "cancelled" and cancelled["candidate"] is None
    assert sessions.cancel(tenant, space, user, session["session_id"], cancel) == cancelled
    assert service.task(tenant, task["task_id"], user)["revision"] == task["revision"]


async def test_node_result_reads_persisted_checkpoint_only(platform: Any) -> None:
    service, user, _, tenant, space = platform
    sessions, session, task = _create(platform, operation="node_result", node_id="observe")
    with pytest.raises(OctopError):
        await sessions.readback(
            tenant, space, user, session["session_id"], _action(session, "no-result")
        )
    with service.services.db.transaction() as conn:
        conn.execute(
            "UPDATE bm_tasks SET checkpoint_json=? WHERE tenant_id=? AND task_id=?",
            (
                canonical(
                    {"node_results": {"observe": {"status": "succeeded", "result": {"value": 7}}}}
                ),
                tenant,
                task["task_id"],
            ),
        )
    ready = await sessions.readback(
        tenant, space, user, session["session_id"], _action(session, "read-node")
    )
    assert ready["candidate"]["output"]["node_result"]["result"] == {"value": 7}


@pytest.mark.parametrize(
    "component,operation", [("beatdesign", "canvas"), ("rna", "genes"), ("rna", "observability")]
)
async def test_real_project_adapters_receive_only_frozen_project(
    platform: Any, monkeypatch: pytest.MonkeyPatch, component: str, operation: str
) -> None:
    _, user, _, tenant, space = platform
    _bind(platform, component)
    monkeypatch.setenv("OCTOP_BEATDESIGN_MCP_URL", "http://local.test/mcp")
    monkeypatch.setenv("OCTOP_RNA_URL", "http://local.test")
    sessions, session, _ = _create(platform, component, operation)
    calls: list[str] = []

    async def beat(
        self: BeatDesignAdapter, tenant_id: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        assert (
            self.tenant_projects == {tenant: "project-a"}
            and tenant_id == tenant
            and arguments == {}
        )
        assert self._authorization_check is not None
        self._authorization_check()
        calls.append(tool)
        return {"cards": ["a"]}

    async def rna(self: RnaAdapter, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        assert method == "GET" and payload is None
        assert self._authorization_check is not None
        self._authorization_check()
        calls.append(path)
        return {"genes": ["a"]}

    monkeypatch.setattr(BeatDesignAdapter, "call_tool", beat)
    monkeypatch.setattr(RnaAdapter, "_request", rna)
    ready = await sessions.readback(
        tenant, space, user, session["session_id"], _action(session, "read")
    )
    assert len(calls) == 1 and ready["candidate"]["verified"] is True
    assert ready["candidate"]["usage"] == {"total_tokens": 0}


@pytest.mark.parametrize("change", ["binding", "deployment", "membership", "race"])
async def test_inflight_changes_cannot_persist_readback(
    platform: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    service, user, admin, tenant, space = platform
    _bind(platform, "rna")
    monkeypatch.setenv("OCTOP_RNA_URL", "http://local.test")
    sessions, session, _ = _create(platform, "rna", "genes")

    async def genes(self: RnaAdapter, _: str) -> dict[str, Any]:
        if change == "binding":
            ProjectToolsService(service).save_binding("rna", tenant, admin, "project-b", 1)
        elif change == "deployment":
            monkeypatch.setenv("OCTOP_RNA_URL", "http://changed.test")
        elif change == "membership":
            with service.services.db.transaction() as conn:
                conn.execute(
                    "DELETE FROM bm_memberships WHERE tenant_id=? AND user_id=?", (tenant, user.id)
                )
        else:
            sessions.cancel(
                tenant, space, user, session["session_id"], _action(session, "cancel-inflight")
            )
        return {"genes": []}

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    with pytest.raises(OctopError) as error:
        await sessions.readback(
            tenant, space, user, session["session_id"], _action(session, "read")
        )
    assert error.value.status == (403 if change == "membership" else 409)
    document = (
        sessions.repo.get(tenant, space, user.id, session["session_id"])
        if change != "membership"
        else None
    )
    if document is not None:
        assert document["candidate"] is None


async def test_hindsight_reads_private_bank_without_retaining(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, user, _, tenant, space = platform
    monkeypatch.setenv("OCTOP_HINDSIGHT_URL", "http://local.test")
    sessions, session, _ = _create(
        platform, "hindsight", "recall", input={"query": "previous outcome", "max_tokens": 128}
    )

    async def recall(
        self: HindsightAdapter, *, tenant_id: str, query: str, max_tokens: int
    ) -> dict[str, Any]:
        assert (
            tenant_id == tenant + ":space:" + space
            and query == "previous outcome"
            and max_tokens == 128
        )
        assert self._authorization_check is not None
        self._authorization_check()
        return {"bank_id": self.bank_id(tenant_id), "results": []}

    monkeypatch.setattr(HindsightAdapter, "recall", recall)
    ready = await sessions.readback(
        tenant, space, user, session["session_id"], _action(session, "read")
    )
    assert ready["candidate"]["source"] == "hindsight"


async def test_benchmark_reads_only_private_existing_report(platform: Any) -> None:
    service, user, _, tenant, space = platform
    private = (
        service.components.directory("commerceagentbench", "results_subdirectory")
        / tenant_namespace(tenant)
        / space
    )
    _bundle(private)
    sessions, session, _ = _create(
        platform,
        "commerceagentbench",
        "report",
        input={"run_dir": "run-1"},
    )
    ready = await sessions.readback(
        tenant, space, user, session["session_id"], _action(session, "read")
    )
    assert ready["candidate"]["output"]["goal_outcome"] == "achieved"
    with pytest.raises(OctopError):
        sessions.create(
            tenant,
            space,
            user,
            {
                "component_id": "commerceagentbench",
                "operation": "report",
                "input": {"run_dir": "../foreign"},
                "idempotency_key": "escape",
            },
        )


async def test_semantica_reads_existing_receipt_without_indexing(platform: Any) -> None:
    service, user, _, tenant, space = platform
    sessions, session, task = _create(platform, "semantica", "receipt")
    with pytest.raises(OctopError):
        await sessions.readback(
            tenant, space, user, session["session_id"], _action(session, "no-receipt")
        )
    service.repo.append(
        tenant,
        task["task_id"],
        "provenance.exported",
        {"operation_id": "export", "actor_user_id": user.id, "count": 1, "head_digest": "a" * 64},
    )
    before = service.evidence(tenant, task["task_id"], user)
    ready = await sessions.readback(
        tenant, space, user, session["session_id"], _action(session, "read")
    )
    assert ready["candidate"]["source"] == "octop_semantica_receipt"
    assert ready["candidate"]["output"]["receipts"][0]["receipt"]["operation_id"] == "export"
    assert service.evidence(tenant, task["task_id"], user) == before


async def test_temporal_describe_and_query_use_only_derived_authorized_id(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, user, _, tenant, space = platform
    monkeypatch.setenv("OCTOP_TEMPORAL_ADDRESS", "temporal.test:7233")
    task = service.create_task(tenant, user, {**task_body(space), "mode": "temporal"})
    sessions = OperationSessionService(service)
    session = sessions.create(
        tenant,
        space,
        user,
        {
            "component_id": "temporal",
            "operation": "status",
            "input": {},
            "task_id": task["task_id"],
            "idempotency_key": "open",
        },
    )
    calls: list[str] = []

    class Handle:
        async def describe(self) -> Any:
            calls.append("describe")
            return SimpleNamespace(status=SimpleNamespace(name="RUNNING"))

        async def query(self, name: str) -> str:
            calls.append(name)
            return "sleeping"

    class Client:
        def get_workflow_handle(self, identity: str) -> Handle:
            assert identity == workflow_id(tenant, task["task_id"])
            return Handle()

    async def connect(**kwargs: Any) -> Client:
        assert kwargs["address"] == "temporal.test:7233"
        return Client()

    monkeypatch.setattr("octop.infra.longterm.temporal_runtime.connect", connect)
    ready = await sessions.readback(
        tenant, space, user, session["session_id"], _action(session, "read")
    )
    assert calls == ["describe", "status"]
    assert ready["candidate"]["output"]["runtime_status"] == "sleeping"


async def test_two_concurrent_readbacks_accept_only_one_candidate(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, user, _, tenant, space = platform
    sessions, session, _ = _create(platform)
    arrived = 0
    gate = asyncio.Event()

    async def read(document: Any, actor: Any) -> tuple[Any, str]:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            gate.set()
        await gate.wait()
        return {"read": arrived}, "mocked_readback"

    monkeypatch.setattr(sessions, "_read", read)
    results = await asyncio.gather(
        *(
            sessions.readback(
                tenant, space, user, session["session_id"], _action(session, "read-" + str(index))
            )
            for index in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, OctopError)]
    assert len(errors) == 1 and errors[0].status == 409
    assert sessions.get(tenant, space, user, session["session_id"])["revision"] == 2


def test_console_is_role_filtered_and_invalid_urls_never_escape(platform: Any) -> None:
    service, user, admin, tenant, space = platform
    service.services.settings_repo.set(
        "longterm.component.octop", json.dumps({"console_url": "http://console.test"})
    )
    sessions, session, _ = _create(platform)
    assert session["snapshot"]["console"]["kind"] == "internal"
    service.set_member(tenant, user, admin.id, "admin")
    admin_space = service.create_space(tenant, admin, "admin")["space_id"]
    task = service.create_task(tenant, admin, task_body(admin_space))
    own = sessions.create(
        tenant,
        admin_space,
        admin,
        {
            "component_id": "octop",
            "operation": "task",
            "input": {},
            "task_id": task["task_id"],
            "idempotency_key": "admin",
        },
    )
    assert own["snapshot"]["console"]["url"] == "http://console.test"
    service.services.settings_repo.set(
        "longterm.component.octop",
        json.dumps({"console_url": "http://user:secret@console.test/?token=x"}),
    )
    assert (
        sessions.get(tenant, admin_space, admin, own["session_id"])["snapshot"]["console"]["kind"]
        == "internal"
    )
    with service.services.db.transaction() as conn:
        conn.execute("UPDATE users SET role='user' WHERE id=?", (admin.id,))
    assert (
        sessions.get(tenant, admin_space, admin, own["session_id"])["snapshot"]["console"]["kind"]
        == "internal"
    )


def test_migrations_are_discoverable_and_replay_safe(platform: Any) -> None:
    service, _, _, _, _ = platform
    assert all(
        23 in {version for version, _ in _discover(dialect)} for dialect in ("sqlite", "postgresql")
    )
    run_migrations(service.services.db)
    with service.services.db.connect() as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"bm_operation_sessions", "bm_operation_commands"} <= names
