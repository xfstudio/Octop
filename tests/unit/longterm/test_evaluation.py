"""Commerce bundle contracts: process health never substitutes for the verifier."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from octop.infra.longterm.evaluation import CommerceBenchAdapter, execution_metrics


def _bundle(tmp_path: Path, *, returncode: int = 0, passed: bool = True) -> Path:
    run = tmp_path / "run-1"
    (run / "agent").mkdir(parents=True)
    (run / "verifier").mkdir()
    reward = {
        "passed": passed,
        "score": float(passed),
        "raw_score": 0.9,
        "verifier_exit": 0 if passed else 6,
        "reward_mode": "binary_all_checks",
        "check_summary": {"total": 11, "passed": 11 if passed else 8},
    }
    manifest = {
        "task_id": "commerce-task",
        "run_id": "run-1",
        "agent_exec_returncode": returncode,
        "reward": reward,
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run / "verifier" / "final_reward.json").write_text(json.dumps(reward), encoding="utf-8")
    (run / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "source": "openclaw_chat",
                "duration_ms": 12450,
                "tool_calls": [{"name": "browser"}],
                "tool_results": [{"isError": True}],
                "usage": {"total_tokens": 450},
            }
        ),
        encoding="utf-8",
    )
    return run


def test_real_verifier_overrules_process_exit_and_preserves_provenance(tmp_path: Path) -> None:
    _bundle(tmp_path, returncode=1)
    result = CommerceBenchAdapter(tmp_path).load_run("run-1", expected_task_id="commerce-task")
    assert result["execution_health"]["status"] == "degraded"
    assert result["goal_outcome"] == "achieved"
    assert result["score"] == 1.0
    assert result["raw_score"] == 0.9
    assert result["long_horizon_metrics"] == {
        "duration_ms": 12450.0,
        "observed_steps": 1,
        "step_source": "openclaw_chat",
        "tool_calls": 1,
        "tool_errors": 1,
        "total_tokens": 450.0,
        "checks_total": 11,
        "checks_passed": 11,
        "cycle_count": None,
        "restart_recovery_rate": None,
    }
    assert len(result["provenance"]) == 3
    assert all(len(row["sha256"]) == 64 for row in result["provenance"])


def test_clean_process_with_failed_goal_is_not_achieved(tmp_path: Path) -> None:
    _bundle(tmp_path, passed=False)
    result = CommerceBenchAdapter(tmp_path).load_run("run-1")
    assert result["execution_health"]["status"] == "healthy"
    assert result["goal_outcome"] == "not_achieved"
    assert result["score"] == 0.0


def test_missing_verifier_does_not_fabricate_score_from_agent_pass(tmp_path: Path) -> None:
    run = _bundle(tmp_path)
    (run / "verifier" / "final_reward.json").unlink()
    (run / "manifest.json").write_text(
        json.dumps({"task_id": "commerce-task", "agent_exec_returncode": 0}), encoding="utf-8"
    )
    (run / "agent" / "result.json").write_text('[{"passed": true}]', encoding="utf-8")
    result = CommerceBenchAdapter(tmp_path).load_run("run-1")
    assert result["goal_outcome"] == "unverified"
    assert result["score"] is None


@pytest.mark.parametrize("score", [True, -0.1, 1.1, "1", None, 10**400])
def test_invalid_scores_remain_unknown(tmp_path: Path, score: Any) -> None:
    run = _bundle(tmp_path)
    reward_path = run / "verifier" / "final_reward.json"
    reward = json.loads(reward_path.read_text(encoding="utf-8"))
    reward["score"] = score
    reward_path.write_text(json.dumps(reward), encoding="utf-8")
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reward"] = reward
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert CommerceBenchAdapter(tmp_path).load_run("run-1")["score"] is None


def test_incomplete_verifier_cannot_prove_goal(tmp_path: Path) -> None:
    run = _bundle(tmp_path)
    (run / "verifier" / "final_reward.json").unlink()
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reward"]["verifier_exit"] = 124
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = CommerceBenchAdapter(tmp_path).load_run("run-1")
    assert result["goal_outcome"] == "unverified"
    assert result["score"] is None


def test_proxy_request_steps_are_not_claimed_as_tool_calls(tmp_path: Path) -> None:
    run = _bundle(tmp_path)
    (run / "agent" / "trajectory.json").write_text(
        json.dumps(
            {"source": "proxy_usage", "recovered_from_proxy_usage": True, "tool_calls": [{}, {}]}
        ),
        encoding="utf-8",
    )
    metrics = CommerceBenchAdapter(tmp_path).load_run("run-1")["long_horizon_metrics"]
    assert metrics["observed_steps"] == 2
    assert metrics["step_source"] == "proxy_requests"
    assert metrics["tool_calls"] is None
    assert metrics["total_tokens"] is None


def test_conflicting_artifacts_and_wrong_task_are_rejected(tmp_path: Path) -> None:
    run = _bundle(tmp_path)
    adapter = CommerceBenchAdapter(tmp_path)
    with pytest.raises(ValueError, match="task does not match"):
        adapter.load_run("run-1", expected_task_id="another-task")
    (run / "verifier" / "final_reward.json").write_text('{"passed":false}', encoding="utf-8")
    with pytest.raises(ValueError, match="disagree"):
        adapter.load_run("run-1")


@pytest.mark.parametrize("relative", ["..", "../outside", "."])
def test_result_root_cannot_be_escaped(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError, match="relative to the result root"):
        CommerceBenchAdapter(tmp_path).load_run(relative)


def test_absolute_path_and_oversized_artifact_are_rejected(tmp_path: Path) -> None:
    _bundle(tmp_path)
    with pytest.raises(ValueError):
        CommerceBenchAdapter(tmp_path).load_run(str(tmp_path / "run-1"))
    with pytest.raises(ValueError, match="size limit"):
        CommerceBenchAdapter(tmp_path, max_artifact_bytes=1).load_run("run-1")


def test_bm_denominators_exclude_nodes_without_execution_conditions() -> None:
    metrics = execution_metrics(
        [
            {"kind": "node", "due": True, "execution_ready": True, "status": "confirmed"},
            {"kind": "node", "due": True, "execution_ready": False, "status": "waiting_approval"},
            {"kind": "node", "due": True, "execution_ready": True, "status": "running"},
            {"kind": "action", "authorized": True, "status": "confirmed"},
            {"kind": "action", "authorized": True, "status": "uncertain"},
            {"kind": "action", "authorized": False, "status": "rejected"},
        ]
    )
    assert metrics["task_completion_rate"] == 0.5
    assert metrics["excluded_nodes"] == 1
    assert metrics["confirmed_action_rate"] == 0.5
    assert metrics["confirmed_action_denominator"] == 2
    assert metrics["waiting_approval"] == metrics["uncertain"] == metrics["rejected"] == 1
    assert metrics["goal_outcome"] == "unverified"
    assert execution_metrics([])["task_completion_rate"] is None


def test_runtime_returns_need_independent_readback_to_count_as_confirmed() -> None:
    identity = {"tenant_id": "a", "task_id": "goal", "cycle": 1, "node_id": "n1"}
    intent = {**identity, "kind": "action_intent", "authorized": True}
    completion = {
        **identity,
        "kind": "action_completed",
        "status": "succeeded",
        "result": {"output": "done"},
    }
    metrics = execution_metrics([intent, completion])
    assert metrics["confirmed_action_denominator"] == 1
    assert metrics["confirmed_action_rate"] == 0
    assert metrics["runtime_actions_succeeded"] == 1
    completion["result"] = {"readback": {"confirmed": True, "evidence_refs": ["platform:readback"]}}
    assert execution_metrics([intent, completion])["confirmed_action_rate"] == 1
    assert execution_metrics([completion])["confirmed_action_rate"] is None
