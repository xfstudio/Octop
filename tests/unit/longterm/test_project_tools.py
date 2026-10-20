"""Project reads are authenticated, privately scoped and cannot mutate upstreams."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any

import pytest

from octop.infra.longterm.adapters import BeatDesignAdapter, RnaAdapter
from tests.unit.longterm.test_api import HttpControlPlane, _space
from tests.unit.longterm.test_api import http_plane as http_plane

_BASE = "/api/longterm"


async def _bind(plane: HttpControlPlane, tenant_id: str, component: str, project: str) -> None:
    granted = await plane.client.put(
        f"{_BASE}/tenants/{tenant_id}/members",
        headers=plane.owner_auth,
        json={"user_id": plane.foreign_id, "role": "admin"},
    )
    assert granted.status_code == 200, granted.text
    result = await plane.client.put(
        f"{_BASE}/tenants/{tenant_id}/integrations/{component}/project",
        headers=plane.foreign_auth,
        json={"project_id": project, "expected_revision": 0},
    )
    assert result.status_code == 200, result.text


async def test_binding_requires_host_and_tenant_authority_and_cas(
    http_plane: HttpControlPlane,
) -> None:
    tenant, _ = await _space(http_plane)
    url = f"{_BASE}/tenants/{tenant}/integrations/beatdesign/project"
    body = {"project_id": "exclusive-project", "expected_revision": 0}
    assert (await http_plane.client.get(url)).status_code == 401
    assert (
        await http_plane.client.put(url, headers=http_plane.owner_auth, json=body)
    ).status_code == 403
    assert (
        await http_plane.client.put(url, headers=http_plane.foreign_auth, json=body)
    ).status_code == 403
    await _bind(http_plane, tenant, "beatdesign", "exclusive-project")
    assert (
        await http_plane.client.put(url, headers=http_plane.foreign_auth, json=body)
    ).status_code == 409
    owner = await http_plane.client.get(url, headers=http_plane.owner_auth)
    assert owner.status_code == 200
    assert owner.json()["project_id"] == "exclusive-project"
    assert owner.json()["can_manage"] is False


async def test_project_cannot_be_shared_between_tenants(http_plane: HttpControlPlane) -> None:
    tenant, _ = await _space(http_plane)
    await _bind(http_plane, tenant, "rna", "exclusive-project")
    tenant2 = (
        await http_plane.client.post(
            f"{_BASE}/tenants", headers=http_plane.foreign_auth, json={"name": "other"}
        )
    ).json()["tenant_id"]
    response = await http_plane.client.put(
        f"{_BASE}/tenants/{tenant2}/integrations/rna/project",
        headers=http_plane.foreign_auth,
        json={"project_id": "exclusive-project", "expected_revision": 1},
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "project_already_bound"


async def test_private_space_read_rejects_foreign_access_and_project_override(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, space = await _space(http_plane)
    await _bind(http_plane, tenant, "beatdesign", "project-a")
    monkeypatch.setattr(
        http_plane.service.components,
        "resolve",
        lambda _: {"enabled": True, "endpoint": "http://local.test/mcp"},
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def read(
        self: BeatDesignAdapter, tid: str, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert self.tenant_projects == {tenant: "project-a"}
        calls.append((tid, tool, args))
        return {"canvas": {"cards": []}}

    monkeypatch.setattr(BeatDesignAdapter, "call_tool", read)
    url = f"{_BASE}/tenants/{tenant}/spaces/{space}/integrations/beatdesign/read"
    foreign = await http_plane.client.post(
        url, headers=http_plane.foreign_auth, json={"operation": "canvas"}
    )
    assert foreign.status_code == 404
    for body in (
        {"operation": "canvas", "arguments": {"projectId": "project-b"}},
        {"operation": "bdesign_generation_submit"},
    ):
        assert (
            await http_plane.client.post(url, headers=http_plane.owner_auth, json=body)
        ).status_code == 422
    assert calls == []
    result = await http_plane.client.post(
        url, headers=http_plane.owner_auth, json={"operation": "canvas"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["project_id"] == "project-a"
    assert calls == [(tenant, "bdesign_canvas_get", {})]


async def test_rna_read_revoked_membership_discards_inflight_response(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, space = await _space(http_plane)
    await _bind(http_plane, tenant, "rna", "project-a")
    private_space = await http_plane.client.post(
        f"{_BASE}/tenants/{tenant}/spaces",
        headers=http_plane.foreign_auth,
        json={"name": "admin private"},
    )
    space = private_space.json()["space_id"]
    monkeypatch.setattr(
        http_plane.service.components,
        "resolve",
        lambda _: {"enabled": True, "base_url": "http://local.test"},
    )

    async def genes(self: RnaAdapter, tid: str) -> dict[str, Any]:
        http_plane.shared.longterm_repo.set_member(tenant, http_plane.foreign_id, None)
        return {"genes": []}

    monkeypatch.setattr(RnaAdapter, "genes", genes)
    result = await http_plane.client.post(
        f"{_BASE}/tenants/{tenant}/spaces/{space}/integrations/rna/read",
        headers=http_plane.foreign_auth,
        json={"operation": "genes"},
    )
    assert result.status_code == 403, result.text


async def test_binding_revoked_before_commit_does_not_change_settings(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _ = await _space(http_plane)
    granted = await http_plane.client.put(
        f"{_BASE}/tenants/{tenant}/members",
        headers=http_plane.owner_auth,
        json={"user_id": http_plane.foreign_id, "role": "admin"},
    )
    assert granted.status_code == 200
    original = http_plane.shared.longterm_repo.bind_project

    def revoke_then_commit(*args: Any) -> bool:
        http_plane.shared.longterm_repo.set_member(tenant, http_plane.foreign_id, None)
        return original(*args)

    monkeypatch.setattr(http_plane.shared.longterm_repo, "bind_project", revoke_then_commit)
    url = f"{_BASE}/tenants/{tenant}/integrations/beatdesign/project"
    result = await http_plane.client.put(
        url,
        headers=http_plane.foreign_auth,
        json={"project_id": "must-not-bind", "expected_revision": 0},
    )
    assert result.status_code == 403, result.text
    current = await http_plane.client.get(url, headers=http_plane.owner_auth)
    assert current.json()["project_id"] is None
    assert current.json()["revision"] == 0


async def test_mcp_initialization_revocation_prevents_tool_dispatch(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _ = await _space(http_plane)
    await _bind(http_plane, tenant, "beatdesign", "project-a")
    space = (
        await http_plane.client.post(
            f"{_BASE}/tenants/{tenant}/spaces",
            headers=http_plane.foreign_auth,
            json={"name": "private admin"},
        )
    ).json()["space_id"]
    monkeypatch.setattr(
        http_plane.service.components,
        "resolve",
        lambda _: {"enabled": True, "endpoint": "http://local.test/mcp"},
    )
    dispatched: list[str] = []

    class Session:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def initialize(self) -> None:
            http_plane.shared.longterm_repo.set_member(tenant, http_plane.foreign_id, None)

        async def call_tool(self, tool: str, **_: Any) -> dict[str, Any]:
            dispatched.append(tool)
            return {"result": {}}

    @asynccontextmanager
    async def transport(*_: Any, **__: Any) -> AsyncIterator[tuple[None, None, None]]:
        yield None, None, None

    mcp = ModuleType("mcp")
    mcp.ClientSession = Session  # type: ignore[attr-defined]
    http_transport = ModuleType("mcp.client.streamable_http")
    http_transport.streamablehttp_client = transport  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", http_transport)
    result = await http_plane.client.post(
        f"{_BASE}/tenants/{tenant}/spaces/{space}/integrations/beatdesign/read",
        headers=http_plane.foreign_auth,
        json={"operation": "canvas"},
    )
    assert result.status_code == 403, result.text
    assert dispatched == []
