"""Docker lifecycle controls are scoped to existing, verified Compose containers."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from octop.infra.errors import OctopError
from octop.infra.longterm import component_runtime
from tests.unit.longterm.test_api import HttpControlPlane
from tests.unit.longterm.test_api import http_plane as http_plane
from tests.unit.longterm.test_component_runtime import _admin, _manifest

PROJECT = "private-runtime-test"
SECRET = "synthetic-daemon-secret /private/deployment/path"


class DockerDaemon:
    def __init__(self) -> None:
        self.ids = {"temporal": "a" * 64, "worker": "b" * 64}
        self.inspects = {
            cid: {
                "Id": cid,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": PROJECT,
                        "com.docker.compose.service": service,
                        "com.docker.compose.oneoff": "False",
                    },
                    "Env": [SECRET],
                },
                "State": {"Status": "exited", "Health": {"Status": "healthy", "Log": [SECRET]}},
            }
            for service, cid in self.ids.items()
        }
        self.calls: list[tuple[str, str]] = []
        self.mutations: list[tuple[str, str]] = []
        self.missing: set[str] = set()
        self.fail_get = False
        self.fail_post = False
        self.short_id = False
        self.duplicate = False
        self.dispatched = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "docker"
        assert request.url.path.startswith("/v1.45/")
        path = request.url.path.removeprefix("/v1.45/")
        self.calls.append((request.method, path))
        if request.method == "GET" and self.fail_get:
            return httpx.Response(500, json={"message": SECRET})
        if request.method == "GET" and path == "containers/json":
            assert request.url.params["all"] == "true"
            filters = json.loads(request.url.params["filters"])
            assert set(filters) == {"label"}
            assert len(filters["label"]) == 2
            labels = dict(value.split("=", 1) for value in filters["label"])
            assert labels["com.docker.compose.project"] == PROJECT
            service = labels["com.docker.compose.service"]
            assert service in self.ids
            rows = (
                []
                if service in self.missing
                else [{"Id": self.ids[service][:12] if self.short_id else self.ids[service]}]
            )
            return httpx.Response(200, json=rows * (2 if self.duplicate else 1))
        parts = path.split("/")
        assert len(parts) == 3 and parts[0] == "containers"
        cid, action = parts[1:]
        assert cid in self.inspects and len(cid) == 64
        if request.method == "GET":
            assert action == "json"
            return httpx.Response(200, json=copy.deepcopy(self.inspects[cid]))
        assert request.method == "POST" and action in {"start", "stop"}
        timeout = (
            "40"
            if self.inspects[cid]["Config"]["Labels"]["com.docker.compose.service"] == "hindsight"
            else "30"
        )
        assert dict(request.url.params) == ({"t": timeout} if action == "stop" else {})
        self.mutations.append((cid, action))
        self.dispatched.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail_post:
            return httpx.Response(500, json={"message": SECRET})
        self.inspects[cid]["State"]["Status"] = "running" if action == "start" else "exited"
        return httpx.Response(204)


@pytest.fixture
def docker_runtime(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[component_runtime.ComponentRuntimeManager, DockerDaemon]:
    _manifest(
        tmp_path,
        monkeypatch,
        {
            "temporal": {
                "mode": "docker",
                "project_name": PROJECT,
                "services": ["temporal", "worker"],
                "console_url": "http://localhost:8233",
            }
        },
    )
    monkeypatch.setenv("OCTOP_DOCKER_SOCKET", "/private-test/docker.sock")
    daemon = DockerDaemon()

    def transport(*, uds: str) -> httpx.MockTransport:
        assert uds == "/private-test/docker.sock"
        return httpx.MockTransport(daemon.handle)

    monkeypatch.setattr(component_runtime.httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("Docker mode must never use a CLI")),
    )
    return http_plane.service.component_runtime, daemon


async def _action(
    runtime: component_runtime.ComponentRuntimeManager,
    plane: HttpControlPlane,
    action: str,
    rev: int,
) -> dict[str, Any]:
    return await runtime.lifecycle(
        "temporal", _admin(plane), action=action, confirmed=True, expected_revision=rev
    )


async def test_docker_uses_only_verified_existing_ids_and_orders_services(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
) -> None:
    runtime, daemon = docker_runtime
    before = await runtime.status("temporal", _admin(http_plane))
    assert before["mode"] == "docker" and before["state"] == "stopped"
    assert before["can_start"] and not before["can_stop"]
    started = await _action(runtime, http_plane, "start", 0)
    assert started["state"] == "running" and started["revision"] == 1
    assert started["console"]["url"] == "http://localhost:8233"
    assert daemon.mutations == [(daemon.ids[name], "start") for name in ("temporal", "worker")]
    assert all(set(service) == {"name", "state", "health"} for service in started["services"])
    assert SECRET not in json.dumps(started)
    stopped = await _action(runtime, http_plane, "stop", 1)
    assert stopped["state"] == "stopped" and stopped["revision"] == 2
    assert daemon.mutations[2:] == [(daemon.ids[name], "stop") for name in ("worker", "temporal")]
    for _, path in daemon.calls:
        assert path == "containers/json" or path.rsplit("/", 1)[1] in {"json", "start", "stop"}
    audits = http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.finished")
    assert len(audits) == 2
    assert all(json.loads(audit.payload)["outcome"] == "completed" for audit in audits)
    assert SECRET not in str([audit.payload for audit in audits])


@pytest.mark.parametrize("health", ["unhealthy", "starting"])
async def test_docker_running_and_health_are_independent(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
    health: str,
) -> None:
    runtime, daemon = docker_runtime
    for inspect in daemon.inspects.values():
        inspect["State"].update(Status="running", Health={"Status": health, "Log": [SECRET]})
    row = await runtime.status("temporal", _admin(http_plane))
    assert row["state"] == "running" and row["can_stop"] and not row["can_start"]
    assert {service["health"] for service in row["services"]} == {health}


async def test_docker_partial_allows_completing_existing_service_start(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
) -> None:
    runtime, daemon = docker_runtime
    daemon.inspects[daemon.ids["temporal"]]["State"]["Status"] = "running"
    before = await runtime.status("temporal", _admin(http_plane))
    assert before["state"] == "partial" and before["can_start"] and before["can_stop"]
    assert (await _action(runtime, http_plane, "start", 0))["state"] == "running"


@pytest.mark.parametrize(
    "invalid",
    [
        {"project_name": "Other-project"},
        {"project_name": "project.name"},
        {"project_name": "-project"},
        {"services": []},
        {"services": ["worker", "worker"]},
        {"services": ["worker;bad"]},
        {"services": "worker"},
        {"compose_file": "/private/deployment.yml"},
        {"profiles": ["unexpected"]},
        {"argv": ["unapproved"]},
    ],
)
async def test_docker_manifest_rejects_unbound_targets(
    http_plane: HttpControlPlane,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: dict[str, Any],
) -> None:
    _manifest(
        tmp_path,
        monkeypatch,
        {
            "temporal": {
                "mode": "docker",
                "project_name": PROJECT,
                "services": ["temporal", "worker"],
                **invalid,
            }
        },
    )
    row = await http_plane.service.component_runtime.status("temporal", _admin(http_plane))
    assert row["state"] == "unknown" and row["reason"] == "runtime_manifest_invalid"
    assert not row["can_start"] and not row["can_stop"]


@pytest.mark.parametrize(
    "invalid",
    [
        "project",
        "service",
        "oneoff",
        "missing-oneoff",
        "id",
        "status",
        "short-id",
        "duplicate",
        "missing",
        "daemon",
    ],
)
async def test_docker_bad_identity_missing_services_or_daemon_fails_closed(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
    invalid: str,
) -> None:
    runtime, daemon = docker_runtime
    inspect = daemon.inspects[daemon.ids["worker"]]
    if invalid in {"project", "service", "oneoff"}:
        inspect["Config"]["Labels"]["com.docker.compose." + invalid] = "other-project"
    elif invalid == "missing-oneoff":
        del inspect["Config"]["Labels"]["com.docker.compose.oneoff"]
    elif invalid == "id":
        inspect["Id"] = "c" * 64
    elif invalid == "status":
        inspect["State"]["Status"] = [SECRET]
    elif invalid == "short-id":
        daemon.short_id = True
    elif invalid == "duplicate":
        daemon.duplicate = True
    elif invalid == "missing":
        daemon.missing.add("worker")
    else:
        daemon.fail_get = True
    row = await runtime.status("temporal", _admin(http_plane))
    assert row["state"] == "unknown" and row["reason"] == "runtime_observation_failed"
    assert not row["can_start"] and not row["can_stop"]
    with pytest.raises(OctopError) as failed:
        await _action(runtime, http_plane, "start", 0)
    assert failed.value.status == 409
    assert SECRET not in str(failed.value.details) + json.dumps(row)
    assert daemon.mutations == []
    assert not http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.requested")


@pytest.mark.parametrize("change", ["missing", "labels", "audit", "permission"])
async def test_docker_revalidates_all_targets_and_authority_after_intent(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    runtime, daemon = docker_runtime
    original = http_plane.shared.audit_repo.user_event

    def intent(username: str, action: str, *args: Any) -> None:
        if action == "longterm.component.lifecycle.requested":
            if change == "audit":
                raise RuntimeError(SECRET)
            if change == "missing":
                daemon.missing.add("worker")
            elif change == "labels":
                daemon.inspects[daemon.ids["worker"]]["Config"]["Labels"][
                    "com.docker.compose.project"
                ] = "different-project"
            elif change == "permission":
                http_plane.shared.user_repo.set_disabled(http_plane.foreign_id, True)
        original(username, action, *args)

    monkeypatch.setattr(http_plane.shared.audit_repo, "user_event", intent)
    with pytest.raises(OctopError) as failed:
        await _action(runtime, http_plane, "start", 0)
    assert failed.value.status == (403 if change == "permission" else 503)
    assert daemon.mutations == []
    record = json.loads(http_plane.shared.settings_repo.get("longterm.component.runtime.temporal"))
    assert record["pending"] is None and record["outcome"] == "uncertain"


async def test_docker_daemon_mutation_error_is_sanitized_and_uncertain(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
) -> None:
    runtime, daemon = docker_runtime
    daemon.fail_post = True
    with pytest.raises(OctopError) as failed:
        await _action(runtime, http_plane, "start", 0)
    assert failed.value.status == 503
    assert failed.value.details == {"reason": "runtime_command_failed"}
    audits = http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.finished")
    assert len(audits) == 1 and json.loads(audits[0].payload)["outcome"] == "uncertain"
    assert SECRET not in (audits[0].payload or "") + str(failed.value)
    assert daemon.mutations == [(daemon.ids["temporal"], "start")]


async def test_docker_revocation_between_posts_prevents_next_service_mutation(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, daemon = docker_runtime
    original = daemon.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        response = await original(request)
        if request.method == "POST":
            http_plane.shared.user_repo.set_role(http_plane.foreign_id, "user")
        return response

    monkeypatch.setattr(daemon, "handle", handle)
    with pytest.raises(OctopError) as failed:
        await _action(runtime, http_plane, "start", 0)
    assert failed.value.status == 403
    assert daemon.mutations == [(daemon.ids["temporal"], "start")]


async def test_docker_cancelled_dispatch_records_uncertain_without_releasing_early(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
) -> None:
    runtime, daemon = docker_runtime
    daemon.release = asyncio.Event()
    caller = asyncio.create_task(_action(runtime, http_plane, "start", 0))
    try:
        async with asyncio.timeout(8):
            await daemon.dispatched.wait()
        operation = next(iter(runtime._operations))
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        record = json.loads(
            http_plane.shared.settings_repo.get("longterm.component.runtime.temporal")
        )
        assert record["revision"] == 1 and record["pending"] is None
        assert record["outcome"] == "uncertain"
        audits = http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.finished")
        assert len(audits) == 1 and json.loads(audits[0].payload)["outcome"] == "uncertain"
    finally:
        daemon.release.set()
        await asyncio.gather(caller, return_exceptions=True)


async def test_docker_close_does_not_stop_managed_containers(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
) -> None:
    runtime, daemon = docker_runtime
    await _action(runtime, http_plane, "start", 0)
    before = list(daemon.mutations)
    await runtime.close()
    assert daemon.mutations == before
    assert all(inspect["State"]["Status"] == "running" for inspect in daemon.inspects.values())


async def test_docker_hindsight_receives_its_longer_graceful_stop_budget(
    http_plane: HttpControlPlane,
    docker_runtime: tuple[component_runtime.ComponentRuntimeManager, DockerDaemon],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, daemon = docker_runtime
    cid = daemon.ids["temporal"]
    daemon.ids = {"hindsight": cid}
    daemon.inspects = {cid: daemon.inspects[cid]}
    daemon.inspects[cid]["Config"]["Labels"]["com.docker.compose.service"] = "hindsight"
    daemon.inspects[cid]["State"]["Status"] = "running"
    _manifest(
        tmp_path,
        monkeypatch,
        {"hindsight": {"mode": "docker", "project_name": PROJECT, "services": ["hindsight"]}},
    )
    stopped = await runtime.lifecycle(
        "hindsight", _admin(http_plane), action="stop", confirmed=True, expected_revision=0
    )
    assert stopped["state"] == "stopped" and daemon.mutations == [(cid, "stop")]
