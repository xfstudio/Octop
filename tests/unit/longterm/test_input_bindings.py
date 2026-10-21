"""Workflow data links cannot access anything beyond successful dependencies."""

from __future__ import annotations

import copy

import pytest

from octop.infra.longterm.execution import WorkflowValidationError, compile_workflow
from octop.infra.longterm.input_bindings import (
    InputBindingError,
    resolve_input_bindings,
)
from octop.infra.longterm.runtime import LongTermRuntime


def workflow(nodes):
    return {
        "tenant_id": "tenant-link",
        "task_id": "task-link",
        "goal": "Pass data through registered workflow nodes",
        "workflow": {"nodes": nodes},
    }


def echo(node_id, data, dependencies=None):
    return {
        "id": node_id,
        "skill": "local.echo",
        "input": data,
        "risk": "R0",
        "depends_on": dependencies or [],
    }


def test_json_pointer_preserves_json_types_and_does_not_alias_source():
    result = {"status": "succeeded", "result": {"output": {"a/b": {"~value": [7, False]}}}}
    dependencies = {"upstream": result}
    linked = resolve_input_bindings(
        {"value": {"$node": "upstream", "pointer": "/result/output/a~1b/~0value"}},
        dependencies,
    )
    assert linked == {"value": [7, False]}
    linked["value"].append("changed")
    assert result["result"]["output"]["a/b"]["~value"] == [7, False]


@pytest.mark.parametrize(
    "link",
    [
        {"$node": "not-declared"},
        {"$node": "source", "pointer": "https://example.test/value"},
        {"$node": "source", "pointer": "/bad~2escape"},
        {"$node": "source", "pointer": "/result", "extra": "not-a-template"},
    ],
)
def test_invalid_or_undeclared_links_are_rejected_before_plan_creation(link):
    value = workflow([echo("source", {}), echo("target", {"value": link}, ["source"])])
    with pytest.raises(WorkflowValidationError):
        compile_workflow(value)


def test_failed_dependency_cannot_supply_data_and_large_values_are_bounded():
    with pytest.raises(InputBindingError, match="did not succeed"):
        resolve_input_bindings({"$node": "source"}, {"source": {"status": "failed"}})
    with pytest.raises(InputBindingError, match="128 KiB"):
        resolve_input_bindings(
            {"$node": "source"},
            {"source": {"status": "succeeded", "result": {"output": "x" * 131072}}},
        )


def test_aggregate_budget_is_checked_before_copying_all_repeated_links(monkeypatch):
    original_deepcopy = copy.deepcopy
    copied = []

    def track_copy(value):
        copied.append(value)
        return original_deepcopy(value)

    monkeypatch.setattr(copy, "deepcopy", track_copy)
    links = [{"$node": "source", "pointer": "/result"} for _ in range(20)]
    source = {"status": "succeeded", "result": {"text": "x" * 12000}}
    with pytest.raises(InputBindingError, match="128 KiB"):
        resolve_input_bindings(links, {"source": source})
    assert 0 < len(copied) < len(links)


@pytest.mark.parametrize(
    "pointer", ["/result/output/missing", "/result/output/01", "/result/output/-"]
)
def test_missing_keys_and_noncanonical_array_indices_fail(pointer):
    with pytest.raises(InputBindingError):
        resolve_input_bindings(
            {"$node": "source", "pointer": pointer},
            {"source": {"status": "succeeded", "result": {"output": [1]}}},
        )


async def test_three_node_pipeline_passes_outputs_without_mutating_the_frozen_plan():
    value = workflow(
        [
            echo("seed", {"query": "shared component input"}),
            echo("middle", {"$node": "seed", "pointer": "/result/output"}, ["seed"]),
            echo(
                "last",
                {"message": {"$node": "middle", "pointer": "/result/output/query"}},
                ["middle"],
            ),
        ]
    )
    original = copy.deepcopy(value)
    runtime = LongTermRuntime()
    checkpoint = None
    for _ in range(12):
        outcome = await runtime.advance(value, checkpoint)
        checkpoint = outcome.checkpoint
        if outcome.status == "completed":
            break
    assert outcome.status == "completed"
    assert checkpoint["node_results"]["last"]["result"]["output"] == {
        "message": "shared component input"
    }
    assert checkpoint["token_usage"]["budget_consumed"] == 0
    assert value == original


async def test_unresolvable_pointer_stops_before_target_intent_or_dispatch():
    value = workflow(
        [echo("source", {}), echo("target", {"$node": "source", "pointer": "/absent"}, ["source"])]
    )
    events = []

    async def record(event):
        events.append(event)

    runtime = LongTermRuntime(evidence_writer=record)
    checkpoint = None
    for _ in range(10):
        outcome = await runtime.advance(value, checkpoint)
        checkpoint = outcome.checkpoint
        if outcome.status == "failed":
            break
    assert outcome.status == "failed"
    assert outcome.result["reason"] == "node_input_reference_invalid"
    assert not any(event.get("node_id") == "target" for event in events)
