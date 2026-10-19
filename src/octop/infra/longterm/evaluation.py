"""Read real Commerce Agent Bench bundles without treating process exit as success."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _score(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number <= 1 else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def execution_metrics(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """BM slide 39 denominators; no goal outcome is inferred from these events.

    Node records use ``kind=node``, ``due`` and ``execution_ready``. Action
    records use ``kind=action`` and ``authorized``. Missing eligibility is not
    silently counted as eligible; unknown, waiting and uncertain remain visible.
    """
    nodes = [row for row in events if row.get("kind") == "node"]
    eligible = [
        row for row in nodes if row.get("due") is True and row.get("execution_ready") is True
    ]
    actions = [row for row in events if row.get("kind") == "action"]
    # Runtime completion events are attempts. An earlier intent alone does not
    # prove dispatch, and a successful tool return alone is not a readback.
    runtime_actions = [row for row in events if row.get("kind") == "action_completed"]
    intents = {
        (row.get("tenant_id"), row.get("task_id"), row.get("cycle"), row.get("node_id")): row
        for row in events
        if row.get("kind") == "action_intent"
    }
    for row in runtime_actions:
        identity = (row.get("tenant_id"), row.get("task_id"), row.get("cycle"), row.get("node_id"))
        result = row.get("result")
        result = result if isinstance(result, dict) else {}
        readback = result.get("readback")
        readback = readback if isinstance(readback, dict) else {}
        verified = (result.get("verified") is True or readback.get("confirmed") is True) and bool(
            result.get("evidence_refs") or readback.get("evidence_refs")
        )
        actions.append(
            {
                "authorized": intents.get(identity, {}).get("authorized") is True,
                "status": "confirmed" if verified else row.get("status"),
            }
        )
    authorized = [row for row in actions if row.get("authorized") is True]
    confirmed_nodes = sum(row.get("status") == "confirmed" for row in eligible)
    confirmed_actions = sum(row.get("status") == "confirmed" for row in authorized)
    return {
        "task_completion_rate": _ratio(confirmed_nodes, len(eligible)),
        "task_completion_numerator": confirmed_nodes,
        "task_completion_denominator": len(eligible),
        "excluded_nodes": len(nodes) - len(eligible),
        "confirmed_action_rate": _ratio(confirmed_actions, len(authorized)),
        "confirmed_action_numerator": confirmed_actions,
        "confirmed_action_denominator": len(authorized),
        "waiting_approval": sum(
            row.get("status") == "waiting_approval" or row.get("kind") == "approval_required"
            for row in events
        ),
        "uncertain": sum(row.get("status") == "uncertain" for row in actions),
        "rejected": sum(row.get("status") == "rejected" for row in actions),
        "inconclusive": sum(row.get("goal_outcome") == "inconclusive" for row in events),
        "runtime_actions_attempted": len(runtime_actions),
        "runtime_actions_succeeded": sum(
            row.get("status") == "succeeded" for row in runtime_actions
        ),
        "runtime_actions_authorization_unknown": sum(
            identity not in intents or intents[identity].get("authorized") is not True
            for identity in (
                (row.get("tenant_id"), row.get("task_id"), row.get("cycle"), row.get("node_id"))
                for row in runtime_actions
            )
        ),
        "goal_outcome": "unverified",
    }


class CommerceBenchAdapter:
    """Import a single task run below a tenant-owned result directory.

    The upstream source of truth is ``verifier/final_reward.json`` (or the
    identical ``manifest.reward``), not ``agent/result.json.passed``. This
    adapter never invokes a model, creates a reward, or reads an arbitrary path.
    Call blocking file reads through an executor from async transports.
    """

    def __init__(self, results_root: Path, *, max_artifact_bytes: int = 16 * 1024 * 1024) -> None:
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self.results_root = results_root.resolve()
        self.max_artifact_bytes = max_artifact_bytes

    def _read(
        self,
        run_dir: Path,
        relative: str,
        provenance: list[dict[str, str]],
        *,
        required: bool = False,
    ) -> dict[str, Any] | None:
        path = (run_dir / relative).resolve()
        if not path.is_relative_to(run_dir):
            raise ValueError("Benchmark artifact escapes its run directory")
        if not path.exists() and not required:
            return None
        if path.stat().st_size > self.max_artifact_bytes:
            raise ValueError(f"Benchmark artifact exceeds size limit: {relative}")
        raw = path.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"Benchmark artifact must be a JSON object: {relative}")
        provenance.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest()})
        return value

    def load_run(
        self, relative_run_dir: str, *, expected_task_id: str | None = None
    ) -> dict[str, Any]:
        """Return recorded outcomes, unknown fields as null, and source hashes."""
        relative = Path(relative_run_dir)
        run_dir = (self.results_root / relative).resolve()
        if (
            relative.is_absolute()
            or not run_dir.is_relative_to(self.results_root)
            or run_dir == self.results_root
        ):
            raise ValueError("A run directory relative to the result root is required")
        provenance: list[dict[str, str]] = []
        manifest = self._read(run_dir, "manifest.json", provenance, required=True)
        assert manifest is not None
        task_id = manifest.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Benchmark manifest requires task_id")
        if expected_task_id is not None and task_id != expected_task_id:
            raise ValueError("Benchmark task does not match the requested task")
        final = self._read(run_dir, "verifier/final_reward.json", provenance)
        reward = final if final is not None else manifest.get("reward")
        reward = reward if isinstance(reward, dict) else {}
        # Manifest and the dedicated reward must agree when both are present.
        if (
            final is not None
            and isinstance(manifest.get("reward"), dict)
            and manifest["reward"] != final
        ):
            raise ValueError("Benchmark manifest and final reward disagree")
        trajectory = self._read(run_dir, "agent/trajectory.json", provenance) or {}
        returncode = manifest.get("agent_exec_returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            returncode = None
        signals = reward.get("framework_signals")
        signals = signals if isinstance(signals, dict) else {}
        verifier_exit = reward.get("verifier_exit", signals.get("verifier_exit"))
        verifier_completed = type(verifier_exit) is int and verifier_exit in (0, 6)
        passed = reward.get("passed")
        outcome = "achieved" if passed is True else "not_achieved"
        reasons: list[str] = []
        if not verifier_completed or not isinstance(passed, bool):
            outcome = "unverified"
            reasons.append("completed_verifier_and_explicit_verdict_required")
        score = _score(reward.get("score", reward.get("reward"))) if verifier_completed else None
        if score is None:
            reasons.append("no_valid_recorded_score")
        calls = trajectory.get("tool_calls")
        calls = calls if isinstance(calls, list) else None
        results = trajectory.get("tool_results")
        results = results if isinstance(results, list) else None
        usage = trajectory.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        # OpenClaw recovery may expose proxy request counts as synthetic steps.
        proxy_steps = trajectory.get("recovered_from_proxy_usage") is True
        tool_errors = (
            sum(isinstance(row, dict) and row.get("isError") is True for row in results)
            if results is not None
            else None
        )
        return {
            "source": "CommerceAgentBench",
            "task_id": task_id,
            "run_id": manifest.get("run_id", relative_run_dir),
            "execution_health": {
                "status": "healthy"
                if returncode == 0
                else "degraded"
                if returncode is not None
                else "unknown",
                "agent_exec_returncode": returncode,
                "agent_early_terminated": manifest.get("agent_early_terminated") is True,
                "verifier_completed": verifier_completed,
                "verifier_exit": verifier_exit if type(verifier_exit) is int else None,
            },
            "goal_outcome": outcome,
            "score": score,
            "raw_score": _score(reward.get("raw_score")),
            "reward_mode": reward.get("reward_mode"),
            "long_horizon_metrics": {
                "duration_ms": _number(trajectory.get("duration_ms")),
                "observed_steps": len(calls) if calls is not None else None,
                "step_source": "proxy_requests" if proxy_steps else trajectory.get("source"),
                "tool_calls": len(calls) if calls is not None and not proxy_steps else None,
                "tool_errors": tool_errors,
                "total_tokens": _number(usage.get("total_tokens", usage.get("totalTokens"))),
                "checks_total": self._check_count(reward, "total"),
                "checks_passed": self._check_count(reward, "passed"),
                "cycle_count": None,
                "restart_recovery_rate": None,
            },
            "experiment": {
                key: manifest.get(key)
                for key in (
                    "harness",
                    "image",
                    "model_provider",
                    "model_name",
                    "started_at",
                    "finished_at",
                )
            },
            "provenance": provenance,
            "limitations": reasons + ["Benchmark task success is not verified business uplift."],
        }

    @staticmethod
    def _check_count(reward: dict[str, Any], name: str) -> int | None:
        summary = reward.get("check_summary")
        value = summary.get(name) if isinstance(summary, dict) else None
        return value if type(value) is int and value >= 0 else None
