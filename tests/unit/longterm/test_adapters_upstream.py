from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from octop.infra.longterm.adapters import (
    BeatDesignAdapter,
    HindsightAdapter,
    IntegrationUnavailable,
    RnaAdapter,
    SemanticaAdapter,
    tenant_namespace,
)
from octop.infra.longterm.beatdesign_workflow import import_beatdesign_workflow
from octop.infra.longterm.execution import WorkflowValidationError
from octop.infra.longterm.temporal_runtime import (
    start_task,
    wake_task,
    workflow_id,
    workflow_runner,
)


async def test_hindsight_retain_uses_real_async_sdk_contract_and_tenant_bank():
    client = SimpleNamespace(aretain=AsyncMock(return_value={"success": True}))
    now = datetime(2026, 10, 19, tzinfo=UTC)
    adapter = HindsightAdapter("http://memory.invalid", client=client, clock=lambda: now)
    result = await adapter.retain(
        tenant_id="tenant-a",
        task_id="goal-a",
        content="Verified lesson",
        evidence_refs=["evidence:1"],
        verified=True,
        ttl_seconds=60,
        consent=True,
    )
    request = client.aretain.await_args.kwargs
    assert request["bank_id"] == adapter.bank_id("tenant-a")
    assert request["bank_id"] != adapter.bank_id("tenant-b")
    assert request["metadata"]["octop_tenant"] == tenant_namespace("tenant-a")
    assert request["retain_async"] is False
    assert result["expires_at"] == (now + timedelta(seconds=60)).isoformat()
    assert result["ttl_enforcement"] == "adapter_read_filter"


@pytest.mark.parametrize(
    "override", [{"verified": False}, {"evidence_refs": []}, {"ttl_seconds": 0}, {"consent": False}]
)
async def test_hindsight_does_not_retain_unqualified_memories(override):
    client = SimpleNamespace(aretain=AsyncMock())
    adapter = HindsightAdapter("http://memory.invalid", client=client)
    args = {
        "tenant_id": "a",
        "task_id": "t",
        "content": "data",
        "evidence_refs": ["e:1"],
        "verified": True,
        "ttl_seconds": 60,
        "consent": True,
    }
    args.update(override)
    with pytest.raises(ValueError):
        await adapter.retain(**args)
    client.aretain.assert_not_awaited()


async def test_hindsight_recall_filters_expired_unscoped_and_metadata_free_results():
    now = datetime(2026, 10, 19, tzinfo=UTC)
    metadata = {
        "octop_tenant": tenant_namespace("a"),
        "octop_expires_at": (now + timedelta(seconds=1)).isoformat(),
        "octop_evidence_refs": '["e:1"]',
    }
    rows = [
        {"id": "good", "document_id": "octop-good", "metadata": metadata},
        {
            "id": "old",
            "document_id": "octop-old",
            "metadata": {**metadata, "octop_expires_at": now.isoformat()},
        },
        {"id": "other", "metadata": {**metadata, "octop_tenant": tenant_namespace("b")}},
        {"id": "observation", "metadata": {}},
    ]
    client = SimpleNamespace(
        arecall=AsyncMock(return_value={"results": rows}),
        documents=SimpleNamespace(delete_document=AsyncMock()),
    )
    adapter = HindsightAdapter("http://memory.invalid", client=client, clock=lambda: now)
    result = await adapter.recall(tenant_id="a", query="lesson")
    assert [item["id"] for item in result["results"]] == ["good"]
    assert result["expired_document_ids"] == ["octop-old"]
    assert client.arecall.await_args.kwargs["types"] == ["world", "experience"]
    assert client.arecall.await_args.kwargs["tags_match"] == "all_strict"
    assert await adapter.delete_expired(tenant_id="a", recall_result=result) == 1
    client.documents.delete_document.assert_awaited_once_with(adapter.bank_id("a"), "octop-old")
    with pytest.raises(ValueError, match="bank mismatch"):
        await adapter.delete_expired(tenant_id="b", recall_result=result)


async def test_semantica_confirms_readback_and_uses_tenant_specific_storage(tmp_path):
    paths, records = [], {}

    class Manager:
        def __init__(self, *, storage_path):
            paths.append(storage_path)

        def track_entity(self, **kwargs):
            records[kwargs["entity_id"]] = kwargs
            return object()

        def get_provenance(self, entity_id):
            return records.get(entity_id)

    adapter = SemanticaAdapter(tmp_path, manager_factory=Manager)
    first = await adapter.record(
        {"tenant_id": "a", "task_id": "t", "phase": "Act"}, evidence_ref="ledger:1"
    )
    second = await adapter.record(
        {"tenant_id": "b", "task_id": "t", "phase": "Act"}, evidence_ref="ledger:2"
    )
    assert paths[0] != paths[1]
    assert first["indexed"] is True
    assert first["entity_id"] != second["entity_id"]
    assert records[first["entity_id"]]["source"] == "ledger:1"


async def test_semantica_swallowed_storage_failure_is_not_reported_as_indexed(tmp_path):
    class BrokenManager:
        def __init__(self, **kwargs):
            pass

        def track_entity(self, **kwargs):
            return None

        def get_provenance(self, entity_id):
            return None

    with pytest.raises(IntegrationUnavailable, match="not confirmed"):
        await SemanticaAdapter(tmp_path, manager_factory=BrokenManager).record(
            {"tenant_id": "a", "task_id": "t"}, evidence_ref="ledger:1"
        )


async def test_rna_uses_actual_gene_api_and_requires_explicit_review():
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"revision": 3})
    client = SimpleNamespace(request=AsyncMock(return_value=response))
    adapter = RnaAdapter("http://rna.invalid", {"a": "project-a"}, http_client=client)
    await adapter.genes("a")
    assert client.request.await_args.args == (
        "GET",
        "http://rna.invalid/api/v2/projects/project-a/genes",
    )
    await adapter.observability("a")
    assert client.request.await_args.args[1].endswith("/genes/observability")
    with pytest.raises(ValueError, match="explicit"):
        await adapter.review_gene("a", "lesson:1", "confirm")
    await adapter.review_gene(
        "a", "lesson:1", "rewrite", text="New wording", explicitly_authorized=True
    )
    request = client.request.await_args
    assert request.args[1].endswith("/genes/lesson%3A1")
    assert request.kwargs["json"] == {"action": "rewrite", "text": "New wording"}


async def test_beatdesign_rejects_cross_tenant_or_read_disguised_write_before_network():
    adapter = BeatDesignAdapter("http://design.invalid/mcp", {"a": "project-a"})
    with pytest.raises(ValueError, match="cross-tenant"):
        await adapter.call_tool("a", "bdesign_canvas_get", {"projectId": "project-b"})
    with pytest.raises(ValueError, match="catalog"):
        await adapter.call_tool("a", "bdesign_project_list", {})
    with pytest.raises(ValueError, match="R0"):
        await adapter.execute(
            {"skill": "beatdesign.bdesign_canvas_apply", "risk": "R0"}, {"tenant_id": "a"}
        )


def test_beatdesign_import_uses_real_card_prompt_and_explicit_dependencies_only():
    result = import_beatdesign_workflow(
        {
            "revision": 3,
            "document": {
                "cards": [
                    {
                        "id": "card-a",
                        "prompt": '```octop-execution\n{"id":"a","skill":"agent.analyze","risk":"R0","depends_on":[]}\n```',
                        "referenceCardIds": ["card-b"],
                    },
                    {
                        "id": "card-b",
                        "prompt": '```octop-execution\n{"id":"b","skill":"agent.analyze","risk":"R0","depends_on":["a"]}\n```',
                    },
                ],
                "edges": [{"source": "card-b", "target": "card-a"}],
            },
        }
    )
    assert result["nodes"][0]["depends_on"] == []
    assert result["nodes"][1]["depends_on"] == ["a"]
    assert result["nodes"][0]["beatdesign_card_id"] == "card-a"


def test_beatdesign_import_requires_explicit_semantic_manifest():
    with pytest.raises(WorkflowValidationError):
        import_beatdesign_workflow({"cards": [{"id": "card-a", "prompt": "A normal media prompt"}]})
    with pytest.raises(WorkflowValidationError, match="explicitly"):
        import_beatdesign_workflow(
            {
                "cards": [{"id": "a"}],
                "octop_execution": {
                    "schema_version": 1,
                    "nodes": [{"card_id": "a", "id": "a", "skill": "agent.analyze"}],
                },
            }
        )
    result = import_beatdesign_workflow(
        {
            "cards": [{"id": "a"}],
            "octop_execution": {
                "schema_version": 1,
                "nodes": [{"card_id": "a", "id": "a", "skill": "agent.analyze", "depends_on": []}],
            },
        }
    )
    assert result["nodes"][0]["risk"] == "R2"


async def test_temporal_client_uses_tenant_derived_ids_and_real_registered_names():
    client = SimpleNamespace(
        start_workflow=AsyncMock(return_value=SimpleNamespace(id="workflow-id")),
        get_workflow_handle=lambda identity: SimpleNamespace(signal=AsyncMock()),
    )
    assert workflow_id("a", "t") != workflow_id("b", "t")
    result = await start_task(client, tenant_id="a", task_id="t")
    assert result == "workflow-id"
    assert client.start_workflow.await_args.args == (
        "octop.longterm.goal",
        {"tenant_id": "a", "task_id": "t"},
    )
    assert client.start_workflow.await_args.kwargs["id"] == workflow_id("a", "t")
    await wake_task(client, tenant_id="a", task_id="t")


async def test_temporal_definition_passes_sdk_sandbox_validation():
    pytest.importorskip("temporalio")
    from temporalio import workflow

    from octop.infra.longterm.temporal_workflow import LongTermTaskWorkflow

    definition = workflow._Definition.must_from_class(LongTermTaskWorkflow)
    workflow_runner().prepare_workflow(definition)
