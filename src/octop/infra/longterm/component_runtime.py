"""Administrator lifecycle operations on explicitly bound, host-trusted services.

HTTP supplies an action and revision, never an executable, PID or deployment path.
The durable operation claim serializes controllers sharing the settings database.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.components import NAMES, ComponentManager
from octop.infra.users.identity import User

_INTERNAL = {
    "octop": "/workbench/platform?section=tasks",
    "temporal": "/workbench/platform?section=tasks",
    "beatdesign": "/workbench/platform?section=workflow",
    "semantica": "/workbench/platform?section=integrations",
    "hindsight": "/workbench/platform?section=integrations",
    "commerceagentbench": "/workbench/platform?section=integrations",
    "rna": "/workbench/platform?section=integrations",
}
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_STOPPED = {"exited", "created", "dead"}
_DOCKER_STATES = _STOPPED | {"running", "paused", "restarting", "removing"}


def _error(reason: str, status: int = 422) -> OctopError:
    return OctopError(
        ErrorCode.LONGTERM_CONFLICT if status == 409 else ErrorCode.LONGTERM_INVALID,
        "component lifecycle operation failed",
        status=status,
        details={"reason": reason},
    )


def _console_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) <= 32 for c in value):
        raise ValueError("invalid console URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in value
    ):
        raise ValueError("invalid console URL")
    return value


def _read_manifest() -> dict[str, dict[str, Any]]:
    filename = os.getenv("OCTOP_COMPONENT_RUNTIME_FILE")
    if not filename:
        return {}
    path = Path(filename)
    if not path.is_absolute() or path.stat().st_size > 131072:
        raise ValueError("invalid runtime manifest")
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(doc, dict) or set(doc) != {"version", "components"} or doc["version"] != 1:
        raise ValueError("invalid runtime manifest")
    entries = doc["components"]
    if not isinstance(entries, dict) or set(entries) - set(NAMES):
        raise ValueError("invalid components")
    for component_id, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError("invalid runtime binding")
        mode = entry.get("mode")
        allowed = {"mode", "console_url"}
        if mode == "compose":
            allowed |= {"compose_file", "project_name", "services", "profiles"}
            file = Path(entry.get("compose_file", ""))
            if not file.is_absolute() or not file.is_file():
                raise ValueError("invalid compose file")
            if not isinstance(entry.get("project_name"), str) or not _NAME.fullmatch(
                entry["project_name"]
            ):
                raise ValueError("invalid compose project")
            for field in ("services", "profiles"):
                names = entry.get(field, [])
                if (
                    not isinstance(names, list)
                    or len(names) > 20
                    or (field == "services" and not names)
                    or any(not isinstance(n, str) or not _NAME.fullmatch(n) for n in names)
                ):
                    raise ValueError("invalid compose selection")
        elif mode == "docker":
            allowed |= {"project_name", "services"}
            project = entry.get("project_name")
            names = entry.get("services")
            if not isinstance(project, str) or not _PROJECT.fullmatch(project):
                raise ValueError("invalid compose project")
            if (
                not isinstance(names, list)
                or not 1 <= len(names) <= 20
                or any(not isinstance(n, str) or not _NAME.fullmatch(n) for n in names)
                or len(set(names)) != len(names)
            ):
                raise ValueError("invalid docker selection")
        elif mode == "process":
            allowed |= {"argv", "cwd", "env"}
            argv = entry.get("argv")
            if (
                not isinstance(argv, list)
                or not 1 <= len(argv) <= 100
                or any(not isinstance(a, str) or not a or "\x00" in a for a in argv)
            ):
                raise ValueError("invalid process command")
            cwd = Path(entry.get("cwd", ""))
            if not cwd.is_absolute() or not cwd.is_dir():
                raise ValueError("invalid process directory")
            env = entry.get("env", {})
            if not isinstance(env, dict) or any(
                not isinstance(k, str)
                or not k
                or "=" in k
                or "\x00" in k
                or not isinstance(v, str)
                or "\x00" in v
                for k, v in env.items()
            ):
                raise ValueError("invalid process environment")
        elif mode not in {"embedded", "external"}:
            raise ValueError("invalid runtime mode")
        if set(entry) - allowed or (component_id == "octop" and mode != "embedded"):
            raise ValueError("invalid runtime binding")
        if entry.get("console_url"):
            _console_url(entry["console_url"])
    return entries


class UnixDockerAPI:
    """A fixed-version Docker API over a host-configured Unix socket only."""

    def __init__(self) -> None:
        socket = os.environ.get("OCTOP_DOCKER_SOCKET", "/var/run/docker.sock")
        if not socket.startswith("/") or "\x00" in socket:
            raise _error("runtime_unavailable", 503)
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket),
            base_url="http://docker/v1.45/",
            timeout=httpx.Timeout(45, connect=3),
            trust_env=False,
            follow_redirects=False,
        )

    async def __aenter__(self) -> UnixDockerAPI:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            async with self.client.stream(method, path, params=params) as response:
                if response.status_code not in ({200} if method == "GET" else {204, 304}):
                    raise _error("runtime_command_failed", 503)
                if method != "GET":
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1048576:
                        raise _error("runtime_observation_failed", 503)
                return json.loads(body)
        except httpx.TimeoutException as exc:
            raise _error("runtime_timeout", 504) from exc
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _error("runtime_command_failed", 503) from exc


class ComponentRuntimeManager:
    """Process handles are private; Compose and Docker support shared controllers."""

    def __init__(self, components: ComponentManager) -> None:
        self.components = components
        self.services = components.services
        self.owner = uuid4().hex
        self._manifest: dict[str, dict[str, Any]] | None = None
        self._manifest_invalid = False
        self._manifest_lock = asyncio.Lock()
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_stopped: set[str] = set()
        self._operations: set[asyncio.Task[dict[str, Any]]] = set()
        self._closed = False

    async def _io(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(None, partial(fn, *args, **kwargs))

    def _authorize(self, user: User) -> None:
        self.components.assert_admin(user)
        current = self.services.user_repo.get(user.id)
        if current is None or current.disabled or current.role != "admin":
            raise OctopError(
                ErrorCode.FORBIDDEN,
                "administrator authority revoked",
                details={"reason": "runtime_permission_revoked"},
            )

    async def _bindings(self) -> dict[str, dict[str, Any]]:
        async with self._manifest_lock:
            if self._manifest is None:
                try:
                    self._manifest = await self._io(_read_manifest)
                except (OSError, ValueError, TypeError):
                    self._manifest_invalid = True
                    self._manifest = {}
        return self._manifest

    @staticmethod
    def _key(component_id: str) -> str:
        return "longterm.component.runtime." + component_id

    def _record(self, component_id: str) -> tuple[str | None, dict[str, Any]]:
        raw = self.services.settings_repo.get(self._key(component_id))
        return raw, json.loads(raw) if raw else {"revision": 0}

    async def inventory(self, user: User) -> dict[str, Any]:
        await self._io(self._authorize, user)
        rows = await asyncio.gather(*(self.status(key, user) for key in NAMES))
        await self._io(self._authorize, user)
        return {"components": rows}

    async def status(self, component_id: str, user: User) -> dict[str, Any]:
        await self._io(self._authorize, user)
        self.components._known(component_id)
        bindings = await self._bindings()
        entry = bindings.get(component_id, {})
        values = await self._io(self.components.resolve, component_id)
        _, record = await self._io(self._record, component_id)
        default_mode = (
            "external" if component_id in {"hindsight", "beatdesign", "temporal"} else "embedded"
        )
        mode = entry.get("mode", default_mode)
        # Remote API integrations can be disabled locally, but their server is unmanaged.
        if not entry and component_id == "rna" and values.get("base_url"):
            mode = "external"
        state = "enabled" if values["enabled"] else "disabled"
        reason: str | None = "runtime_external" if mode == "external" else "runtime_embedded"
        services: list[dict[str, Any]] = []
        can_start, can_stop = not values["enabled"], bool(values["enabled"])
        if mode in {"process", "compose", "docker"}:
            reason = None
            try:
                if mode == "compose":
                    state, services = await self._observe_compose(entry)
                elif mode == "docker":
                    state, services = await self._observe_docker(entry)
                else:
                    proc = self._processes.get(component_id)
                    if proc is not None:
                        if component_id in self._process_stopped:
                            state = "stopped"
                        elif proc.returncode is None:
                            state = "running"
                        else:
                            # A launcher can exit while descendants still run. Never
                            # overwrite its ownership by treating that as a clean stop.
                            state, reason = "unknown", "runtime_owner_unavailable"
                    elif record.get("process_active"):
                        state, reason = "unknown", "runtime_owner_unavailable"
                    else:
                        state = "stopped"
                    services = [{"name": component_id, "state": state, "health": None}]
            except OctopError:
                state, reason = "unknown", "runtime_observation_failed"
            can_start = state == "stopped"
            can_stop = state in {"running", "partial"}
            if mode == "docker" and state == "partial":
                can_start = any(s["state"] in _STOPPED for s in services)
                can_stop = any(s["state"] in {"running", "paused", "restarting"} for s in services)
        if record.get("pending"):
            if record.get("expires_at", 0) > time.time():
                state = "starting" if record["pending"] == "start" else "stopping"
                reason = "runtime_busy"
                can_start = can_stop = False
            elif mode == "process":
                state, reason = "unknown", "runtime_owner_unavailable"
                can_start = can_stop = False
        if self._manifest_invalid or self._closed:
            state, reason = (
                "unknown",
                "runtime_manifest_invalid" if self._manifest_invalid else "runtime_unavailable",
            )
            can_start = can_stop = False
        url = entry.get("console_url") or values.get("console_url")
        if url:
            try:
                url = _console_url(url)
            except ValueError:
                url = None
        return {
            "component_id": component_id,
            "name": NAMES[component_id],
            "mode": mode,
            "state": state,
            "enabled": values["enabled"],
            "revision": record["revision"],
            "can_start": can_start,
            "can_stop": can_stop,
            "reason": reason,
            "services": services,
            "console": {
                "url": url or _INTERNAL[component_id],
                "kind": "external" if url else "internal",
                "label": "native" if url else "integration",
                "available": True,
            },
        }

    @staticmethod
    def _compose(entry: dict[str, Any]) -> list[str]:
        args = [
            "docker",
            "compose",
            "--file",
            entry["compose_file"],
            "--project-name",
            entry["project_name"],
        ]
        for profile in entry.get("profiles", []):
            args += ["--profile", profile]
        return args

    async def _command(self, argv: list[str], *, capture: bool = False) -> str:
        """Never return raw tool output to HTTP or audit records."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise _error("runtime_unavailable", 503) from exc
        try:
            async with asyncio.timeout(25):
                output, _ = await proc.communicate()
        except (TimeoutError, asyncio.CancelledError) as exc:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error("runtime_timeout", 504) from exc
        if proc.returncode:
            raise _error("runtime_command_failed", 503)
        if output and len(output) > 1048576:
            raise _error("runtime_observation_failed", 503)
        return output.decode("utf-8", errors="replace") if output else ""

    async def _observe_compose(self, entry: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        output = await self._command(
            self._compose(entry) + ["ps", "--all", "--format", "json", *entry["services"]],
            capture=True,
        )
        try:
            # Compose v2 releases emit either an array or newline-delimited objects.
            rows = (
                json.loads(output)
                if output.lstrip().startswith("[")
                else [json.loads(line) for line in output.splitlines() if line.strip()]
            )
            if not isinstance(rows, list):
                raise ValueError("invalid compose output")
            services = [
                {
                    "name": str(row["Service"]),
                    "state": row.get("State")
                    if row.get("State")
                    in {"running", "exited", "created", "dead", "paused", "restarting", "removing"}
                    else "unknown",
                    "health": row.get("Health")
                    if row.get("Health") in {"healthy", "unhealthy", "starting"}
                    else None,
                }
                for row in rows
                if row["Service"] in entry["services"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("runtime_observation_failed", 503) from exc
        if not services or all(s["state"] in {"exited", "created", "dead"} for s in services):
            return "stopped", services
        running = {s["name"] for s in services if s["state"] == "running"}
        ready = all(
            s["state"] == "running" and s["health"] not in {"unhealthy", "starting"}
            for s in services
        )
        return ("running" if ready and running == set(entry["services"]) else "partial"), services

    @staticmethod
    async def _docker_containers(api: UnixDockerAPI, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Discover every bound service and verify immutable IDs before any mutation."""
        containers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for service in entry["services"]:
            labels = {
                "com.docker.compose.project": entry["project_name"],
                "com.docker.compose.service": service,
            }
            rows = await api.request(
                "GET",
                "containers/json",
                params={
                    "all": "true",
                    "filters": json.dumps({"label": [f"{k}={v}" for k, v in labels.items()]}),
                },
            )
            if not isinstance(rows, list) or not rows or len(rows) > 100:
                raise _error("runtime_observation_failed", 503)
            for row in rows:
                if not isinstance(row, dict):
                    raise _error("runtime_observation_failed", 503)
                container_id = row.get("Id")
                if (
                    not isinstance(container_id, str)
                    or not _CONTAINER_ID.fullmatch(container_id)
                    or container_id in seen
                ):
                    raise _error("runtime_observation_failed", 503)
                seen.add(container_id)
                inspected = await api.request("GET", f"containers/{container_id}/json")
                if not isinstance(inspected, dict) or inspected.get("Id") != container_id:
                    raise _error("runtime_observation_failed", 503)
                config = inspected.get("Config")
                actual = config.get("Labels") if isinstance(config, dict) else None
                if (
                    not isinstance(actual, dict)
                    or any(actual.get(k) != v for k, v in labels.items())
                    or not isinstance(actual.get("com.docker.compose.oneoff"), str)
                    or actual["com.docker.compose.oneoff"].lower() != "false"
                ):
                    raise _error("runtime_observation_failed", 503)
                state = inspected.get("State")
                if (
                    not isinstance(state, dict)
                    or not isinstance(state.get("Status"), str)
                    or state["Status"] not in _DOCKER_STATES
                ):
                    raise _error("runtime_observation_failed", 503)
                health = state.get("Health")
                health = health.get("Status") if isinstance(health, dict) else None
                containers.append(
                    {
                        "id": container_id,
                        "name": service,
                        "state": state["Status"],
                        "health": health
                        if isinstance(health, str)
                        and health in {"healthy", "unhealthy", "starting"}
                        else None,
                    }
                )
        return containers

    async def _observe_docker(self, entry: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        try:
            async with asyncio.timeout(25), UnixDockerAPI() as api:
                containers = await self._docker_containers(api, entry)
        except TimeoutError as exc:
            raise _error("runtime_timeout", 504) from exc
        services = [{k: c[k] for k in ("name", "state", "health")} for c in containers]
        if all(s["state"] == "running" for s in services):
            return "running", services
        if all(s["state"] in _STOPPED for s in services):
            return "stopped", services
        return "partial", services

    async def _operate_docker(self, entry: dict[str, Any], user: User, action: str) -> None:
        try:
            # Finish before the durable 90-second claim can expire, including two
            # ordered 30-second graceful stops for Temporal and its worker.
            async with asyncio.timeout(75), UnixDockerAPI() as api:
                containers = await self._docker_containers(api, entry)
                services = entry["services"] if action == "start" else reversed(entry["services"])
                for service in services:
                    selected = sorted(
                        (c for c in containers if c["name"] == service), key=lambda c: c["id"]
                    )
                    for container in selected:
                        await self._io(self._authorize, user)
                        if self._closed:
                            raise _error("runtime_unavailable", 503)
                        await api.request(
                            "POST",
                            f"containers/{container['id']}/{action}",
                            params={"t": "40" if service == "hindsight" else "30"}
                            if action == "stop"
                            else None,
                        )
        except TimeoutError as exc:
            raise _error("runtime_timeout", 504) from exc

    async def lifecycle(
        self, component_id: str, user: User, *, action: str, confirmed: bool, expected_revision: int
    ) -> dict[str, Any]:
        await self._io(self._authorize, user)
        if not confirmed:
            raise _error("runtime_confirmation_required")
        if action not in {"start", "stop"}:
            raise _error("runtime_invalid_action")
        task = asyncio.create_task(self._operate(component_id, user, action, expected_revision))
        self._operations.add(task)
        task.add_done_callback(self._operation_done)
        # A disconnected browser must not release the durable claim mid-command.
        return await asyncio.shield(task)

    def _operation_done(self, task: asyncio.Task[dict[str, Any]]) -> None:
        self._operations.discard(task)
        if not task.cancelled():
            task.exception()  # Consume failures even when the requesting browser disconnected.

    async def _operate(
        self, component_id: str, user: User, action: str, revision: int
    ) -> dict[str, Any]:
        row = await self.status(component_id, user)
        if row["revision"] != revision:
            raise _error("runtime_revision_conflict", 409)
        if not row["can_" + action]:
            raise _error(row["reason"] or "runtime_unavailable", 409)
        raw, record = await self._io(self._record, component_id)
        if record["revision"] != revision:
            raise _error("runtime_revision_conflict", 409)
        claimed = {
            **record,
            "revision": revision + 1,
            "pending": action,
            "owner": self.owner,
            "operation_id": uuid4().hex,
            "expires_at": time.time() + 90,
        }
        encoded = json.dumps(claimed, sort_keys=True)
        await self._io(self._authorize, user)
        if not await self._io(
            self.services.settings_repo.compare_and_set, self._key(component_id), raw, encoded
        ):
            raise _error("runtime_revision_conflict", 409)
        failure: OctopError | None = None
        cancelled = False
        dispatched = False
        try:
            await self._io(
                self.services.audit_repo.user_event,
                user.username,
                "longterm.component.lifecycle.requested",
                component_id,
                json.dumps(
                    {
                        "action": action,
                        "revision": revision + 1,
                        "operation_id": claimed["operation_id"],
                        "mode": row["mode"],
                    }
                ),
            )
            await self._io(self._authorize, user)
            if self._closed:
                raise _error("runtime_unavailable", 503)
            entry = (await self._bindings()).get(component_id, {})
            dispatched = True
            if row["mode"] == "compose":
                command = (
                    ["up", "--detach", "--no-build", "--pull", "never"]
                    if action == "start"
                    else ["stop", "--timeout", "10"]
                )
                await self._command(self._compose(entry) + command + entry["services"])
            elif row["mode"] == "docker":
                await self._operate_docker(entry, user, action)
            elif row["mode"] == "process":
                if action == "start":
                    proc = await asyncio.create_subprocess_exec(
                        *entry["argv"],
                        cwd=entry["cwd"],
                        env={**os.environ, **entry.get("env", {})},
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=os.name != "nt",
                    )
                    self._processes[component_id] = proc
                    self._process_stopped.discard(component_id)
                else:
                    await self._stop_process(component_id)
                claimed["process_active"] = action == "start"
            else:
                await self._io(
                    self.components.save,
                    component_id,
                    user,
                    values={"enabled": action == "start"},
                    secrets={},
                    clear_secrets=[],
                )
            await self._io(self._authorize, user)
        except asyncio.CancelledError:
            cancelled = True
            failure = _error("runtime_timeout", 504)
            if dispatched and row["mode"] == "process" and action == "start":
                claimed["process_active"] = True
        except OctopError as exc:
            failure = exc
        except Exception:
            failure = _error("runtime_command_failed", 503)
        finally:
            claimed["pending"] = None
            claimed["outcome"] = "uncertain" if failure else "completed"
            saved = await self._io(
                self.services.settings_repo.compare_and_set,
                self._key(component_id),
                encoded,
                json.dumps(claimed, sort_keys=True),
            )
            if not saved:
                failure = _error("runtime_revision_conflict", 409)
        await self._io(
            self.services.audit_repo.user_event,
            user.username,
            "longterm.component.lifecycle.finished",
            component_id,
            json.dumps(
                {
                    "action": action,
                    "revision": revision + 1,
                    "operation_id": claimed["operation_id"],
                    "outcome": claimed["outcome"],
                }
            ),
        )
        if cancelled:
            raise asyncio.CancelledError
        if failure:
            raise failure
        return await self.status(component_id, user)

    async def _stop_process(self, component_id: str) -> None:
        proc = self._processes.get(component_id)
        if proc is None:
            raise _error("runtime_owner_unavailable", 409)
        if component_id in self._process_stopped:
            return
        if proc.returncode is not None:
            raise _error("runtime_owner_unavailable", 409)
        if sys.platform == "win32":
            await self._command(["taskkill", "/PID", str(proc.pid), "/T", "/F"])
        else:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
        try:
            async with asyncio.timeout(10):
                await proc.wait()
        except TimeoutError:
            if sys.platform != "win32":
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            else:
                with suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
        if sys.platform != "win32":
            # The parent exiting does not establish that its entire session exited.
            # Finish descendants that ignored TERM before releasing the owner marker.
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        self._process_stopped.add(component_id)

    async def close(self) -> None:
        self._closed = True
        if self._operations:
            await asyncio.gather(*self._operations, return_exceptions=True)
        for component_id in self._processes:
            try:
                await self._stop_process(component_id)
                raw, record = await self._io(self._record, component_id)
                if record.get("owner") == self.owner:
                    record["process_active"] = False
                    await self._io(
                        self.services.settings_repo.compare_and_set,
                        self._key(component_id),
                        raw,
                        json.dumps(record, sort_keys=True),
                    )
            except (OctopError, OSError):
                # Keep the durable owner marker; another process must never guess a PID.
                continue
