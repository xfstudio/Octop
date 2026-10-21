"""Evaluation contracts, real local DAG trials and private persistent campaigns."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from fastapi import FastAPI

from octop.api.app import _install_exception_handlers
from octop.api.deps import current_user
from octop.api.routers.longterm import service as longterm_dependency
from octop.api.routers.longterm_evaluations import router
from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.longterm import AuthorityRevoked, RevisionConflict
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.longterm.evaluation_center import (
    EvaluationCenterService,
    aggregate_results,
    grade_case,
    load_cab_report,
)
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout


@pytest.fixture
async def center_platform(tmp_path, monkeypatch):
    for name in ("OCTOP_FACT_REPORTS_ROOT", "OCTOP_TEMPORAL_ADDRESS", "OCTOP_LONGTERM_WORKER"):
        monkeypatch.delenv(name, raising=False)
    db = SqlitePool(tmp_path / "evaluations.db")
    run_migrations(db)
    shared = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    first = shared.user_repo.create(username="eval-owner", role="user")
    second = shared.user_repo.create(username="eval-other", role="admin")
    user, other = User(first, "eval-owner", "user", None), User(second, "eval-other", "admin", None)
    svc = LongtermService(shared)
    tenant = svc.create_tenant(user, "评测租户")["tenant_id"]
    space = svc.create_space(tenant, user, "私有实验室")["space_id"]
    try:
        yield svc, user, other, tenant, space
    finally:
        await svc.close()
        db.close()


async def finished(center, tenant, user, identifier):
    for _ in range(300):
        run = center.run(tenant, user, identifier)
        if run["status"] != "running":
            return run
        await asyncio.sleep(0.01)
    pytest.fail("evaluation did not finish its bounded local trials")


def simple_case(expected="fixed"):
    return {
        "case_id": "repair",
        "title": "结构化修复",
        "goal": "保留正确结果",
        "graders": [
            {
                "id": "output",
                "type": "json_equals",
                "path": "/nodes/echo/result/output/message",
                "expected": expected,
            },
            {"id": "path", "type": "required_operation", "operation": "local.echo"},
        ],
    }


def source_task(svc, user, tenant, space, message, key):
    return svc.create_task(
        tenant,
        user,
        {
            "space_id": space,
            "goal": "待评测冻结工作流",
            "workflow": {
                "nodes": [
                    {
                        "id": "echo",
                        "skill": "local.echo",
                        "depends_on": [],
                        "input": {"message": message},
                    }
                ],
                "max_tokens": 1024,
            },
            "idempotency_key": key,
        },
    )


def test_required_operation_rejects_correct_output_false_positive():
    case = simple_case()
    envelope = {"nodes": {"echo": {"result": {"output": {"message": "fixed"}}}}, "trace": []}
    result = grade_case(case, envelope, evidence_refs=["synthetic:test"], trace_complete=True)
    assert result["checks"][0]["status"] == "passed"
    assert result["checks"][1]["status"] == "failed"
    assert result["passed"] is False
    unknown = grade_case(case, envelope, evidence_refs=[], trace_complete=False)
    assert unknown["passed"] is None and unknown["score"] is None


def test_fixed_matrix_statistics_and_real_task_macro_capacity():
    cases = [{"case_id": "A"}, {"case_id": "B"}]
    rows = [
        {
            "case_id": key,
            "repeat_index": index + 1,
            "status": "verified_pass" if value else "verified_fail",
            "result": {"score": value},
        }
        for key, values in (("A", [1, 1, 0]), ("B", [1, 0, 0]))
        for index, value in enumerate(values)
    ]
    result = aggregate_results(cases, 3, rows)
    assert result["pass_rate"] == 0.5
    assert result["at_least_one_success_rate"] == 1
    assert result["all_trials_success_rate"] == 0
    assert result["pass_at_k"]["2"] == pytest.approx(5 / 6)
    rows[2]["status"], rows[5]["status"] = "unverified", "infra_error"
    unknown = aggregate_results(cases, 3, rows)
    assert unknown["coverage"] == pytest.approx(4 / 6)
    assert unknown["pass_rate"] is None and unknown["pass_at_k"] == {}
    assert unknown["pass_rate_lower"] == pytest.approx(0.5)
    assert unknown["pass_rate_upper"] == pytest.approx(5 / 6)
    capacity = aggregate_results(
        cases,
        1,
        [
            {"case_id": "A", "repeat_index": 1, "status": "verified_pass", "result": {"score": 1}},
            {
                "case_id": "B",
                "repeat_index": 1,
                "status": "verified_fail",
                "result": {"score": 1 / 9},
            },
        ],
    )
    assert capacity["capacity"] == pytest.approx(5 / 9)


async def test_builtin_runs_real_dags_and_preserves_inconclusive_goal(center_platform):
    svc, user, _, tenant, space = center_platform
    center = svc.evaluation_center
    body = {"space_id": space, "repeats": 3, "idempotency_key": "builtin"}
    run = center.create_run(tenant, user, body)
    done = await finished(center, tenant, user, run["run_id"])
    assert done["status"] == "completed" and done["source_class"] == "synthetic"
    assert done["aggregate"]["coverage"] == done["aggregate"]["pass_rate"] == 1
    assert len(done["attempts"]) == len(svc.tasks(tenant, user)) == 9
    assert len({row["native_task_id"] for row in done["attempts"]}) == 9
    assert all(row["task_goal_outcome"] == "inconclusive" for row in done["attempts"])
    detail = center.attempt(tenant, user, run["run_id"], done["attempts"][0]["attempt_id"])
    assert detail["evidence_valid"] and detail["trace"]
    assert detail["trace"][0]["operation"] == "local.echo"
    assert detail["trace"][0]["evidence_ref"].startswith("octop-evidence:")
    again = center.create_run(tenant, user, body)
    assert again["run_id"] == run["run_id"]
    assert len(svc.tasks(tenant, user)) == 9
    with pytest.raises(RevisionConflict):
        center.create_run(tenant, user, {**body, "repeats": 2})


async def test_suite_versions_are_immutable_and_private(center_platform):
    svc, user, other, tenant, space = center_platform
    center = svc.evaluation_center
    body = {"space_id": space, "name": "能力集", "cases": [simple_case()], "expected_revision": 0}
    one = center.save_suite(tenant, user, body)
    changed = copy.deepcopy(body)
    changed.update(
        suite_id=one["suite_id"],
        expected_revision=one["revision"],
        name="回归集",
        split="regression",
    )
    changed["cases"][0]["goal"] = "新的目标"
    two = center.save_suite(tenant, user, changed)
    assert two["version"] == 2 and two["digest"] != one["digest"]
    assert center.suite(tenant, user, one["suite_id"], 1)["cases"][0]["goal"] == "保留正确结果"
    svc.set_member(tenant, user, other.id, "admin")
    with pytest.raises(OctopError) as exc:
        center.suite(tenant, other, one["suite_id"])
    assert exc.value.status == 404
    with pytest.raises(RevisionConflict):
        center.save_suite(tenant, user, changed)


async def test_source_snapshot_and_badcase_repair_graduation(center_platform):
    svc, user, _, tenant, space = center_platform
    center = svc.evaluation_center
    suite = center.save_suite(
        tenant,
        user,
        {"space_id": space, "name": "修复能力集", "cases": [simple_case()], "expected_revision": 0},
    )
    broken = source_task(svc, user, tenant, space, "broken", "broken-source")
    fixed = source_task(svc, user, tenant, space, "fixed", "fixed-source")
    body = {
        "space_id": space,
        "suite_id": suite["suite_id"],
        "repeats": 2,
        "subject": {"kind": "source_task", "task_id": broken["task_id"]},
        "idempotency_key": "baseline",
    }
    baseline = await finished(center, tenant, user, center.create_run(tenant, user, body)["run_id"])
    candidate = await finished(
        center,
        tenant,
        user,
        center.create_run(
            tenant,
            user,
            {
                **body,
                "subject": {"kind": "source_task", "task_id": fixed["task_id"]},
                "idempotency_key": "candidate",
            },
        )["run_id"],
    )
    assert baseline["aggregate"]["pass_rate"] == 0 and candidate["aggregate"]["pass_rate"] == 1
    assert svc.task(tenant, broken["task_id"], user)["status"] == "draft"
    derived = svc.task(tenant, baseline["attempts"][0]["native_task_id"], user)
    assert derived["workflow"] == broken["workflow"]
    assert baseline["source_class"] == candidate["source_class"] == "synthetic"
    comparison = center.compare(
        tenant,
        user,
        {"baseline_run_id": baseline["run_id"], "candidate_run_id": candidate["run_id"]},
    )
    assert comparison["gate"] == "GO" and comparison["mean_improvement"] == 1
    assert comparison["release_eligible"] is False
    badcase = baseline["badcases"][0]
    with pytest.raises(OctopError):
        center.update_badcase(
            tenant,
            user,
            badcase["badcase_id"],
            {"expected_revision": badcase["revision"], "status": "verified"},
        )
    verified = center.update_badcase(
        tenant,
        user,
        badcase["badcase_id"],
        {
            "expected_revision": badcase["revision"],
            "verification_run_id": candidate["run_id"],
            "root_cause": "input",
            "fix_ref": "fixed-source",
        },
    )
    assert verified["status"] == "verified"
    regression = center.graduate_badcase(
        tenant, user, verified["badcase_id"], {"expected_revision": verified["revision"]}
    )
    assert regression["split"] == "regression" and regression["cases"][0] == simple_case()


async def test_resume_reuses_preallocated_attempt_task_and_cancel_recovers(
    center_platform, monkeypatch
):
    svc, user, _, tenant, space = center_platform
    center = svc.evaluation_center
    monkeypatch.setattr(center, "schedule", lambda *_: None)
    monkeypatch.setattr(svc, "schedule", lambda *_: None)
    run = center.create_run(
        tenant, user, {"space_id": space, "repeats": 1, "idempotency_key": "restart"}
    )
    await center.tick(tenant, run["run_id"])
    first_ids = {row["native_task_id"] for row in center.repo.attempts(tenant, run["run_id"])}
    again = EvaluationCenterService(svc)
    await again.tick(tenant, run["run_id"])
    assert first_ids == {
        row["native_task_id"] for row in again.repo.attempts(tenant, run["run_id"])
    }
    assert len(svc.tasks(tenant, user)) == 2  # parallel cap, not a duplicate task
    current = center.run(tenant, user, run["run_id"])
    paused = center.command(
        tenant,
        user,
        run["run_id"],
        {"command": "pause", "expected_revision": current["revision"], "idempotency_key": "pause"},
    )
    assert all(
        svc.task(tenant, row["task_id"], user)["status"] == "paused"
        for row in svc.tasks(tenant, user)
    )
    assert (
        center.command(
            tenant,
            user,
            run["run_id"],
            {
                "command": "pause",
                "expected_revision": current["revision"],
                "idempotency_key": "pause",
            },
        )["run_id"]
        == run["run_id"]
    )
    cancelled = center.command(
        tenant, user, run["run_id"], {"command": "cancel", "expected_revision": paused["revision"]}
    )
    assert cancelled["status"] == "cancelled"
    assert all(row["status"] == "cancelled" for row in cancelled["attempts"])
    await again.recover()
    await again.close()
    assert len(svc.tasks(tenant, user)) == 2


async def test_budget_scope_revocation_and_rules_are_bounded(center_platform):
    svc, user, other, tenant, space = center_platform
    center = svc.evaluation_center
    with pytest.raises(OctopError):
        center.create_run(
            tenant, user, {"space_id": space, "max_total_tokens": 1, "idempotency_key": "tiny"}
        )
    with pytest.raises(OctopError):
        center.validate_cases(
            [
                {
                    **simple_case(),
                    "graders": [{"id": "exec", "type": "shell", "expected": "anything"}],
                }
            ]
        )
    with pytest.raises(OctopError):
        center.create_run(tenant, other, {"space_id": space, "idempotency_key": "foreign"})
    suite = center.save_suite(
        tenant, user, {"space_id": space, "name": "受限", "cases": [simple_case()]}
    )
    svc.services.user_repo.set_disabled(user.id, True)
    with pytest.raises(OctopError):
        center.suite(tenant, user, suite["suite_id"])
    with pytest.raises(AuthorityRevoked):
        center.repo.save({**suite, "name": "停用后写入"}, expected=suite["revision"], actor=user.id)


def test_cab_import_retains_real_verifier_counts_and_marks_mock_synthetic(tmp_path):
    run = tmp_path / "run"
    (run / "verifier").mkdir(parents=True)
    reward = {
        "schema_version": "2.0",
        "passed": False,
        "score": 0,
        "raw_score": 0.5,
        "verifier_exit": 6,
        "checks_passed": 1,
        "checks_total": 2,
    }
    manifest = {
        "task_id": "task",
        "run_id": "r",
        "harness": "openclaw",
        "agent_exec_returncode": 0,
        "reward": reward,
        "llm_judge_provider": "mock",
        "alternative_harness": {"openclaw_model": "provider/model"},
        "started_at": "2026-01-01T00:00:00",
        "finished_at": "2026-01-01T00:00:02",
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "verifier" / "final_reward.json").write_text(json.dumps(reward), encoding="utf-8")
    report = load_cab_report(tmp_path, "run")
    assert report["synthetic"] is True
    assert report["goal_outcome"] == "not_achieved" and report["capacity"] == 0.5
    assert report["experiment"]["harness"] == "openclaw"
    assert report["experiment"]["model_name"] == "provider/model"
    assert report["long_horizon_metrics"]["duration_ms"] == 2000
    with pytest.raises(ValueError):
        load_cab_report(tmp_path, "../outside")


async def test_http_contract_openapi_and_native_start(center_platform):
    svc, user, other, tenant, space = center_platform
    app = FastAPI()
    _install_exception_handlers(app)
    app.include_router(router, prefix="/api/longterm")
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[longterm_dependency] = lambda: svc
    prefix = f"/api/longterm/tenants/{tenant}/evaluations"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get(prefix + "/suites", params={"space_id": space})
        assert listed.status_code == 200 and len(listed.json()["builtin"]["cases"]) == 3
        started = await client.post(
            prefix + "/runs", json={"space_id": space, "repeats": 1, "idempotency_key": "http"}
        )
        assert started.status_code == 200
        done = await finished(svc.evaluation_center, tenant, user, started.json()["run_id"])
        assert done["aggregate"]["pass_rate"] == 1
        invalid = await client.post(
            prefix + "/runs",
            json={"space_id": space, "idempotency_key": "bad", "command": "echo arbitrary"},
        )
        assert invalid.status_code == 422
        app.dependency_overrides[current_user] = lambda: other
        denied = await client.get(prefix + "/runs/" + done["run_id"])
        assert denied.status_code == 403
    schema = app.openapi()
    assert "EvaluationRunResponse" in schema["components"]["schemas"]
    assert all(
        operation.get("summary") and operation.get("description")
        for route in schema["paths"].values()
        for operation in route.values()
    )
