"""Pure workflow compilation and authorization, shared by local and Temporal runs.

Controls are trusted service inputs, never agent output. An executor must enforce
the declared tool permissions; this gate is not a replacement for a tool sandbox.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from .input_bindings import validate_input_bindings

STAGES = ("Observe", "Diagnose", "Plan", "Compile", "Act", "Verify", "Evaluate")
DECISIONS = frozenset(
    {"continue", "replan", "sleep_until", "human_required", "achieved", "stopped"}
)
RISK_LEVELS = frozenset({"R0", "R1", "R2", "R3"})


class WorkflowValidationError(ValueError):
    """A workflow cannot be safely compiled."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _bounded_int(value: Any, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise WorkflowValidationError(f"{name} must be an integer in 1..{maximum}")
    return value


def validate_workflow(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a bounded DAG, rejecting ambiguous IDs and dependency cycles."""
    raw = workflow.get("nodes", [])
    if not isinstance(raw, list) or not 1 <= len(raw) <= 128:
        raise WorkflowValidationError("workflow.nodes must contain 1..128 nodes")
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise WorkflowValidationError("node must be an object")
        node_id = item.get("id")
        if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > 160:
            raise WorkflowValidationError("node.id must be a non-empty string of at most 160 chars")
        if node_id in seen:
            raise WorkflowValidationError(f"duplicate node: {node_id}")
        seen.add(node_id)
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(value, str) for value in dependencies
        ):
            raise WorkflowValidationError("depends_on must be a list of node IDs")
        if len(set(dependencies)) != len(dependencies):
            raise WorkflowValidationError("duplicate dependency")
        risk = item.get("risk", "R2")
        if not isinstance(risk, str) or risk not in RISK_LEVELS:
            raise WorkflowValidationError("risk must be R0, R1, R2, or R3")
        resources = item.get("resources", [])
        if not isinstance(resources, list) or any(
            not isinstance(resource, str) or not resource for resource in resources
        ):
            raise WorkflowValidationError("resources must be non-empty strings")
        node = dict(item)
        node.update(
            id=node_id, depends_on=sorted(dependencies), risk=risk, resources=sorted(set(resources))
        )
        node["timeout_seconds"] = _bounded_int(
            item.get("timeout_seconds"), "node.timeout_seconds", 300, 86400
        )
        node["max_output_tokens"] = _bounded_int(
            item.get("max_output_tokens"), "node.max_output_tokens", 2048, 8192
        )
        if node.get("skill") == "local.echo":
            node["risk"] = "R0"
        try:
            validate_input_bindings(node.get("input", {}), set(dependencies))
            if node.get("skill") == "component.call":
                from .component_calls import validate_component_call

                validate_component_call(node)
        except ValueError as exc:
            raise WorkflowValidationError(str(exc)) from exc
        # JSON-only data is a persistence and Temporal serialization requirement.
        try:
            canonical_json(node)
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("node must contain finite JSON data") from exc
        nodes.append(node)
    by_id = {node["id"]: node for node in nodes}
    pending = set(by_id)
    completed: set[str] = set()
    for node in nodes:
        if any(dependency not in by_id for dependency in node["depends_on"]):
            raise WorkflowValidationError(f"unknown dependency in {node['id']}")
    while pending:
        ready = {key for key in pending if set(by_id[key]["depends_on"]) <= completed}
        if not ready:
            raise WorkflowValidationError("workflow dependency cycle")
        completed.update(ready)
        pending.difference_update(ready)
    return nodes


def compile_workflow(task: dict[str, Any]) -> dict[str, Any]:
    """Freeze a plan; mutable controls are deliberately excluded from its digest."""
    for key in ("tenant_id", "task_id", "goal"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise WorkflowValidationError(f"{key} must be a non-empty string")
    workflow = task.get("workflow", {})
    if not isinstance(workflow, dict):
        raise WorkflowValidationError("workflow must be an object")
    plan: dict[str, Any] = {
        "nodes": validate_workflow(workflow),
        "max_cycles": _bounded_int(workflow.get("max_cycles"), "max_cycles", 1, 32),
        "max_parallel": _bounded_int(workflow.get("max_parallel"), "max_parallel", 4, 16),
        "max_actions": _bounded_int(workflow.get("max_actions"), "max_actions", 128, 4096),
        "max_tokens": _bounded_int(workflow.get("max_tokens"), "max_tokens", 32768, 2000000),
        "timeout_seconds": _bounded_int(
            workflow.get("timeout_seconds"), "timeout_seconds", 3600, 604800
        ),
    }
    identity = {key: task[key] for key in ("tenant_id", "task_id", "goal")}
    # Success criteria and operating mode may change authorization semantics.
    identity.update(
        acceptance=task.get("acceptance", {}),
        constraints=task.get("constraints", {}),
        mode=task.get("mode", "local"),
        plan=plan,
    )
    plan["digest"] = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    return plan


def plan_digest(task: dict[str, Any]) -> str:
    return str(compile_workflow(task)["digest"])


def execution_key(task: dict[str, Any], digest: str, cycle: int, node_id: str) -> str:
    identity = [task["tenant_id"], task["task_id"], digest, cycle, node_id]
    return hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must have a timezone")
    return parsed.astimezone(UTC)


def authorized(
    node: dict[str, Any], task: dict[str, Any], checkpoint: dict[str, Any], *, now: datetime
) -> bool:
    """R0 reads pass; writes need an exact approval or a bounded, unexpired mandate.

    R3 is disabled until a real dual-control and MFA implementation exists.
    """
    if node["risk"] == "R0":
        return True
    if node["risk"] == "R3":
        return False
    control = task.get("control", {})
    if not isinstance(control, dict):
        return False
    approvals, mandates = control.get("approvals", []), control.get("mandates", [])
    if not isinstance(approvals, list) or not isinstance(mandates, list):
        return False
    for grant in approvals + mandates:
        if not isinstance(grant, dict):
            continue
        if any(grant.get(key) != task[key] for key in ("tenant_id", "task_id")):
            continue
        if grant.get("plan_digest") != checkpoint["plan_digest"]:
            continue
        scope = grant.get("scope")
        if scope not in {"local", "cloud"}:
            continue
        if task.get("mode", "local") in {"local", "offline"} and scope != "local":
            continue
        if grant.get("approved") is not True:
            continue
        if "expires_at" in grant:
            try:
                if parse_utc(grant["expires_at"]) <= now:
                    continue
            except (TypeError, ValueError):
                continue
        if grant.get("node_id") == node["id"] and grant.get("cycle") == checkpoint["cycle"]:
            return True
        if (
            grant.get("kind") == "mandate"
            and "expires_at" in grant
            and node["id"] in grant.get("node_ids", [])
            and node["risk"] in grant.get("risk_levels", [])
            and isinstance(grant.get("max_actions"), int)
            and not isinstance(grant["max_actions"], bool)
            and checkpoint["actions"] < grant["max_actions"]
        ):
            return True
    return False


def ready_batch(plan: dict[str, Any], results: dict[str, Any]) -> list[dict[str, Any]]:
    """Schedule independent nodes without overlapping declared write resources."""
    ready: list[dict[str, Any]] = []
    locked: set[str] = set()
    reads: set[str] = set()
    for node in plan["nodes"]:
        if node["id"] in results or any(
            dependency not in results for dependency in node["depends_on"]
        ):
            continue
        resources = set(node["resources"])
        if resources & locked or (node["risk"] != "R0" and resources & reads):
            continue
        if node["risk"] != "R0":
            # An unspecified write resource is treated as task-wide exclusive.
            if not resources and ready:
                continue
            if any(item["risk"] != "R0" and not item["resources"] for item in ready):
                continue
            locked.update(resources)
        reads.update(resources)
        ready.append(node)
        if len(ready) == plan["max_parallel"] or (node["risk"] != "R0" and not resources):
            break
    return ready


def token_reservation(
    node: dict[str, Any],
    task: dict[str, Any],
    results: dict[str, Any],
    checkpoint: dict[str, Any] | None = None,
) -> int:
    """Conservative UTF-8 byte estimate plus output cap and message overhead.

    This is a reservation, not measured provider usage. Hidden provider context
    can only be known from its returned usage and is accounted for afterwards.
    """
    if node.get("skill") in {"local.echo", "component.call"}:
        return 0
    context = {
        "goal": task["goal"],
        "input": node.get("input", {}),
        "behavior_snapshot": node.get("behavior_snapshot", {}),
        "dependency_results": {key: results[key] for key in node["depends_on"]},
        "observation": (checkpoint or {}).get("observation", {}),
        "diagnosis": (checkpoint or {}).get("diagnosis", {}),
    }
    estimated_input = len(canonical_json(context).encode("utf-8")) + 1024
    return estimated_input + int(node["max_output_tokens"])
