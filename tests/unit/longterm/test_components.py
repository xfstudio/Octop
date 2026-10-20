"""Component configuration is effective, secret-safe and isolated from tenants."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from octop.infra.longterm.adapters import HindsightAdapter
from octop.infra.longterm.components import NAMES, ComponentManager
from octop.infra.longterm.evolution import RnaSdkAdapter
from octop.infra.users.identity import User
from tests.unit.longterm.test_api import (
    HttpControlPlane,
    _echo_task,
    _space,
)
from tests.unit.longterm.test_api import (
    http_plane as http_plane,
)

BASE = "/api/longterm/integrations"


async def test_members_cannot_read_modify_or_probe_server_configuration(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_HINDSIGHT_URL", "https://private-service.example.test")
    monkeypatch.setenv("OCTOP_HINDSIGHT_API_KEY", "synthetic-hidden-credential")
    for path, method, body in [
        ("/hindsight/config", "GET", None),
        ("/hindsight/config", "PUT", {"values": {"enabled": False}}),
        ("/hindsight/check", "POST", None),
    ]:
        response = await http_plane.client.request(
            method, BASE + path, headers=http_plane.owner_auth, json=body
        )
        assert response.status_code == 403, response.text
    response = await http_plane.client.get(BASE + "/catalog", headers=http_plane.owner_auth)
    assert response.status_code == 200
    assert response.json()["can_manage"] is False
    assert {row["id"] for row in response.json()["components"]} == set(NAMES)
    assert all(not row["fields"] for row in response.json()["components"])
    assert "private-service" not in response.text and "synthetic-hidden" not in response.text


async def test_credentials_persist_outside_settings_and_empty_patch_preserves_them(
    http_plane: HttpControlPlane,
) -> None:
    url = BASE + "/hindsight/config"
    auth = http_plane.foreign_auth
    response = await http_plane.client.put(
        url,
        headers=auth,
        json={
            "values": {"base_url": "https://memory.example.test"},
            "secrets": {"api_key": "synthetic-written-once"},
        },
    )
    assert response.status_code == 200, response.text
    assert "synthetic-written-once" not in response.text
    assert response.json()["secret_configured"] == {"api_key": True}
    raw = http_plane.shared.settings_repo.get("longterm.component.hindsight")
    assert raw is not None and "api_key" not in raw
    assert (
        http_plane.shared.secret_repo.get("longterm.component.hindsight.api_key")
        == b"synthetic-written-once"
    )
    response = await http_plane.client.put(url, headers=auth, json={"secrets": {"api_key": ""}})
    assert response.json()["secret_configured"]["api_key"] is True
    fresh = ComponentManager(http_plane.shared)
    assert fresh.resolve("hindsight")["base_url"] == "https://memory.example.test"
    assert fresh.resolve("hindsight")["api_key"] == "synthetic-written-once"
    response = await http_plane.client.put(url, headers=auth, json={"clear_secrets": ["api_key"]})
    assert response.json()["secret_configured"]["api_key"] is False
    audits = http_plane.shared.audit_repo.query(action="longterm.component.config.updated")
    assert audits and all("synthetic-written-once" not in (entry.payload or "") for entry in audits)


@pytest.mark.parametrize(
    "component,values",
    [
        ("rna", {"node_binary": "unapproved-command"}),
        ("rna", {"model_json": '{"token":"synthetic-secret"}'}),
        ("semantica", {"storage_subdirectory": "../outside"}),
        ("commerceagentbench", {"results_subdirectory": "/outside"}),
        ("octop", {"fact_reports_subdirectory": "C:/outside"}),
        ("hindsight", {"base_url": "file:///private"}),
        ("hindsight", {"base_url": "https://user:password@example.test"}),
        ("hindsight", {"enabled": "false"}),
    ],
)
async def test_rejects_unapproved_execution_paths_and_plaintext_credentials(
    http_plane: HttpControlPlane, component: str, values: dict[str, Any]
) -> None:
    response = await http_plane.client.put(
        BASE + f"/{component}/config", headers=http_plane.foreign_auth, json={"values": values}
    )
    assert response.status_code == 422, response.text


async def test_readonly_probe_distinguishes_configured_from_healthy_and_redacts_upstream(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = http_plane.foreign_auth
    await http_plane.client.put(
        BASE + "/hindsight/config",
        headers=auth,
        json={
            "values": {"base_url": "https://memory.example.test"},
            "secrets": {"api_key": "synthetic-probe-secret"},
        },
    )
    before = await http_plane.client.get(BASE + "/catalog", headers=auth)
    entry = next(row for row in before.json()["components"] if row["id"] == "hindsight")
    assert entry["configured"] is True and entry["healthy"] is None
    original_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="synthetic-probe-secret /private/path")

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(reply), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    checked = await http_plane.client.post(BASE + "/hindsight/check", headers=auth)
    assert checked.status_code == 200, checked.text
    assert checked.json()["configured"] is True and checked.json()["healthy"] is False
    assert checked.json()["checks"] == [{"code": "hindsight_health", "ok": False}]
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == "/health"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-probe-secret"
    assert "synthetic-probe-secret" not in checked.text and "/private" not in checked.text


async def test_saved_hindsight_config_is_used_for_real_memory_adapter_and_task_scoped_delete(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, space = await _space(http_plane)
    task = await _echo_task(http_plane, tenant, space)
    await http_plane.client.put(
        BASE + "/hindsight/config",
        headers=http_plane.foreign_auth,
        json={
            "values": {"base_url": "https://saved-memory.example.test"},
            "secrets": {"api_key": "synthetic-memory-secret"},
        },
    )
    client = AsyncMock()
    client.arecall.return_value = {"results": []}

    def get_client(adapter: HindsightAdapter) -> Any:
        assert adapter.base_url == "https://saved-memory.example.test"
        assert adapter.api_key == "synthetic-memory-secret"
        adapter._client = client
        return client

    monkeypatch.setattr(HindsightAdapter, "_get_client", get_client)
    url = f"/api/longterm/tenants/{tenant}/tasks/{task['task_id']}/memory"
    recalled = await http_plane.client.post(
        url, headers=http_plane.owner_auth, json={"action": "recall", "query": "review"}
    )
    assert recalled.status_code == 200, recalled.text
    assert "synthetic-memory-secret" not in recalled.text
    document_id = "octop-" + "a" * 64
    denied = await http_plane.client.post(
        url,
        headers=http_plane.owner_auth,
        json={"action": "delete", "document_id": document_id, "consent": True},
    )
    assert denied.status_code == 403
    scope = tenant + ":space:" + space
    http_plane.service.repo.append(
        tenant,
        task["task_id"],
        "memory.retain",
        {
            "bank_id": HindsightAdapter.bank_id(scope),
            "document_id": document_id,
            "retained": True,
        },
    )
    deleted = await http_plane.client.post(
        url,
        headers=http_plane.owner_auth,
        json={"action": "delete", "document_id": document_id, "consent": True},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    client.documents.delete_document.assert_awaited_once_with(
        HindsightAdapter.bank_id(scope), document_id
    )
    events = http_plane.service.repo.events(tenant, task["task_id"])
    kinds = [row["event_type"] for row in events]
    assert "memory.delete.intent" in kinds and "memory.delete" in kinds


async def test_relative_results_directory_resolves_under_trusted_root_and_disable_is_effective(
    http_plane: HttpControlPlane,
) -> None:
    auth = http_plane.foreign_auth
    response = await http_plane.client.put(
        BASE + "/commerceagentbench/config",
        headers=auth,
        json={"values": {"results_subdirectory": "imports"}},
    )
    assert response.status_code == 200
    expected = http_plane.shared.paths.root / "longterm/bench-results/imports"
    assert (
        http_plane.service.components.directory("commerceagentbench", "results_subdirectory")
        == expected
    )
    assert not expected.exists()
    admin = User(http_plane.foreign_id, "foreign-admin", "admin", None)
    http_plane.service.components.save(
        "beatdesign", admin, values={"enabled": False}, secrets={}, clear_secrets=[]
    )
    tenant, _ = await _space(http_plane)
    imported = await http_plane.client.post(
        f"/api/longterm/tenants/{tenant}/workflow/import",
        headers=http_plane.owner_auth,
        json={"document": {}},
    )
    assert imported.status_code == 503
    assert "api_key" not in json.dumps(http_plane.service.integrations())


async def test_saved_rna_model_and_secret_reach_the_sdk_without_environment_mutation(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, space = await _space(http_plane)
    task = await _echo_task(http_plane, tenant, space)
    saved = await http_plane.client.put(
        BASE + "/rna/config",
        headers=http_plane.foreign_auth,
        json={
            "values": {"model_json": '{"provider":"openai","model":"test-model"}'},
            "secrets": {"api_key": "synthetic-rna-provider-key"},
        },
    )
    assert saved.status_code == 200
    captured: dict[str, Any] = {}

    async def propose(adapter: RnaSdkAdapter, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"source": "rna_dev", "status": "blocked", "reason": "synthetic_readonly_probe"}

    monkeypatch.setattr(RnaSdkAdapter, "propose", propose)
    response = await http_plane.client.post(
        f"/api/longterm/tenants/{tenant}/tasks/{task['task_id']}/evolution",
        headers=http_plane.owner_auth,
        json={
            "action": "rna_propose",
            "expected_revision": task["revision"],
        },
    )
    assert response.status_code == 200, response.text
    assert captured["api_key"] == "synthetic-rna-provider-key"
    assert captured["model"] == {"provider": "openai", "model": "test-model"}
    assert "synthetic-rna-provider-key" not in response.text


async def test_saved_temporal_connection_and_queue_reach_the_real_dispatch_path(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from octop.infra.longterm import temporal_runtime

    tenant, space = await _space(http_plane)
    task = await _echo_task(http_plane, tenant, space)
    saved = await http_plane.client.put(
        BASE + "/temporal/config",
        headers=http_plane.foreign_auth,
        json={
            "values": {
                "address": "temporal.example.test:7233",
                "namespace": "saved-namespace",
                "task_queue": "saved-queue",
                "tls": True,
            },
            "secrets": {"api_key": "synthetic-temporal-key"},
        },
    )
    assert saved.status_code == 200
    client = object()
    connect = AsyncMock(return_value=client)
    start = AsyncMock(return_value="synthetic-workflow")
    monkeypatch.setattr(temporal_runtime, "connect", connect)
    monkeypatch.setattr(temporal_runtime, "start_task", start)
    await http_plane.service._dispatch_temporal({**task, "status": "queued"})
    connect.assert_awaited_once_with(
        address="temporal.example.test:7233",
        namespace="saved-namespace",
        tls=True,
        api_key="synthetic-temporal-key",
    )
    start.assert_awaited_once_with(
        client, tenant_id=tenant, task_id=task["task_id"], task_queue="saved-queue"
    )


@pytest.mark.parametrize("identities,healthy", [(["123@worker-container"], True), ([], False)])
async def test_temporal_component_probe_accepts_remote_worker_but_requires_a_poller(
    http_plane: HttpControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    identities: list[str],
    healthy: bool,
) -> None:
    pytest.importorskip("temporalio")
    from temporalio.api.taskqueue.v1 import PollerInfo
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse

    from octop.infra.longterm import temporal_runtime

    monkeypatch.setattr(temporal_runtime.socket, "gethostname", lambda: "api-container")
    client = SimpleNamespace(
        workflow_service=SimpleNamespace(
            describe_namespace=AsyncMock(),
            describe_task_queue=AsyncMock(
                return_value=DescribeTaskQueueResponse(
                    pollers=[PollerInfo(identity=identity) for identity in identities]
                )
            ),
        )
    )
    monkeypatch.setattr(temporal_runtime, "connect", AsyncMock(return_value=client))

    response = await http_plane.client.post(
        BASE + "/temporal/check", headers=http_plane.foreign_auth
    )

    assert response.status_code == 200, response.text
    checked = response.json()
    assert checked["healthy"] is healthy
    assert {entry["code"]: entry["ok"] for entry in checked["checks"]} == {
        "temporal_sdk": True,
        "temporal_service": True,
        "temporal_namespace": True,
        "temporal_worker": healthy,
    }


async def test_component_patch_retries_concurrent_public_updates_without_losing_fields(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = http_plane.shared.settings_repo
    original = repo.compare_and_set
    attempts = 0

    def concurrent(key: str, expected: str | None, value: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            repo.set(key, '{"namespace":"concurrent-namespace"}')
            return False
        return original(key, expected, value)

    monkeypatch.setattr(repo, "compare_and_set", concurrent)
    response = await http_plane.client.put(
        BASE + "/temporal/config",
        headers=http_plane.foreign_auth,
        json={"values": {"task_queue": "new-queue"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["values"]["namespace"] == "concurrent-namespace"
    assert response.json()["values"]["task_queue"] == "new-queue"
