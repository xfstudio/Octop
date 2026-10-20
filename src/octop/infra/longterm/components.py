"""Device-local component settings, write-only credentials and read-only probes."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import User

NAMES = {
    "octop": "Octop",
    "temporal": "Temporal Python SDK",
    "beatdesign": "BeatDesign",
    "semantica": "Semantica",
    "hindsight": "Hindsight",
    "commerceagentbench": "CommerceAgentBench",
    "rna": "RNA",
}
# Environment paths and executables are trusted deployment configuration. HTTP
# configuration only selects descendants of these roots, never arbitrary files.
FIELDS: dict[str, dict[str, tuple[str, Any, str]]] = {
    "octop": {
        "facts_enabled": ("boolean", False, ""),
        "fact_reports_subdirectory": ("string", "", ""),
    },
    "temporal": {
        "address": ("string", "", "OCTOP_TEMPORAL_ADDRESS"),
        "namespace": ("string", "default", "OCTOP_TEMPORAL_NAMESPACE"),
        "task_queue": ("string", "octop-longterm", "OCTOP_TEMPORAL_TASK_QUEUE"),
        "tls": ("boolean", False, "OCTOP_TEMPORAL_TLS"),
        "api_key": ("secret", "", "OCTOP_TEMPORAL_API_KEY"),
    },
    "beatdesign": {
        "endpoint": ("string", "", "OCTOP_BEATDESIGN_MCP_URL"),
        "token": ("secret", "", "OCTOP_BEATDESIGN_TOKEN"),
    },
    "semantica": {"storage_subdirectory": ("string", "", "")},
    "hindsight": {
        "base_url": ("string", "", "OCTOP_HINDSIGHT_URL"),
        "api_key": ("secret", "", "OCTOP_HINDSIGHT_API_KEY"),
    },
    "commerceagentbench": {
        "results_subdirectory": ("string", "", ""),
        "evolution_reports_subdirectory": ("string", "", ""),
    },
    "rna": {
        "base_url": ("string", "", "OCTOP_RNA_URL"),
        "token": ("secret", "", "OCTOP_RNA_TOKEN"),
        "model_json": ("string", "", "OCTOP_RNA_MODEL_JSON"),
        "api_key": ("secret", "", "OCTOP_RNA_API_KEY"),
    },
}
for _fields in FIELDS.values():
    _fields["enabled"] = ("boolean", True, "")


def _invalid(reason: str) -> OctopError:
    return OctopError.localized(ErrorCode.LONGTERM_INVALID, details={"reason": reason})


class ComponentManager:
    def __init__(self, services: SharedServices) -> None:
        self.services = services

    @staticmethod
    def assert_admin(user: User) -> None:
        if not user.is_admin:
            raise OctopError.localized(ErrorCode.FORBIDDEN)

    @staticmethod
    def _known(component_id: str) -> None:
        if component_id not in FIELDS:
            raise OctopError.localized(ErrorCode.NOT_FOUND)

    @staticmethod
    def _key(component_id: str) -> str:
        return "longterm.component." + component_id

    def resolve(self, component_id: str) -> dict[str, Any]:
        """Internal effective settings; contains secrets and must never be serialized."""
        self._known(component_id)
        raw = self.services.settings_repo.get(self._key(component_id))
        saved = json.loads(raw) if raw else {}
        result: dict[str, Any] = {}
        for key, (kind, default, env) in FIELDS[component_id].items():
            value = os.getenv(env, default) if env else default
            if kind == "boolean" and isinstance(value, str):
                value = value.lower() in {"1", "true", "yes"}
            if component_id == "octop" and key == "facts_enabled":
                value = bool(os.getenv("OCTOP_FACT_REPORTS_ROOT"))
            if component_id == "rna" and key == "api_key" and not value:
                value = os.getenv(os.getenv("OCTOP_RNA_API_KEY_ENV", ""), "")
            if kind == "secret":
                secret = self.services.secret_repo.get(self._key(component_id) + "." + key)
                result[key] = secret.decode() if secret is not None else value
            else:
                result[key] = saved.get(key, value)
        return result

    def require_enabled(self, component_id: str) -> None:
        if not self.resolve(component_id)["enabled"]:
            raise OctopError.localized(
                ErrorCode.LONGTERM_UNAVAILABLE, details={"reason": "component_disabled"}
            )

    def directory(
        self, component_id: str, key: str, *, values: dict[str, Any] | None = None
    ) -> Path:
        bases = {
            ("octop", "fact_reports_subdirectory"): ("OCTOP_FACT_REPORTS_ROOT", "fact-reports"),
            ("semantica", "storage_subdirectory"): ("", "provenance"),
            ("commerceagentbench", "results_subdirectory"): (
                "OCTOP_BENCH_RESULTS_ROOT",
                "bench-results",
            ),
            ("commerceagentbench", "evolution_reports_subdirectory"): (
                "OCTOP_EVOLUTION_REPORTS_ROOT",
                "evolution-reports",
            ),
        }
        env, fallback = bases[(component_id, key)]
        root = Path(os.getenv(env) or self.services.paths.root / "longterm" / fallback).resolve()
        result = (root / str((values or self.resolve(component_id))[key])).resolve()
        if not result.is_relative_to(root):
            raise _invalid("component_directory_outside_managed_root")
        return result

    def config(self, component_id: str, user: User) -> dict[str, Any]:
        self.assert_admin(user)
        values = self.resolve(component_id)
        secrets = {key for key, spec in FIELDS[component_id].items() if spec[0] == "secret"}
        return {
            "component_id": component_id,
            "values": {key: value for key, value in values.items() if key not in secrets},
            "secret_configured": {key: bool(values[key]) for key in secrets},
            "fields": [
                {
                    "key": key,
                    "type": "boolean" if spec[0] == "boolean" else "string",
                    "secret": key in secrets,
                    "required": False,
                    "configured": bool(values[key]),
                    **({"value": values[key]} if key not in secrets else {}),
                }
                for key, spec in FIELDS[component_id].items()
            ],
        }

    def save(
        self,
        component_id: str,
        user: User,
        *,
        values: dict[str, Any],
        secrets: dict[str, str],
        clear_secrets: list[str],
    ) -> dict[str, Any]:
        self.assert_admin(user)
        self._known(component_id)
        specs = FIELDS[component_id]
        secret_keys = {key for key, spec in specs.items() if spec[0] == "secret"}
        if (
            set(values) - (set(specs) - secret_keys)
            or (set(secrets) | set(clear_secrets)) - secret_keys
        ):
            raise _invalid("unknown_component_field")
        if set(secrets) & set(clear_secrets):
            raise _invalid("conflicting_secret_update")
        for key, value in values.items():
            if specs[key][0] == "boolean":
                if type(value) is not bool:
                    raise _invalid("component_boolean_required")
            elif not isinstance(value, str) or len(value) > (
                16000 if key == "model_json" else 2000
            ):
                raise _invalid("component_string_invalid")
            elif key.endswith("subdirectory"):
                path = Path(value)
                if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
                    raise _invalid("relative_component_directory_required")
                self.directory(component_id, key, values={**self.resolve(component_id), **values})
            elif key in {"base_url", "endpoint"} and value:
                parsed = urlsplit(value)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise _invalid("component_http_url_required")
            elif key == "address" and value and not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", value):
                raise _invalid("temporal_host_port_required")
            elif key == "model_json" and value:
                try:
                    model = json.loads(value)
                except ValueError as exc:
                    raise _invalid("rna_model_json_invalid") from exc
                if not isinstance(model, dict):
                    raise _invalid("rna_model_json_invalid")
                from .service import validate_public_data

                validate_public_data(model)

                def check_model_fields(item: object) -> None:
                    if isinstance(item, dict):
                        if any(
                            str(key).lower().replace("_", "")
                            in {
                                "token",
                                "apikey",
                                "password",
                                "secret",
                                "credential",
                                "credentials",
                            }
                            for key in item
                        ):
                            raise _invalid("rna_credentials_require_secret_fields")
                        for child in item.values():
                            check_model_fields(child)
                    elif isinstance(item, list):
                        for child in item:
                            check_model_fields(child)

                check_model_fields(model)
        if any(len(value) > 16000 for value in secrets.values()):
            raise _invalid("component_secret_too_long")
        for _ in range(3):
            raw = self.services.settings_repo.get(self._key(component_id))
            saved = json.loads(raw) if raw else {}
            saved.update(values)
            if self.services.settings_repo.compare_and_set(
                self._key(component_id), raw, json.dumps(saved)
            ):
                break
        else:
            raise OctopError.localized(ErrorCode.LONGTERM_CONFLICT)
        for key, value in {**dict.fromkeys(clear_secrets, ""), **secrets}.items():
            if not value and key not in clear_secrets:
                continue
            secret_key = self._key(component_id) + "." + key
            encoded = value.encode()
            self.services.secret_repo.get_or_create(secret_key, value.encode)
            self.services.secret_repo.rotate(secret_key, encoded)
        self.services.settings_repo.delete(self._key(component_id) + ".probe")
        self.services.audit_repo.user_event(
            user.username,
            "longterm.component.config.updated",
            component_id,
            json.dumps(
                {
                    "fields": sorted(values),
                    "secret_fields": sorted(key for key, value in secrets.items() if value),
                    "cleared_secret_fields": sorted(clear_secrets),
                }
            ),
        )
        return self.config(component_id, user)

    def catalog(self, user: User | None = None) -> dict[str, Any]:
        components = []
        for component_id, name in NAMES.items():
            values = self.resolve(component_id)
            configured = self.configured(component_id, values)
            raw = self.services.settings_repo.get(self._key(component_id) + ".probe")
            probe = json.loads(raw) if raw else None
            enabled = values["enabled"]
            healthy = probe["healthy"] if probe and enabled else None
            status = (
                "disabled"
                if not enabled
                else ("healthy" if healthy else "unhealthy")
                if healthy is not None
                else "configured"
                if configured
                else "unconfigured"
            )
            detail = {
                "disabled": "disabled",
                "unconfigured": "configuration_required",
                "configured": "not_checked",
                "healthy": "check_passed",
                "unhealthy": "check_failed",
            }[status]
            components.append(
                {
                    "id": component_id,
                    "name": name,
                    "configured": configured,
                    "healthy": healthy,
                    "status": status,
                    "detail": detail,
                    "checked_at": probe["checked_at"] if probe else None,
                    "fields": self.config(component_id, user)["fields"]
                    if user and user.is_admin
                    else [],
                    "actions": {
                        "octop": ["tenants", "tasks", "evidence"],
                        "temporal": ["durable_tasks"],
                        "beatdesign": ["workflow_import", "project_tools"],
                        "semantica": ["provenance"],
                        "hindsight": ["recall", "retain", "delete"],
                        "commerceagentbench": ["evaluate"],
                        "rna": ["evolution", "project_tools"],
                    }[component_id],
                }
            )
        return {
            "can_manage": bool(user and user.is_admin),
            "scope": "server",
            "components": components,
        }

    def configured(self, component_id: str, values: dict[str, Any]) -> bool:
        if not values["enabled"]:
            return False
        if component_id == "temporal":
            return bool(values["address"])
        if component_id == "hindsight":
            return bool(values["base_url"])
        if component_id == "rna":
            return bool(values["base_url"] or (values["model_json"] and values["api_key"]))
        if component_id == "commerceagentbench":
            return self.directory(component_id, "results_subdirectory").is_dir()
        return True

    async def check(self, component_id: str, user: User) -> dict[str, Any]:
        self.assert_admin(user)
        values = self.resolve(component_id)
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        checks: list[dict[str, Any]] = []

        async def run(code: str, callback: Any) -> None:
            try:
                async with asyncio.timeout(8):
                    ok = await callback()
                checks.append({"code": code, "ok": bool(ok)})
            except Exception:
                checks.append({"code": code, "ok": False})

        async def local(callback: Any) -> Any:
            return await asyncio.get_running_loop().run_in_executor(None, callback)

        async def http_health(base_url: str, token: str) -> bool:
            import httpx

            async with httpx.AsyncClient(
                timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(
                    base_url.rstrip("/") + "/health",
                    headers={"Authorization": "Bearer " + token} if token else {},
                )
                return response.is_success

        if values["enabled"]:
            if component_id == "octop":

                def database_available() -> bool:
                    with self.services.db.connect() as connection:
                        return connection.execute("SELECT 1 AS probe").fetchone() is not None

                await run(
                    "octop_database",
                    lambda: local(database_available),
                )
            elif component_id == "semantica":
                await run(
                    "semantica_sdk",
                    lambda: local(
                        lambda: callable(
                            getattr(
                                importlib.import_module("semantica.provenance.manager"),
                                "ProvenanceManager",
                                None,
                            )
                        )
                    ),
                )
            elif component_id == "beatdesign":
                from .beatdesign_workflow import import_beatdesign_workflow

                await run(
                    "beatdesign_import", lambda: local(lambda: callable(import_beatdesign_workflow))
                )
                if values["endpoint"]:

                    async def mcp_probe() -> bool:
                        from mcp import ClientSession
                        from mcp.client.streamable_http import streamablehttp_client

                        headers = (
                            {"Authorization": "Bearer " + values["token"]}
                            if values["token"]
                            else None
                        )
                        async with (
                            streamablehttp_client(values["endpoint"], headers=headers) as (
                                read,
                                write,
                                _,
                            ),
                            ClientSession(read, write) as session,
                        ):
                            await session.initialize()
                            await session.list_tools()
                        return True

                    await run("beatdesign_mcp", mcp_probe)
            elif component_id == "hindsight":
                await run(
                    "hindsight_health", lambda: http_health(values["base_url"], values["api_key"])
                )
            elif component_id == "commerceagentbench":
                await run(
                    "benchmark_results",
                    lambda: local(
                        lambda: self.directory(component_id, "results_subdirectory").is_dir()
                    ),
                )
                await run(
                    "evolution_reports",
                    lambda: local(
                        lambda: self.directory(
                            component_id, "evolution_reports_subdirectory"
                        ).is_dir()
                    ),
                )
            elif component_id == "rna":
                sdk = Path(
                    os.getenv("OCTOP_RNA_SDK_ROOT")
                    or Path(__file__).resolve().parents[5] / "rna_dev/packages/sdk"
                )
                await run("rna_sdk", lambda: local(lambda: (sdk / "src/evolution.mjs").is_file()))
                await run(
                    "rna_node",
                    lambda: local(
                        lambda: shutil.which(os.getenv("OCTOP_NODE_BINARY", "node")) is not None
                    ),
                )
                if values["base_url"]:
                    await run(
                        "rna_service", lambda: http_health(values["base_url"], values["token"])
                    )
            elif component_id == "temporal":
                from .temporal_runtime import connect, worker_is_polling

                await run(
                    "temporal_sdk",
                    lambda: local(lambda: importlib.util.find_spec("temporalio") is not None),
                )

                async def temporal_probe() -> bool:
                    service_check = {"code": "temporal_service", "ok": False}
                    namespace_check = {"code": "temporal_namespace", "ok": False}
                    checks.extend([service_check, namespace_check])
                    client = await connect(
                        address=values["address"],
                        namespace=values["namespace"],
                        tls=values["tls"],
                        api_key=values["api_key"] or None,
                    )
                    service_check["ok"] = True
                    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

                    await client.workflow_service.describe_namespace(
                        DescribeNamespaceRequest(namespace=values["namespace"])
                    )
                    namespace_check["ok"] = True
                    return await worker_is_polling(
                        client, namespace=values["namespace"], task_queue=values["task_queue"]
                    )

                await run("temporal_worker", temporal_probe)
        result = {
            "component_id": component_id,
            "configured": self.configured(component_id, values),
            "healthy": bool(checks) and all(check["ok"] for check in checks),
            "checked_at": int(time.time()),
            "checks": checks,
        }
        if (
            fingerprint
            != hashlib.sha256(
                json.dumps(self.resolve(component_id), sort_keys=True).encode()
            ).hexdigest()
        ):
            raise OctopError.localized(
                ErrorCode.LONGTERM_CONFLICT, details={"reason": "component_configuration_changed"}
            )
        self.services.settings_repo.set(self._key(component_id) + ".probe", json.dumps(result))
        self.services.audit_repo.user_event(
            user.username,
            "longterm.component.checked",
            component_id,
            json.dumps({"healthy": result["healthy"], "checks": checks}),
        )
        return result
