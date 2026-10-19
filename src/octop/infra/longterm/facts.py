"""Read an operator-owned, fresh fact/readback projection without trusting model output."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import tenant_namespace
from .execution import parse_utc


def read_fact_report(
    root: Path, task: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """The configured source directory is writable only by trusted fact adapters.

    Reports bind the tenant, task, frozen plan, source evidence and observation
    window. The task HTTP API cannot supply a successful report or source root.
    """
    timestamp = now or datetime.now(UTC)
    source_root = (root / tenant_namespace(task["tenant_id"]) / task["space_id"]).resolve()
    path = (source_root / (task["task_id"] + ".json")).resolve()
    if not path.is_relative_to(source_root) or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError("trusted fact report unavailable")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("invalid fact report schema")
    for field in ("tenant_id", "space_id", "task_id", "plan_digest"):
        if report.get(field) != task[field]:
            raise ValueError("fact report scope mismatch")
    checkpoint = task.get("checkpoint") or {}
    if type(report.get("cycle")) is not int or report["cycle"] != checkpoint.get("cycle", 1):
        raise ValueError("fact report cycle mismatch")
    observed = parse_utc(report.get("observed_at", ""))
    expires = parse_utc(report.get("expires_at", ""))
    if observed > timestamp or expires <= timestamp or expires <= observed:
        raise ValueError("fact report stale or future dated")
    last_completed = checkpoint.get("last_action_completed_at")
    if (
        checkpoint.get("phase") == "Verify"
        and last_completed
        and observed < parse_utc(last_completed)
    ):
        raise ValueError("readback predates the action")
    acceptance = task.get("acceptance", {})
    for field in ("metric_revision", "scope", "currency", "timezone", "source_ids"):
        if field in acceptance and report.get(field) != acceptance[field]:
            raise ValueError("fact report acceptance identity mismatch")
    references = report.get("evidence_refs")
    if (
        not isinstance(references, list)
        or not references
        or any(not isinstance(ref, str) or not ref.strip() for ref in references)
    ):
        raise ValueError("fact source evidence is required")
    if not isinstance(report.get("facts"), dict) or report.get("quality") != "fit":
        raise ValueError("operational data is unfit")
    if report.get("goal_outcome") not in {"achieved", "not_achieved", "inconclusive"}:
        raise ValueError("explicit fact outcome is required")
    # Achieved requires a mature observation window and provider readback.
    if report["goal_outcome"] == "achieved" and (
        report.get("readback_confirmed") is not True
        or report.get("observation_window_complete") is not True
    ):
        raise ValueError("achievement needs confirmed readback and a mature window")
    duration = acceptance.get("observation_window_seconds")
    if report["goal_outcome"] == "achieved" and duration is not None:
        if type(duration) not in {int, float} or duration <= 0:
            raise ValueError("invalid observation window acceptance")
        window = report.get("observation_window")
        if not isinstance(window, dict):
            raise ValueError("observation window timestamps required")
        start = parse_utc(window.get("started_at", ""))
        end = parse_utc(window.get("ended_at", ""))
        if end > observed or (end - start).total_seconds() < duration:
            raise ValueError("observation window is not mature")
    return report
