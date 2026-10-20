"""Lifecycle controls use host authority, durable claims and owned process handles."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from octop.infra.errors import OctopError
from octop.infra.longterm.component_runtime import ComponentRuntimeManager
from octop.infra.longterm.components import ComponentManager
from octop.infra.users.identity import User
from tests.unit.longterm.test_api import HttpControlPlane, _space
from tests.unit.longterm.test_api import http_plane as http_plane

BASE = "/api/longterm/integrations"


@pytest.fixture(autouse=True)
def isolated_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OCTOP_COMPONENT_RUNTIME_FILE",
        "OCTOP_HINDSIGHT_URL",
        "OCTOP_BEATDESIGN_MCP_URL",
        "OCTOP_RNA_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _admin(plane: HttpControlPlane) -> User:
    return User(plane.foreign_id, "foreign-admin", "admin", None)


def _manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, components: dict[str, Any]) -> Path:
    path = tmp_path / "private-runtime.json"
    path.write_text(json.dumps({"version": 1, "components": components}), encoding="utf-8")
    monkeypatch.setenv("OCTOP_COMPONENT_RUNTIME_FILE", str(path))
    return path


async def _inventory(plane: HttpControlPlane) -> dict[str, dict[str, Any]]:
    response = await plane.client.get(BASE + "/runtime", headers=plane.foreign_auth)
    assert response.status_code == 200, response.text
    return {row["component_id"]: row for row in response.json()["components"]}


async def _action(
    plane: HttpControlPlane, component: str, requested_action: str, revision: int, **extra: Any
) -> Any:
    return await plane.client.post(
        BASE + f"/{component}/lifecycle",
        headers=plane.foreign_auth,
        json={
            "action": requested_action,
            "confirmed": True,
            "expected_revision": revision,
            **extra,
        },
    )


async def test_runtime_and_lifecycle_require_host_admin_even_for_tenant_owner(
    http_plane: HttpControlPlane,
) -> None:
    await _space(http_plane)
    body = {"action": "stop", "confirmed": True, "expected_revision": 0}
    for headers, expected in (({}, 401), (http_plane.owner_auth, 403)):
        inventory = await http_plane.client.get(BASE + "/runtime", headers=headers)
        action = await http_plane.client.post(
            BASE + "/semantica/lifecycle", headers=headers, json=body
        )
        assert inventory.status_code == expected, inventory.text
        assert action.status_code == expected, action.text
    assert http_plane.service.components.resolve("semantica")["enabled"] is True
    assert not http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.requested")
    assert (await _inventory(http_plane))["semantica"]["revision"] == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"argv": ["unapproved-program"]},
        {"pid": 123},
        {"compose_file": "unapproved-file"},
        {"action": "restart"},
        {"confirmed": "true"},
        {"expected_revision": True},
    ],
)
async def test_lifecycle_body_cannot_supply_execution_targets_or_coerced_consent(
    http_plane: HttpControlPlane, extra: dict[str, Any]
) -> None:
    response = await _action(http_plane, "semantica", "stop", 0, **extra)
    assert response.status_code == 422, response.text
    assert http_plane.service.components.resolve("semantica")["enabled"] is True
    assert http_plane.shared.settings_repo.get("longterm.component.runtime.semantica") is None


async def test_embedded_lifecycle_changes_effective_enabled_with_confirmation_and_revision(
    http_plane: HttpControlPlane,
) -> None:
    before = (await _inventory(http_plane))["semantica"]
    assert before["mode"] == "embedded" and before["state"] == "enabled"
    rejected = await _action(http_plane, "semantica", "stop", 0, confirmed=False)
    assert rejected.status_code == 422
    assert http_plane.shared.settings_repo.get("longterm.component.runtime.semantica") is None
    stopped = await _action(http_plane, "semantica", "stop", 0)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "disabled" and stopped.json()["revision"] == 1
    with pytest.raises(OctopError) as disabled:
        http_plane.service.components.require_enabled("semantica")
    assert disabled.value.details["reason"] == "component_disabled"
    stale = await _action(http_plane, "semantica", "start", 0)
    assert stale.status_code == 409
    assert http_plane.service.components.resolve("semantica")["enabled"] is False
    started = await _action(http_plane, "semantica", "start", 1)
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "enabled" and started.json()["revision"] == 2
    http_plane.service.components.require_enabled("semantica")
    requested = http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.requested")
    finished = http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.finished")
    assert len(requested) == len(finished) == 2
    assert {row.actor for row in requested} == {"foreign-admin"}
    assert all(json.loads(row.payload)["outcome"] == "completed" for row in finished)


@pytest.mark.parametrize(
    "invalid",
    [
        {"mode": "shell", "argv": ["unapproved"]},
        {"mode": "process", "argv": [sys.executable], "cwd": "relative"},
        {"mode": "external", "console_url": "https://user:secret@example.test"},
        {"mode": "external", "console_url": "https://example.test/?token=secret"},
        {"mode": "external", "argv": ["unapproved"]},
    ],
)
async def test_invalid_manifest_fails_closed_for_every_binding_without_partial_apply(
    http_plane: HttpControlPlane,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: dict[str, Any],
) -> None:
    _manifest(tmp_path, monkeypatch, {"semantica": {"mode": "embedded"}, "rna": invalid})
    inventory = await _inventory(http_plane)
    assert all(row["state"] == "unknown" for row in inventory.values())
    assert all(row["reason"] == "runtime_manifest_invalid" for row in inventory.values())
    assert all(not row["can_start"] and not row["can_stop"] for row in inventory.values())
    response = await _action(http_plane, "semantica", "stop", 0)
    assert response.status_code == 409, response.text
    assert http_plane.service.components.resolve("semantica")["enabled"] is True
    assert not http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.requested")


async def test_external_integration_switch_does_not_claim_to_stop_remote_server(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_HINDSIGHT_URL", "https://memory.example.test")
    spawn = AsyncMock(side_effect=AssertionError("external integrations must not spawn processes"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    before = (await _inventory(http_plane))["hindsight"]
    assert before["mode"] == "external" and before["state"] == "enabled"
    assert before["reason"] == "runtime_external" and before["services"] == []
    stopped = await _action(http_plane, "hindsight", "stop", 0)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "disabled"
    assert stopped.json()["reason"] == "runtime_external"
    assert stopped.json()["services"] == []
    assert http_plane.service.components.resolve("hindsight")["base_url"] == (
        "https://memory.example.test"
    )
    spawn.assert_not_called()


async def test_private_python_process_stops_on_request_and_close_without_adopting_foreign_pid(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "private_sleeper.py"
    marker = tmp_path / "started.json"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'cwd': os.getcwd(), 'value': os.environ['OCTOP_RUNTIME_TEST_VALUE']}))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    _manifest(
        tmp_path,
        monkeypatch,
        {
            "semantica": {
                "mode": "process",
                "argv": [sys.executable, str(script), str(marker)],
                "cwd": str(tmp_path),
                "env": {"OCTOP_RUNTIME_TEST_VALUE": "private-test"},
            }
        },
    )
    runtime = http_plane.service.component_runtime
    fresh = ComponentRuntimeManager(ComponentManager(http_plane.shared))
    processes: list[asyncio.subprocess.Process] = []
    try:
        started = await _action(http_plane, "semantica", "start", 0)
        assert started.status_code == 200, started.text
        assert started.json()["state"] == "running"
        process = runtime._processes["semantica"]
        processes.append(process)
        async with asyncio.timeout(8):
            while not marker.exists():
                await asyncio.sleep(0.02)
        assert json.loads(marker.read_text()) == {"cwd": str(tmp_path), "value": "private-test"}
        foreign = await fresh.status("semantica", _admin(http_plane))
        assert foreign["state"] == "unknown" and foreign["reason"] == "runtime_owner_unavailable"
        assert not foreign["can_start"] and not foreign["can_stop"]
        for action in ("start", "stop"):
            with pytest.raises(OctopError) as denied:
                await fresh.lifecycle(
                    "semantica",
                    _admin(http_plane),
                    action=action,
                    confirmed=True,
                    expected_revision=1,
                )
            assert denied.value.status == 409
        assert process.returncode is None
        stopped = await _action(http_plane, "semantica", "stop", 1)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "stopped" and process.returncode is not None
        restarted = await _action(http_plane, "semantica", "start", 2)
        assert restarted.status_code == 200, restarted.text
        processes.append(runtime._processes["semantica"])
        await runtime.close()
        assert all(proc.returncode is not None for proc in processes)
        assert (await fresh.status("semantica", _admin(http_plane)))["state"] == "stopped"
    finally:
        await runtime.close()
        await fresh.close()
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_exited_parent_stays_unknown_and_cannot_be_started_again(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest(
        tmp_path,
        monkeypatch,
        {
            "semantica": {
                "mode": "process",
                "argv": [sys.executable, "-c", "pass"],
                "cwd": str(tmp_path),
            }
        },
    )
    runtime = http_plane.service.component_runtime
    started = await _action(http_plane, "semantica", "start", 0)
    assert started.status_code == 200, started.text
    process = runtime._processes["semantica"]
    try:
        async with asyncio.timeout(8):
            await process.wait()
        row = (await _inventory(http_plane))["semantica"]
        assert row["state"] == "unknown"
        assert row["can_start"] is False
        repeated = await _action(http_plane, "semantica", "start", row["revision"])
        assert repeated.status_code == 409, repeated.text
        assert runtime._processes["semantica"] is process
        await runtime.close()
        record = json.loads(
            http_plane.shared.settings_repo.get("longterm.component.runtime.semantica")
        )
        assert record["process_active"] is True
        observer = ComponentRuntimeManager(ComponentManager(http_plane.shared))
        try:
            after_close = await observer.status("semantica", _admin(http_plane))
            assert after_close["state"] == "unknown" and after_close["can_start"] is False
        finally:
            await observer.close()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await runtime.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group termination")
async def test_stop_kills_owned_child_ignoring_term_after_parent_exits(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_script = tmp_path / "private_child.py"
    parent_script = tmp_path / "private_parent.py"
    ready = tmp_path / "child-ready.json"
    heartbeat = tmp_path / "child-heartbeat"
    child_script.write_text(
        "import json, os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "ready, heartbeat = map(pathlib.Path, sys.argv[1:])\n"
        "pending = ready.with_suffix('.pending')\n"
        "pending.write_text(json.dumps({'pid': os.getpid(), 'group': os.getpgrp()}))\n"
        "pending.replace(ready)\n"
        "while True:\n"
        "    pending = heartbeat.with_suffix('.pending')\n"
        "    pending.write_text(str(time.monotonic_ns()))\n"
        "    pending.replace(heartbeat)\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, *sys.argv[1:]])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    _manifest(
        tmp_path,
        monkeypatch,
        {
            "semantica": {
                "mode": "process",
                "argv": [
                    sys.executable,
                    str(parent_script),
                    str(child_script),
                    str(ready),
                    str(heartbeat),
                ],
                "cwd": str(tmp_path),
            }
        },
    )
    runtime = http_plane.service.component_runtime
    process: asyncio.subprocess.Process | None = None
    child_pid: int | None = None
    try:
        started = await _action(http_plane, "semantica", "start", 0)
        assert started.status_code == 200, started.text
        process = runtime._processes["semantica"]
        async with asyncio.timeout(8):
            while not ready.exists() or not heartbeat.exists():
                await asyncio.sleep(0.02)
        child = json.loads(ready.read_text())
        child_pid = child["pid"]
        assert child["group"] == process.pid == os.getpgid(child_pid)
        # The child has installed SIG_IGN and is alive before requesting stop.
        first_heartbeat = heartbeat.read_text()
        async with asyncio.timeout(3):
            while heartbeat.read_text() == first_heartbeat:
                await asyncio.sleep(0.02)
        stopped = await _action(http_plane, "semantica", "stop", 1)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "stopped"
        assert process.returncode is not None
        last_heartbeat = heartbeat.read_text()
        await asyncio.sleep(0.2)
        # Orphan zombies may await their system reaper, but no descendant may keep running.
        assert heartbeat.read_text() == last_heartbeat
    finally:
        if process is not None:
            if child_pid is not None:
                with suppress(ProcessLookupError):
                    if os.getpgid(child_pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        await runtime.close()


async def test_two_managers_compete_for_one_durable_revision_claim(
    http_plane: HttpControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = ComponentRuntimeManager(ComponentManager(http_plane.shared))
    original = http_plane.shared.settings_repo.compare_and_set
    barrier = threading.Barrier(2, timeout=8)

    def compete(key: str, expected: str | None, value: str) -> bool:
        if key == "longterm.component.runtime.semantica" and json.loads(value).get("pending"):
            barrier.wait()
        return original(key, expected, value)

    monkeypatch.setattr(http_plane.shared.settings_repo, "compare_and_set", compete)
    try:
        results = await asyncio.gather(
            *(
                runtime.lifecycle(
                    "semantica",
                    _admin(http_plane),
                    action="stop",
                    confirmed=True,
                    expected_revision=0,
                )
                for runtime in (http_plane.service.component_runtime, second)
            ),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, dict)]
        conflicts = [result for result in results if isinstance(result, OctopError)]
        assert len(successes) == len(conflicts) == 1, results
        assert successes[0]["state"] == "disabled" and successes[0]["revision"] == 1
        assert conflicts[0].status == 409
        assert (
            len(http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.requested"))
            == 1
        )
    finally:
        await second.close()


def _compose_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    compose = tmp_path / "trusted-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    _manifest(
        tmp_path,
        monkeypatch,
        {
            "temporal": {
                "mode": "compose",
                "compose_file": str(compose),
                "project_name": "private-runtime-test",
                "profiles": ["temporal-server", "worker"],
                "services": ["temporal", "worker"],
                "console_url": "https://temporal.example.test/console",
            }
        },
    )
    return [
        "docker",
        "compose",
        "--file",
        str(compose),
        "--project-name",
        "private-runtime-test",
        "--profile",
        "temporal-server",
        "--profile",
        "worker",
    ]


async def test_compose_uses_only_bound_services_and_never_builds_pulls_or_removes_volumes(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = _compose_manifest(tmp_path, monkeypatch)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    running = False

    class Command:
        returncode = 0

        def __init__(self, output: bytes = b"") -> None:
            self.output = output

        async def communicate(self) -> tuple[bytes, None]:
            return self.output, None

    async def spawn(*argv: str, **kwargs: Any) -> Command:
        nonlocal running
        args = list(argv)
        calls.append((args, kwargs))
        assert args[: len(prefix)] == prefix
        assert kwargs["stdin"] == kwargs["stderr"] == asyncio.subprocess.DEVNULL
        command = args[len(prefix) :]
        if command[0] == "ps":
            assert command == ["ps", "--all", "--format", "json", "temporal", "worker"]
            assert kwargs["stdout"] == asyncio.subprocess.PIPE
            rows = (
                [
                    {"Service": service, "State": "running", "Health": "healthy"}
                    for service in ("temporal", "worker")
                ]
                if running
                else []
            )
            return Command(json.dumps(rows).encode())
        assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
        if command[0] == "up":
            assert command == [
                "up",
                "--detach",
                "--no-build",
                "--pull",
                "never",
                "temporal",
                "worker",
            ]
            running = True
        else:
            assert command == ["stop", "--timeout", "10", "temporal", "worker"]
            running = False
        return Command()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    started = await _action(http_plane, "temporal", "start", 0)
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "running"
    assert started.json()["console"]["url"] == "https://temporal.example.test/console"
    stopped = await _action(http_plane, "temporal", "stop", 1)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "stopped"
    mutations = [args[len(prefix) :] for args, _ in calls if args[len(prefix)] != "ps"]
    assert len(mutations) == 2


async def test_compose_command_failure_never_returns_or_audits_raw_output(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = _compose_manifest(tmp_path, monkeypatch)
    secret = "synthetic-runtime-secret /private/deployment/path"

    class Command:
        def __init__(self, observe: bool) -> None:
            self.returncode = 0 if observe else 17
            self.output = b"[]" if observe else secret.encode()

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.output, secret.encode()

    async def spawn(*argv: str, **kwargs: Any) -> Command:
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        return Command(list(argv)[len(prefix)] == "ps")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    response = await _action(http_plane, "temporal", "start", 0)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["details"]["reason"] == "runtime_command_failed"
    assert "synthetic-runtime-secret" not in response.text and "/private/" not in response.text
    audits = http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.finished")
    assert len(audits) == 1 and json.loads(audits[0].payload)["outcome"] == "uncertain"
    assert secret not in (audits[0].payload or "")


@pytest.mark.parametrize("revocation", ["demoted", "disabled"])
async def test_authority_revoked_after_intent_blocks_compose_mutation(
    http_plane: HttpControlPlane,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    _compose_manifest(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    async def command(argv: list[str], *, capture: bool = False) -> str:
        commands.append(argv)
        assert capture and "ps" in argv, "no mutation may run after host authority is revoked"
        return "[]"

    original = http_plane.shared.audit_repo.user_event

    def revoke(username: str, action: str, *args: Any) -> None:
        original(username, action, *args)
        if action == "longterm.component.lifecycle.requested":
            if revocation == "demoted":
                http_plane.shared.user_repo.set_role(http_plane.foreign_id, "user")
            else:
                http_plane.shared.user_repo.set_disabled(http_plane.foreign_id, True)

    monkeypatch.setattr(http_plane.service.component_runtime, "_command", command)
    monkeypatch.setattr(http_plane.shared.audit_repo, "user_event", revoke)
    response = await _action(http_plane, "temporal", "start", 0)
    assert response.status_code == 403, response.text
    assert len(commands) == 1
    record = json.loads(http_plane.shared.settings_repo.get("longterm.component.runtime.temporal"))
    assert record["pending"] is None and record["outcome"] == "uncertain"


async def test_audit_intent_failure_prevents_service_dispatch(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compose_manifest(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    async def command(argv: list[str], *, capture: bool = False) -> str:
        commands.append(argv)
        assert capture and "ps" in argv, "an unaudited lifecycle request must not dispatch"
        return "[]"

    original = http_plane.shared.audit_repo.user_event

    def reject_intent(username: str, action: str, *args: Any) -> None:
        if action == "longterm.component.lifecycle.requested":
            raise RuntimeError("synthetic audit unavailable")
        original(username, action, *args)

    monkeypatch.setattr(http_plane.service.component_runtime, "_command", command)
    monkeypatch.setattr(http_plane.shared.audit_repo, "user_event", reject_intent)
    response = await _action(http_plane, "temporal", "start", 0)
    assert response.status_code == 503, response.text
    assert len(commands) == 1
    assert not http_plane.shared.audit_repo.query(action="longterm.component.lifecycle.requested")


async def test_cancelled_dispatched_operation_records_uncertain_and_releases_claim(
    http_plane: HttpControlPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _compose_manifest(tmp_path, monkeypatch)
    runtime = http_plane.service.component_runtime
    dispatched = asyncio.Event()
    release = asyncio.Event()

    async def command(argv: list[str], *, capture: bool = False) -> str:
        if capture:
            assert "ps" in argv
            return "[]"
        assert "up" in argv
        dispatched.set()
        await release.wait()
        return ""

    monkeypatch.setattr(runtime, "_command", command)
    caller = asyncio.create_task(
        runtime.lifecycle(
            "temporal", _admin(http_plane), action="start", confirmed=True, expected_revision=0
        )
    )
    try:
        async with asyncio.timeout(8):
            await dispatched.wait()
        assert len(runtime._operations) == 1
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
        assert len(audits) == 1
        finished = json.loads(audits[0].payload)
        assert finished["outcome"] == "uncertain"
        assert finished["operation_id"] == record["operation_id"]
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
