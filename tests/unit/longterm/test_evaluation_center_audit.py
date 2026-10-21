"""Independent regressions for evaluation control recovery and false positives."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import pytest

from octop.infra.db.repos.longterm import RevisionConflict, canonical
from octop.infra.errors import OctopError
from octop.infra.longterm.adapters import tenant_namespace
from octop.infra.longterm.evaluation_center import EvaluationCenterService
from tests.unit.longterm.test_evaluation import _bundle
from tests.unit.longterm.test_service import platform as platform


async def _queued(
    platform: Any, monkeypatch: pytest.MonkeyPatch, *, forbidden: bool = False
) -> tuple[Any, Any, Any, Any]:
    service, user, _, tenant, space = platform
    center = service.evaluation_center
    monkeypatch.setattr(service, "schedule", lambda *_: None)
    monkeypatch.setattr(center, "schedule", lambda *_: None)
    graders = [{"id": "terminal", "type": "execution_status", "expected": "completed"}]
    if forbidden:
        graders.append(
            {
                "id": "no-index",
                "type": "forbidden_operation",
                "operation": "component.semantica.index",
                "safety": True,
            }
        )
    suite = center.save_suite(
        tenant,
        user,
        {
            "space_id": space,
            "name": "Independent control audit",
            "cases": [
                {"case_id": "audit", "goal": "Run one controlled local echo", "graders": graders}
            ],
        },
    )
    run = center.create_run(
        tenant,
        user,
        {
            "space_id": space,
            "suite_id": suite["suite_id"],
            "repeats": 1,
            "max_parallel": 1,
            "max_total_tokens": 4096,
            "idempotency_key": "audit-create",
        },
    )
    await center.tick(tenant, run["run_id"])
    run = center.run(tenant, user, run["run_id"])
    attempt = run["attempts"][0]
    task = service.task(tenant, attempt["native_task_id"], user)
    assert task["status"] == "queued"
    return center, run, attempt, task


async def test_cancel_cas_conflict_keeps_child_eligible_for_recovery(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, user, _, tenant, _ = platform
    center, run, attempt, task = await _queued(platform, monkeypatch)
    command = service.command
    conflicts = 0

    def once(tenant_id: str, task_id: str, actor: Any, data: dict[str, Any]) -> Any:
        nonlocal conflicts
        if data["command"] == "cancel" and conflicts == 0:
            conflicts += 1
            raise RevisionConflict("injected child checkpoint race")
        return command(tenant_id, task_id, actor, data)

    monkeypatch.setattr(service, "command", once)
    result = center.command(
        tenant,
        user,
        run["run_id"],
        {
            "command": "cancel",
            "expected_revision": run["revision"],
            "idempotency_key": "audit-cancel",
        },
    )
    child = service.task(tenant, task["task_id"], user)
    observed = center.run(tenant, user, run["run_id"])["attempts"][0]
    assert observed["status"] != "cancelled" or child["status"] in {
        "cancelled",
        "completed",
        "failed",
        "uncertain",
    }
    recovered = EvaluationCenterService(service)
    monkeypatch.setattr(recovered, "schedule", lambda *_: None)
    await recovered.recover()
    await recovered.tick(tenant, result["run_id"])
    assert service.task(tenant, task["task_id"], user)["status"] == "cancelled"
    assert recovered.run(tenant, user, run["run_id"])["attempts"][0]["status"] == "cancelled"


async def test_cancel_between_admission_check_and_child_insert_cannot_orphan_child(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, user, _, tenant, space = platform
    center = service.evaluation_center
    monkeypatch.setattr(service, "schedule", lambda *_: None)
    monkeypatch.setattr(center, "schedule", lambda *_: None)
    run = center.create_run(
        tenant,
        user,
        {"space_id": space, "repeats": 1, "idempotency_key": "admission-cancel"},
    )
    insert = service.repo.create_task
    interrupted = False

    def cancel_before_insert(document: Any, key: str, request: str) -> Any:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            current = center.run(tenant, user, run["run_id"])
            center.command(
                tenant,
                user,
                run["run_id"],
                {
                    "command": "cancel",
                    "expected_revision": current["revision"],
                    "idempotency_key": "cancel-during-admission",
                },
            )
        return insert(document, key, request)

    monkeypatch.setattr(service.repo, "create_task", cancel_before_insert)
    with suppress(RevisionConflict):
        await center.tick(tenant, run["run_id"])
    recovered = EvaluationCenterService(service)
    monkeypatch.setattr(recovered, "schedule", lambda *_: None)
    await recovered.recover()
    await recovered.tick(tenant, run["run_id"])
    assert interrupted
    assert all(
        task["status"] in {"cancelled", "completed", "failed", "uncertain"}
        for task in service.tasks(tenant, user)
    )


async def test_resume_recovery_replays_child_control_after_campaign_persist(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, user, _, tenant, _ = platform
    center, run, _, task = await _queued(platform, monkeypatch)
    paused = center.command(
        tenant,
        user,
        run["run_id"],
        {
            "command": "pause",
            "expected_revision": run["revision"],
            "idempotency_key": "audit-pause",
        },
    )
    assert service.task(tenant, task["task_id"], user)["status"] == "paused"
    control = center._control_tasks

    def crash(saved: Any, actor: Any, command: str) -> None:
        if command == "resume":
            raise RuntimeError("injected crash after campaign command persisted")
        control(saved, actor, command)

    monkeypatch.setattr(center, "_control_tasks", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        center.command(
            tenant,
            user,
            run["run_id"],
            {
                "command": "resume",
                "expected_revision": paused["revision"],
                "idempotency_key": "audit-resume",
            },
        )
    assert center.run(tenant, user, run["run_id"])["status"] == "running"
    recovered = EvaluationCenterService(service)
    monkeypatch.setattr(recovered, "schedule", lambda *_: None)
    await recovered.recover()
    await recovered.tick(tenant, run["run_id"])
    assert service.task(tenant, task["task_id"], user)["status"] != "paused"


async def test_forbidden_operation_after_first_evidence_page_cannot_pass(
    platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, user, _, tenant, _ = platform
    center, run, _, task = await _queued(platform, monkeypatch, forbidden=True)
    with service.repo.scoped(tenant) as conn:
        for index in range(1001):
            service.repo._append(
                conn, tenant, task["task_id"], "audit.observation", {"index": index}
            )
        service.repo._append(
            conn,
            tenant,
            task["task_id"],
            "action_completed",
            {
                "kind": "action_completed",
                "node_id": "echo",
                "cycle": 1,
                "operation": "component.semantica.index",
                "status": "succeeded",
                "result": {"indexed": True},
            },
        )
        conn.execute(
            "UPDATE bm_tasks SET status='completed',checkpoint_json=? WHERE tenant_id=? AND task_id=?",
            (
                canonical(
                    {
                        "node_results": {"echo": {"status": "succeeded", "result": {"output": {}}}},
                        "token_usage": {"total_tokens": 0},
                    }
                ),
                tenant,
                task["task_id"],
            ),
        )
    evidence = service.evidence(tenant, task["task_id"], user)
    assert evidence["valid"] is True and evidence["last_sequence"] > evidence["next_cursor"]
    await center.tick(tenant, run["run_id"])
    attempt = center.run(tenant, user, run["run_id"])["attempts"][0]
    assert attempt["status"] == "verified_fail"
    assert attempt["result"]["safety_passed"] is False


def test_cab_import_rejects_symlinked_private_result_root(platform: Any) -> None:
    service, user, _, tenant, space = platform
    center = service.evaluation_center
    base = service.components.directory("commerceagentbench", "results_subdirectory")
    private = base / tenant_namespace(tenant) / space
    other_space = service.create_space(tenant, user, "Other private scope")["space_id"]
    other = base / tenant_namespace(tenant) / other_space
    _bundle(other)
    private.parent.mkdir(parents=True, exist_ok=True)
    try:
        private.symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this host")
    with pytest.raises((OctopError, ValueError)):
        center.import_cab(
            tenant,
            user,
            {"space_id": space, "run_dirs": ["run-1"], "idempotency_key": "symlink-import"},
        )
