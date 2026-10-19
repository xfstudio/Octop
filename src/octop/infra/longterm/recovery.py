"""Reconstruct committed node outcomes when a worker died before checkpointing."""

from __future__ import annotations

import copy
from typing import Any

from .execution import execution_key


def recover_execution(
    task: dict[str, Any], events: list[dict[str, Any]]
) -> tuple[dict[str, Any], str | None]:
    checkpoint = copy.deepcopy(task.get("checkpoint") or {})
    pending: dict[str, list[dict[str, Any]]] = {}
    # Stable keys identify logical actions, not dispatch attempts. An R0 action
    # can be retried after a crash; every attempt still consumes its reservation.
    attempts: list[dict[str, Any]] = []
    failure: str | None = None
    for event in events:
        kind, payload = event["event_type"], event["payload"]
        if kind not in {"action_intent", "action_completed", "action_not_dispatched"}:
            continue
        if payload.get("plan_digest", task["plan_digest"]) != task["plan_digest"]:
            continue
        node_id = str(payload.get("node_id"))
        key = payload.get("idempotency_key") or execution_key(
            task, task["plan_digest"], payload.get("cycle", 1), node_id
        )
        if kind == "action_intent":
            attempt = {"intent": payload, "outcome": None, "deferred": False}
            attempts.append(attempt)
            pending.setdefault(key, []).append(attempt)
        elif kind == "action_not_dispatched":
            if pending.get(key):
                pending[key].pop()["deferred"] = True
        else:
            matched = pending[key].pop() if pending.get(key) else None
            intent = matched["intent"] if matched else {}
            if matched:
                matched["outcome"] = payload
            if payload.get("status") != "succeeded":
                failure = "uncertain" if intent.get("risk") != "R0" else failure or "failed"
            if checkpoint and payload.get("cycle") == checkpoint.get("cycle"):
                result = {"status": payload["status"]}
                if "result" in payload:
                    result["result"] = payload["result"]
                if "error_type" in payload:
                    result["error_type"] = payload["error_type"]
                checkpoint.setdefault("node_results", {})[node_id] = result
                checkpoint["last_action_completed_at"] = payload.get(
                    "completed_at", checkpoint.get("last_action_completed_at")
                )
    dispatched = [attempt for attempt in attempts if not attempt["deferred"]]
    if checkpoint and dispatched:
        checkpoint["actions"] = max(checkpoint.get("actions", 0), len(dispatched))
        known, unknown, consumed = 0, 0, 0
        for attempt in dispatched:
            intent, outcome = attempt["intent"], attempt["outcome"] or {}
            measured = outcome.get("token_measured")
            if type(measured) is int and measured >= 0:
                known += measured
            else:
                unknown += 1
            consumed += outcome.get("token_charge", intent.get("token_reserved", 0))
        checkpoint["token_usage"] = {
            "total_tokens": None if unknown else known,
            "known_total_tokens": known,
            "unknown_actions": unknown,
            "budget_consumed": max(
                consumed, checkpoint.get("token_usage", {}).get("budget_consumed", 0)
            ),
        }
    if any(
        attempt["intent"].get("risk") != "R0" for group in pending.values() for attempt in group
    ):
        return checkpoint, "uncertain"
    return checkpoint, failure
