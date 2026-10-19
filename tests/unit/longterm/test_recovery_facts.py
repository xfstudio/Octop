"""Crash recovery and independent acceptance evidence must survive real boundaries."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from octop.infra.longterm.adapters import tenant_namespace
from octop.infra.longterm.execution import execution_key
from octop.infra.longterm.facts import read_fact_report
from octop.infra.longterm.recovery import recover_execution
from octop.infra.longterm.runtime import LongTermRuntime


def task():
    return {
        "tenant_id": "tenant",
        "space_id": "space",
        "task_id": "task",
        "plan_digest": "digest",
        "checkpoint": {"cycle": 1, "phase": "Act", "actions": 0, "node_results": {}},
    }


def event(kind, *, cycle=1, risk="R0", node="read", **data):
    return {
        "event_type": kind,
        "payload": {
            "plan_digest": "digest",
            "cycle": cycle,
            "node_id": node,
            "risk": risk,
            **data,
        },
    }


def test_confirmed_results_restore_without_repeating_completed_node():
    value = task()
    events = [
        event("action_intent", token_reserved=100),
        event(
            "action_completed",
            status="succeeded",
            result={"output": "saved"},
            token_measured=12,
            token_charge=12,
        ),
    ]
    restored, error = recover_execution(value, events)
    assert error is None
    assert restored["node_results"]["read"] == {
        "status": "succeeded",
        "result": {"output": "saved"},
    }
    assert restored["actions"] == 1
    assert restored["token_usage"]["budget_consumed"] == 12
    assert not value["checkpoint"]["node_results"]


def test_each_crashed_read_attempt_consumes_budget_even_with_same_stable_key():
    value = task()
    key = execution_key(value, "digest", 1, "read")
    events = [
        event("action_intent", idempotency_key=key, token_reserved=100),
        event("action_intent", idempotency_key=key, token_reserved=100),
        event(
            "action_completed",
            idempotency_key=key,
            status="succeeded",
            token_measured=12,
            token_charge=12,
        ),
    ]
    restored, error = recover_execution(value, events)
    assert error is None
    assert restored["actions"] == 2
    assert restored["token_usage"] == {
        "total_tokens": None,
        "known_total_tokens": 12,
        "unknown_actions": 1,
        "budget_consumed": 112,
    }
    value["checkpoint"] = restored
    assert recover_execution(value, events)[0] == restored


def test_cycles_do_not_reuse_previous_result_and_undispatched_intents_cost_nothing():
    value = task()
    value["checkpoint"]["cycle"] = 2
    events = [
        event("action_intent", token_reserved=100),
        event(
            "action_completed",
            status="succeeded",
            result={"old": True},
            token_measured=10,
            token_charge=10,
        ),
        event("action_intent", cycle=2, token_reserved=100),
        event("action_not_dispatched", cycle=2),
    ]
    restored, error = recover_execution(value, events)
    assert error is None
    assert restored["node_results"] == {}
    assert restored["actions"] == 1
    assert restored["token_usage"]["budget_consumed"] == 10


@pytest.mark.parametrize("completed", [False, True])
def test_unknown_or_failed_writes_are_never_replayed(completed):
    events = [event("action_intent", risk="R2", token_reserved=100)]
    if completed:
        events.append(event("action_completed", risk="R2", status="uncertain", token_charge=100))
    restored, error = recover_execution(task(), events)
    assert error == "uncertain"
    assert restored["token_usage"]["budget_consumed"] == 100


def report(value, now):
    return {
        "schema_version": 1,
        **{field: value[field] for field in ("tenant_id", "space_id", "task_id", "plan_digest")},
        "cycle": value["checkpoint"]["cycle"],
        "observed_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=1)).isoformat(),
        "facts": {"count": 3},
        "quality": "fit",
        "evidence_refs": ["provider:readback:1"],
        "goal_outcome": "achieved",
        "readback_confirmed": True,
        "observation_window_complete": True,
    }


def save_report(root, value, payload):
    path = (
        root
        / tenant_namespace(value["tenant_id"])
        / value["space_id"]
        / (value["task_id"] + ".json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other"),
        ("space_id", "other"),
        ("task_id", "other"),
        ("plan_digest", "other"),
        ("cycle", 2),
        ("cycle", True),
        ("readback_confirmed", False),
        ("observation_window_complete", False),
        ("evidence_refs", []),
        ("quality", "unfit"),
        ("facts", []),
        ("expires_at", "2020-01-01T00:00:00Z"),
        ("observed_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_reports_reject_wrong_identity_stale_or_immature_facts(tmp_path, field, value):
    current, now = task(), datetime.now(UTC)
    payload = report(current, now)
    payload[field] = value
    save_report(tmp_path, current, payload)
    with pytest.raises(ValueError):
        read_fact_report(tmp_path, current, now=now)


def test_readback_must_follow_execution_and_match_frozen_acceptance(tmp_path):
    current, now = task(), datetime.now(UTC)
    current["checkpoint"].update(phase="Verify", last_action_completed_at=now.isoformat())
    payload = report(current, now)
    save_report(tmp_path, current, payload)
    with pytest.raises(ValueError, match="predates"):
        read_fact_report(tmp_path, current, now=now)
    payload["observed_at"] = now.isoformat()
    current["acceptance"] = {"scope": {"store": "one"}, "currency": "CNY", "metric_revision": "v2"}
    save_report(tmp_path, current, payload)
    with pytest.raises(ValueError, match="acceptance"):
        read_fact_report(tmp_path, current, now=now)
    payload.update(current["acceptance"])
    save_report(tmp_path, current, payload)
    assert read_fact_report(tmp_path, current, now=now)["facts"] == {"count": 3}


def test_declared_observation_window_must_be_mature(tmp_path):
    current, now = task(), datetime.now(UTC)
    current["acceptance"] = {"observation_window_seconds": 60}
    payload = report(current, now)
    payload["observation_window"] = {
        "started_at": (now - timedelta(seconds=40)).isoformat(),
        "ended_at": (now - timedelta(seconds=2)).isoformat(),
    }
    save_report(tmp_path, current, payload)
    with pytest.raises(ValueError, match="not mature"):
        read_fact_report(tmp_path, current, now=now)
    payload["observation_window"]["started_at"] = (now - timedelta(seconds=100)).isoformat()
    save_report(tmp_path, current, payload)
    assert read_fact_report(tmp_path, current, now=now)["goal_outcome"] == "achieved"


async def test_independent_reports_can_complete_seven_stage_success(tmp_path):
    value = task()
    value.update(goal="verify", workflow={"nodes": [{"id": "echo", "skill": "local.echo"}]})
    value.pop("checkpoint")

    async def facts(current, state):
        scoped = {**current, "checkpoint": state}
        now = datetime.now(UTC)
        payload = report(scoped, now)
        payload["observed_at"] = now.isoformat()
        save_report(tmp_path, scoped, payload)
        return read_fact_report(tmp_path, scoped)

    async def evaluate(current, state):
        return {"decision": "achieved", "goal_outcome": state["verification"]["goal_outcome"]}

    runtime = LongTermRuntime(observer=facts, verifier=facts, evaluator=evaluate)
    state = None
    # Runtime compiles a digest containing the actual task and normalized nodes.
    from octop.infra.longterm.execution import plan_digest

    value["plan_digest"] = plan_digest(value)
    for _ in range(7):
        outcome = await runtime.advance(value, state)
        state = copy.deepcopy(outcome.checkpoint)
    assert outcome.status == "completed"
    assert outcome.result["goal_outcome"] == "achieved"
    assert outcome.result["execution_health"] == "succeeded"
