from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest

from octop.infra.longterm.execution import WorkflowValidationError, compile_workflow, plan_digest
from octop.infra.longterm.runtime import LongTermRuntime


def task(nodes=None, **workflow):
    return {
        "tenant_id": "tenant-a",
        "task_id": "goal-a",
        "goal": "Check the result",
        "mode": "offline",
        "workflow": {
            "nodes": nodes or [{"id": "read", "skill": "local.echo", "input": {"value": 1}}],
            **workflow,
        },
    }


async def advance_to_act(runtime, value):
    checkpoint = None
    for _ in range(4):
        result = await runtime.advance(value, checkpoint)
        assert result.status == "running"
        checkpoint = result.checkpoint
    assert checkpoint["phase"] == "Act"
    return checkpoint


async def finish(runtime, value, checkpoint=None):
    for _ in range(40):
        result = await runtime.advance(value, checkpoint)
        checkpoint = result.checkpoint
        if result.status != "running":
            return result
    raise AssertionError("runtime did not stop within the bound")


def approval(value, *, node="write", cycle=1, scope="local"):
    return {
        "tenant_id": value["tenant_id"],
        "task_id": value["task_id"],
        "plan_digest": plan_digest(value),
        "approved": True,
        "node_id": node,
        "cycle": cycle,
        "scope": scope,
    }


@pytest.mark.parametrize(
    "nodes",
    [
        [{"id": "a"}, {"id": "a"}],
        [{"id": "a", "depends_on": ["missing"]}],
        [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}],
        [{"id": "a", "risk": "unclassified"}],
    ],
)
def test_compilation_rejects_ambiguous_or_cyclic_workflows(nodes):
    with pytest.raises(WorkflowValidationError):
        compile_workflow(task(nodes))


def test_digest_binds_goal_tenant_plan_and_preserves_controls():
    value = task()
    original = plan_digest(value)
    value["control"] = {"paused": True, "approvals": []}
    assert plan_digest(value) == original
    value["tenant_id"] = "tenant-b"
    assert plan_digest(value) != original
    value["tenant_id"] = "tenant-a"
    value["workflow"]["nodes"][0]["input"] = {"value": 2}
    assert plan_digest(value) != original


async def test_seven_stages_complete_offline_without_claiming_goal_success():
    runtime, value = LongTermRuntime(), task()
    checkpoint, phases = None, []
    for _ in range(7):
        result = await runtime.advance(value, checkpoint)
        phases.append(result.checkpoint["phase"])
        checkpoint = result.checkpoint
    assert phases == ["Diagnose", "Plan", "Compile", "Act", "Verify", "Evaluate", "Evaluate"]
    assert result.status == "completed"
    assert result.result["execution_health"] == "succeeded"
    assert result.result["goal_outcome"] == "inconclusive"
    assert result.result["decision"] == "stopped"


async def test_checkpoint_identity_and_plan_must_match():
    runtime, value = LongTermRuntime(), task()
    first = await runtime.advance(value)
    changed = copy.deepcopy(value)
    changed["tenant_id"] = "tenant-b"
    with pytest.raises(WorkflowValidationError, match="identity"):
        await runtime.advance(changed, first.checkpoint)
    changed = copy.deepcopy(value)
    changed["goal"] = "Different goal"
    with pytest.raises(WorkflowValidationError, match="digest"):
        await runtime.advance(changed, first.checkpoint)


async def test_parallel_independent_nodes_and_dependency_readback():
    running, peak, seen = 0, 0, []

    async def execute(node, scoped):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        seen.append((node["id"], scoped["execution"]))
        running -= 1
        return {"output": node["id"]}

    value = task(
        [
            {"id": "a", "risk": "R0"},
            {"id": "b", "risk": "R0"},
            {"id": "c", "risk": "R0", "depends_on": ["a", "b"]},
        ],
        max_parallel=2,
    )
    runtime = LongTermRuntime(execute)
    checkpoint = await advance_to_act(runtime, value)
    first = await runtime.advance(value, checkpoint)
    assert peak == 2
    assert set(first.checkpoint["node_results"]) == {"a", "b"}
    second = await runtime.advance(value, first.checkpoint)
    assert second.checkpoint["phase"] == "Verify"
    assert set(seen[-1][1]["dependency_results"]) == {"a", "b"}
    assert len({item[1]["idempotency_key"] for item in seen}) == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda grant: grant.update(tenant_id="other"),
        lambda grant: grant.update(plan_digest="old"),
        lambda grant: grant.update(cycle=2),
        lambda grant: grant.update(scope="cloud"),
    ],
)
async def test_write_approvals_are_exact_and_offline_cannot_consume_cloud(mutate):
    value = task([{"id": "write", "risk": "R1"}])
    grant = approval(value)
    mutate(grant)
    value["control"] = {"approvals": [grant]}
    runtime = LongTermRuntime()
    checkpoint = await advance_to_act(runtime, value)
    result = await runtime.advance(value, checkpoint)
    assert result.status == "waiting_approval"
    assert result.checkpoint["actions"] == 0


async def test_write_intent_is_durable_before_dispatch_and_missing_sink_fails_closed():
    history = []

    async def write(event):
        history.append(event["kind"])

    async def execute(node, scoped):
        assert history == ["action_intent"]
        return {"ok": True}

    value = task([{"id": "write", "risk": "R1"}])
    value["control"] = {"approvals": [approval(value)]}
    absent = LongTermRuntime(execute)
    result = await absent.advance(value, await advance_to_act(absent, value))
    assert result.status == "failed"
    assert result.result["reason"] == "durable_evidence_writer_required"
    runtime = LongTermRuntime(execute, evidence_writer=write)
    result = await runtime.advance(value, await advance_to_act(runtime, value))
    assert result.status == "running"
    assert history == ["action_intent", "action_completed"]


async def test_revocation_is_checked_again_at_outbound_boundary():
    called = False

    async def write(event):
        pass

    async def execute(node, scoped):
        nonlocal called
        called = True
        return {}

    async def revoked(value, checkpoint):
        return {"approvals": []}

    value = task([{"id": "write", "risk": "R2"}])
    value["control"] = {"approvals": [approval(value)]}
    runtime = LongTermRuntime(execute, evidence_writer=write, control_checker=revoked)
    result = await runtime.advance(value, await advance_to_act(runtime, value))
    assert result.status == "waiting_approval"
    assert called is False
    assert result.events[-1]["kind"] == "action_not_dispatched"


async def test_write_transport_failure_is_uncertain_and_never_replayed():
    calls = 0

    async def write(event):
        pass

    async def execute(node, scoped):
        nonlocal calls
        calls += 1
        raise ConnectionError("request might already have committed")

    value = task([{"id": "write", "risk": "R2"}])
    value["control"] = {"approvals": [approval(value)]}
    runtime = LongTermRuntime(execute, evidence_writer=write)
    result = await runtime.advance(value, await advance_to_act(runtime, value))
    assert result.status == "uncertain"
    resumed = await runtime.advance(value, result.checkpoint)
    assert resumed.status == "uncertain"
    assert calls == 1
    assert "might already" not in str(result.to_dict())


async def test_R3_cannot_be_enabled_by_an_ordinary_approval():
    value = task([{"id": "write", "risk": "R3"}])
    value["control"] = {"approvals": [approval(value)]}
    result = await finish(LongTermRuntime(), value)
    assert result.status == "failed"
    assert result.result["reason"] == "R3_disabled"


async def test_goal_success_requires_independent_observation_and_verification():
    async def evaluate(value, checkpoint):
        return {"decision": "achieved", "goal_outcome": "achieved"}

    async def verify(value, checkpoint):
        return {"goal_outcome": "achieved", "evidence_refs": ["readback:1"]}

    async def observe(value, checkpoint):
        return {"evidence_refs": ["baseline:1"]}

    missing = await finish(LongTermRuntime(evaluator=evaluate, verifier=verify), task())
    assert missing.result["goal_outcome"] == "inconclusive"
    present = await finish(
        LongTermRuntime(evaluator=evaluate, verifier=verify, observer=observe), task()
    )
    assert present.result["goal_outcome"] == "achieved"


async def test_pause_resume_cancel_and_cycle_budget_are_bounded():
    value, runtime = task(), LongTermRuntime()
    value["control"] = {"paused": True}
    paused = await runtime.advance(value)
    assert paused.status == "paused"
    assert paused.checkpoint["phase"] == "Observe"
    value["control"] = {"cancelled": True}
    cancelled = await runtime.advance(value, paused.checkpoint)
    assert cancelled.status == "cancelled"

    async def keep_running(value, checkpoint):
        return {"decision": "continue"}

    result = await finish(LongTermRuntime(evaluator=keep_running), task(max_cycles=2))
    assert result.status == "completed"
    assert result.checkpoint["cycle"] == 2
    assert result.checkpoint["actions"] == 2
    assert result.result["goal_outcome"] == "inconclusive"


async def test_sleeping_does_not_execute_early_and_wakes_in_next_cycle():
    now = datetime(2026, 10, 19, tzinfo=UTC)

    async def sleep(value, checkpoint):
        return {"decision": "sleep_until", "sleep_until": (now + timedelta(minutes=1)).isoformat()}

    runtime = LongTermRuntime(evaluator=sleep, clock=lambda: now)
    value = task(max_cycles=2)
    result = await finish(runtime, value)
    assert result.status == "sleeping"
    still = await runtime.advance(value, result.checkpoint)
    assert still.status == "sleeping"
    assert still.checkpoint["cycle"] == 2
    now += timedelta(minutes=2)
    awake = await runtime.advance(value, still.checkpoint)
    assert awake.status == "running"
    assert awake.checkpoint["phase"] == "Diagnose"


async def test_elapsed_wall_budget_stops_before_any_action():
    now = datetime(2026, 10, 19, tzinfo=UTC)
    runtime, value = LongTermRuntime(clock=lambda: now), task(timeout_seconds=1)
    result = await runtime.advance(value)
    now += timedelta(seconds=2)
    expired = await runtime.advance(value, result.checkpoint)
    assert expired.status == "failed"
    assert expired.checkpoint["actions"] == 0
    assert expired.result["reason"] == "wall_time_budget_exhausted"


async def test_observed_facts_reach_agents_and_are_included_in_dispatch_budget():
    calls = []

    async def observe(value, checkpoint):
        return {"facts": {"inventory": 8}, "quality": "fit", "evidence_refs": ["source:1"]}

    async def execute(node, scoped):
        calls.append(scoped["execution"])
        return {"usage": {"total_tokens": 1}}

    value = task([{"id": "analyze", "risk": "R0"}])
    runtime = LongTermRuntime(execute, observer=observe)
    result = await runtime.advance(value, await advance_to_act(runtime, value))
    assert calls[0]["observation"]["facts"]["inventory"] == 8
    assert calls[0]["diagnosis"]["evidence_refs"] == ["source:1"]
    assert result.status == "running"

    async def large_observe(value, checkpoint):
        return {"facts": {"large": "x" * 10000}, "evidence_refs": ["source:2"]}

    value["workflow"]["max_tokens"] = 9000
    blocked = LongTermRuntime(execute, observer=large_observe)
    result = await blocked.advance(value, await advance_to_act(blocked, value))
    assert result.status == "failed"
    assert result.result["reason"] == "token_budget_exhausted"
    assert len(calls) == 1


async def test_human_pause_resume_refreshes_facts_in_a_bounded_cycle():
    seen = []

    async def observe(value, checkpoint):
        seen.append(checkpoint["cycle"])
        return {"facts": {"revision": len(seen)}, "evidence_refs": ["source:new"]}

    async def evaluate(value, checkpoint):
        return {"decision": "human_required" if checkpoint["cycle"] == 1 else "stopped"}

    value = task(max_cycles=2)
    runtime = LongTermRuntime(observer=observe, evaluator=evaluate)
    paused = await finish(runtime, value)
    assert paused.status == "paused"
    resumed = await runtime.advance(value, paused.checkpoint)
    assert resumed.status == "running"
    assert resumed.checkpoint["cycle"] == 2
    assert seen == [1, 2]
    assert (await finish(runtime, value, resumed.checkpoint)).status == "completed"


async def test_token_reservation_stops_before_provider_call():
    calls = 0

    async def execute(node, scoped):
        nonlocal calls
        calls += 1
        return {"usage": {"total_tokens": 1}}

    value = task([{"id": "model", "risk": "R0", "max_output_tokens": 100}], max_tokens=100)
    runtime = LongTermRuntime(execute)
    result = await runtime.advance(value, await advance_to_act(runtime, value))
    assert result.status == "failed"
    assert result.result["reason"] == "token_budget_exhausted"
    assert calls == 0


async def test_task_wall_budget_interrupts_an_inflight_read():
    async def execute(node, scoped):
        await asyncio.sleep(10)
        return {}

    value = task([{"id": "slow", "risk": "R0", "timeout_seconds": 60}], timeout_seconds=1)
    runtime = LongTermRuntime(execute)
    result = await runtime.advance(value, await advance_to_act(runtime, value))
    assert result.status == "failed"
    assert result.checkpoint["node_results"]["slow"]["error_type"] == "TimeoutError"


async def test_token_accounting_keeps_unknown_separate_from_measured_zero():
    async def execute(node, scoped):
        return (
            {"usage": {"total_tokens": 12}}
            if node["id"] == "measured"
            else {"output": "usage missing"}
        )

    value = task(
        [{"id": "measured", "risk": "R0"}, {"id": "unknown", "risk": "R0"}], max_parallel=1
    )
    runtime = LongTermRuntime(execute)
    checkpoint = await advance_to_act(runtime, value)
    first = await runtime.advance(value, checkpoint)
    assert first.checkpoint["token_usage"]["total_tokens"] == 12
    second = await runtime.advance(value, first.checkpoint)
    usage = second.checkpoint["token_usage"]
    assert usage["total_tokens"] is None
    assert usage["known_total_tokens"] == 12
    assert usage["unknown_actions"] == 1
    assert usage["budget_consumed"] > 12
    assert second.events[-1]["token_measured"] is None


async def test_parallel_batch_cannot_overspend_a_single_action_mandate():
    value = task(
        [
            {"id": "write", "risk": "R1", "resources": ["one"]},
            {"id": "two", "risk": "R1", "resources": ["two"]},
        ]
    )
    grant = approval(value)
    grant.pop("node_id")
    grant.pop("cycle")
    grant.update(
        kind="mandate",
        node_ids=["write", "two"],
        risk_levels=["R1"],
        max_actions=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    )
    value["control"] = {"mandates": [grant]}
    runtime = LongTermRuntime()
    result = await runtime.advance(value, await advance_to_act(runtime, value))
    assert result.status == "waiting_approval"
    assert result.checkpoint["actions"] == 0
