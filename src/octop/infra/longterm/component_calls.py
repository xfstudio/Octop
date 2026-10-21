"""Bounded component operations shared by local and Temporal workflow nodes.

Only the catalog below is executable. Deployment endpoints and credentials are
resolved on the server, while project bindings are frozen into the task plan.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.adapters import (
    BeatDesignAdapter,
    HindsightAdapter,
    RnaAdapter,
    tenant_namespace,
)
from octop.infra.longterm.evaluation import CommerceBenchAdapter
from octop.infra.longterm.input_bindings import InputBindingError, validate_input_bindings
from octop.infra.users.identity import User

if TYPE_CHECKING:
    from octop.infra.longterm.execution import WorkflowValidationError
    from octop.infra.longterm.service import LongtermService

_PROJECT_READS = {
    "project": "bdesign_project_get",
    "canvas": "bdesign_canvas_get",
    "assets": "bdesign_asset_list",
    "skills": "bdesign_skill_list",
    "generation_models": "bdesign_generation_models",
    "generation_history": "bdesign_generation_history",
    "editor": "bdesign_editor_get",
    "editor_diagnostics": "bdesign_editor_diagnostics",
}
_OPERATIONS = {
    **{("beatdesign", name): ("R0", {}, True) for name in _PROJECT_READS},
    ("rna", "genes"): ("R0", {}, True),
    ("rna", "observability"): ("R0", {}, True),
    ("hindsight", "recall"): ("R0", {"query": "已验证的任务经验", "max_tokens": 2048}, False),
    ("commerceagentbench", "report"): ("R0", {"run_dir": "recorded-run"}, False),
    ("semantica", "index"): ("R1", {"consent": True}, False),
}
_NODE_FIELDS = {
    "id",
    "skill",
    "component_id",
    "operation",
    "input",
    "risk",
    "depends_on",
    "resources",
    "timeout_seconds",
    "max_output_tokens",
    "component_snapshot",
    "beatdesign_card_id",
}
_MAX_RESULT_BYTES = 2 * 1024 * 1024


def _invalid(reason: str) -> WorkflowValidationError:
    from octop.infra.longterm.execution import WorkflowValidationError

    return WorkflowValidationError("component.call: " + reason)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def operation_catalog() -> list[dict[str, Any]]:
    """Return independent public descriptors; no endpoint or credential is exposed."""
    return [
        {
            "component_id": component,
            "operation": operation,
            "risk": spec[0],
            "input_example": copy.deepcopy(spec[1]),
            "project_required": spec[2],
        }
        for (component, operation), spec in _OPERATIONS.items()
    ]


def _reference(value: Any, node: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    if "$node" not in value:
        raise _invalid("invalid dependency reference")
    try:
        validate_input_bindings(value, set(node.get("depends_on", [])))
    except InputBindingError as exc:
        raise _invalid("invalid dependency reference") from exc
    return True


def _text(value: Any, node: dict[str, Any], name: str, maximum: int) -> None:
    if _reference(value, node):
        return
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid(name + " must be a bounded non-empty string")


def _relative_run(value: str) -> None:
    path = PurePosixPath(value)
    if (
        "\\" in value
        or ":" in value
        or "\x00" in value
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise _invalid("run_dir must stay below the private report directory")


def validate_component_call(node: dict[str, Any]) -> None:
    """Validate a finite allowlisted operation, including unresolved input references."""
    if not isinstance(node, dict) or node.get("skill") != "component.call":
        raise _invalid("component.call skill required")
    if set(node) - _NODE_FIELDS:
        raise _invalid("unsupported node fields")
    component, operation = node.get("component_id"), node.get("operation")
    if not isinstance(component, str) or not isinstance(operation, str):
        raise _invalid("component and operation are required")
    spec = _OPERATIONS.get((component, operation))
    if spec is None:
        raise _invalid("operation is outside the component catalog")
    if node.get("risk") != spec[0]:
        raise _invalid("risk must match the operation catalog")
    dependencies = node.get("depends_on", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) for item in dependencies
    ):
        raise _invalid("depends_on must contain node IDs")
    data = node.get("input", {})
    if not isinstance(data, dict):
        raise _invalid("input must be an object")
    if component in {"beatdesign", "rna"}:
        if data:
            raise _invalid("project reads do not accept caller arguments")
    elif component == "hindsight":
        if set(data) - {"query", "max_tokens"}:
            raise _invalid("unsupported memory arguments")
        _text(data.get("query"), node, "query", 8000)
        maximum = data.get("max_tokens", 2048)
        if type(maximum) is not int or not 128 <= maximum <= 4096:
            raise _invalid("max_tokens must be an integer in 128..4096")
    elif component == "commerceagentbench":
        if set(data) - {"run_dir", "expected_task_id"}:
            raise _invalid("unsupported report arguments")
        _text(data.get("run_dir"), node, "run_dir", 400)
        if isinstance(data["run_dir"], str):
            _relative_run(data["run_dir"])
        if "expected_task_id" in data:
            _text(data["expected_task_id"], node, "expected_task_id", 200)
    elif set(data) != {"consent"} or data["consent"] is not True:
        raise _invalid("Semantica indexing requires literal explicit consent")
    if "component_snapshot" in node:
        snapshot = node["component_snapshot"]
        keys = {"version", "component_id", "operation", "deployment_digest"}
        if spec[2]:
            keys |= {"project_id", "binding_revision"}
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != keys
            or type(snapshot.get("version")) is not int
            or snapshot["version"] != 1
            or snapshot.get("component_id") != component
            or snapshot.get("operation") != operation
            or not isinstance(snapshot.get("deployment_digest"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", snapshot["deployment_digest"])
        ):
            raise _invalid("invalid frozen component snapshot")
        if spec[2] and (
            not isinstance(snapshot["project_id"], str)
            or not snapshot["project_id"]
            or type(snapshot["binding_revision"]) is not int
            or snapshot["binding_revision"] < 0
        ):
            raise _invalid("invalid frozen project binding")
    try:
        if len(_canonical(node).encode()) > 128 * 1024:
            raise _invalid("node exceeds its size limit")
    except (TypeError, ValueError) as exc:
        raise _invalid("node must contain finite JSON data") from exc


def _deployment_digest(service: LongtermService, component: str) -> str:
    config = service.components.resolve(component)
    if component == "beatdesign":
        target = {"endpoint": config.get("endpoint", "")}
    elif component in {"rna", "hindsight"}:
        target = {"base_url": config.get("base_url", "")}
    else:
        key = "storage_subdirectory" if component == "semantica" else "results_subdirectory"
        target = {"directory": str(service.components.directory(component, key, values=config))}
    return hashlib.sha256(_canonical(target).encode()).hexdigest()


def freeze_component_call(
    service: LongtermService, tenant_id: str, user: User, node: dict[str, Any]
) -> dict[str, Any]:
    """Create a server-owned snapshot; caller-supplied snapshots are never trusted."""
    from octop.infra.longterm.project_tools import ProjectToolsService

    if "component_snapshot" in node:
        raise _invalid("component_snapshot is server generated")
    validate_component_call(node)
    service.authorize(tenant_id, user, write=True)
    component, operation = node["component_id"], node["operation"]
    service.components.require_enabled(component)
    snapshot = {
        "version": 1,
        "component_id": component,
        "operation": operation,
        "deployment_digest": _deployment_digest(service, component),
    }
    if _OPERATIONS[(component, operation)][2]:
        binding = ProjectToolsService(service).binding(component, tenant_id, user)
        if not binding["project_id"]:
            raise _invalid("project binding required")
        snapshot.update(project_id=binding["project_id"], binding_revision=binding["revision"])
    return snapshot


def _creator(service: LongtermService, task: dict[str, Any]) -> User:
    creator_id = task.get("creator_user_id")
    if type(creator_id) is not int:
        raise OctopError(ErrorCode.FORBIDDEN, "task creator unavailable")
    row = service.services.user_repo.get(creator_id)
    if row is None or row.disabled:
        raise OctopError(ErrorCode.FORBIDDEN, "task creator unavailable")
    return User(row.id, row.username, row.role, row.display_name, row.locale, row.permissions)


def _check_result(
    service: LongtermService,
    component: str,
    result: dict[str, Any],
    original_config: dict[str, Any],
) -> None:
    from octop.infra.longterm.components import FIELDS
    from octop.infra.longterm.service import validate_public_data

    validate_public_data(result)
    serialized = _canonical(result)
    if len(serialized.encode()) > _MAX_RESULT_BYTES:
        raise _invalid("component result exceeds its size limit")
    config = service.components.resolve(component)
    for name, field in FIELDS[component].items():
        if field[0] == "secret":
            for value in (original_config.get(name), config.get(name)):
                if (
                    isinstance(value, str)
                    and value
                    and json.dumps(value, ensure_ascii=False)[1:-1] in serialized
                ):
                    raise _invalid("component returned private credentials")


async def execute_component_call(
    service: LongtermService, node: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    """Execute one scoped call; invocation intent/completion belongs to the runtime."""
    from octop.infra.longterm.execution import WorkflowValidationError, authorized
    from octop.infra.longterm.project_tools import ProjectToolsService

    validate_component_call(node)
    if "component_snapshot" not in node:
        raise _invalid("frozen component snapshot required")
    data = copy.deepcopy(node.get("input", {}))
    if any(isinstance(value, dict) for value in data.values()):
        raise _invalid("dependency references must be resolved before dispatch")
    component, operation = node["component_id"], node["operation"]
    snapshot = node["component_snapshot"]
    tenant_id, task_id, space_id = task["tenant_id"], task["task_id"], task["space_id"]
    user = await asyncio.to_thread(_creator, service, task)

    def check_authority() -> None:
        _creator(service, task)
        service.authorize(tenant_id, user, write=True)
        latest = service.task(tenant_id, task_id, user)
        if latest["creator_user_id"] != task["creator_user_id"] or latest["space_id"] != space_id:
            raise OctopError(ErrorCode.FORBIDDEN, "private task scope changed")
        service._assert_task_authority(tenant_id, task_id, user)
        service.components.require_enabled(component)
        if _deployment_digest(service, component) != snapshot["deployment_digest"]:
            raise OctopError(ErrorCode.LONGTERM_CONFLICT, "frozen component deployment changed")
        if _OPERATIONS[(component, operation)][2]:
            binding = ProjectToolsService(service).binding(component, tenant_id, user)
            if (binding["project_id"], binding["revision"]) != (
                snapshot["project_id"],
                snapshot["binding_revision"],
            ):
                raise OctopError(ErrorCode.LONGTERM_CONFLICT, "frozen project binding changed")
        if component == "semantica":
            execution = task.get("execution", {})
            checkpoint = task.get("checkpoint", {})
            if (
                execution.get("plan_digest") != task.get("plan_digest")
                or type(execution.get("cycle")) is not int
                or not authorized(
                    node,
                    {**task, "control": latest.get("control", {})},
                    {
                        "plan_digest": task["plan_digest"],
                        "cycle": execution["cycle"],
                        "actions": checkpoint.get("actions", 0),
                    },
                    now=datetime.now(UTC),
                )
            ):
                raise OctopError(ErrorCode.FORBIDDEN, "Semantica node approval required")

    await asyncio.to_thread(check_authority)
    config = await asyncio.to_thread(service.components.resolve, component)
    try:
        async with asyncio.timeout(30):
            if component == "beatdesign":
                if not config.get("endpoint"):
                    raise _invalid("component not configured")
                adapter = BeatDesignAdapter(
                    config["endpoint"],
                    {tenant_id: snapshot["project_id"]},
                    token=config.get("token"),
                    authorization_check=check_authority,
                )
                output = await adapter.call_tool(tenant_id, _PROJECT_READS[operation], {})
                source = "beatdesign_mcp"
            elif component == "rna":
                if not config.get("base_url"):
                    raise _invalid("component not configured")
                rna = RnaAdapter(
                    config["base_url"],
                    {tenant_id: snapshot["project_id"]},
                    token=config.get("token"),
                    authorization_check=check_authority,
                )
                output = await (
                    rna.genes(tenant_id) if operation == "genes" else rna.observability(tenant_id)
                )
                source = "rna_http"
            elif component == "hindsight":
                if not config.get("base_url"):
                    raise _invalid("component not configured")
                memory = HindsightAdapter(
                    config["base_url"],
                    api_key=config.get("api_key"),
                    authorization_check=check_authority,
                )
                try:
                    output = await memory.recall(
                        tenant_id=tenant_id + ":space:" + space_id,
                        query=data["query"],
                        max_tokens=data.get("max_tokens", 2048),
                    )
                finally:
                    await memory.close()
                source = "hindsight"
            elif component == "commerceagentbench":

                def load_report() -> dict[str, Any]:
                    check_authority()
                    root = service.components.directory(component, "results_subdirectory")
                    private_directory = root / tenant_namespace(tenant_id) / space_id
                    private_root = private_directory.resolve()
                    if (
                        private_root != private_directory
                        or not private_root.is_relative_to(root.resolve())
                        or not private_root.is_dir()
                    ):
                        raise _invalid("private report directory unavailable")
                    return CommerceBenchAdapter(private_root).load_run(
                        data["run_dir"], expected_task_id=data.get("expected_task_id")
                    )

                output = await asyncio.to_thread(load_report)
                source = "CommerceAgentBench"
            else:
                output = await service.provenance(tenant_id, task_id, user, consent=True)
                source = "semantica"
    except (OctopError, WorkflowValidationError):
        raise
    except Exception as exc:
        raise OctopError(ErrorCode.LONGTERM_UNAVAILABLE, "component operation unavailable") from exc
    result = {
        "source": source,
        "component_id": component,
        "operation": operation,
        "output": output,
        "usage": {"total_tokens": 0},
    }
    await asyncio.to_thread(check_authority)
    await asyncio.to_thread(_check_result, service, component, result, config)
    return result
