"""Private operation sessions: bounded readback, explicit imports and draft results.

These sessions never execute task nodes or grant approval. Native consoles are
independent applications; their results are read through authenticated adapters.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.longterm import AuthorityRevoked, RevisionConflict, canonical, digest
from octop.infra.db.repos.operation_sessions import OperationSessionRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.adapters import (
    BeatDesignAdapter,
    HindsightAdapter,
    RnaAdapter,
    tenant_namespace,
)
from octop.infra.longterm.component_calls import (
    _PROJECT_READS,
    _deployment_digest,
    operation_catalog,
    validate_component_call,
)
from octop.infra.longterm.component_runtime import _INTERNAL, _console_url
from octop.infra.longterm.components import FIELDS
from octop.infra.longterm.evaluation import CommerceBenchAdapter
from octop.infra.longterm.execution import WorkflowValidationError
from octop.infra.longterm.project_tools import ProjectToolsService
from octop.infra.users.identity import User
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop.infra.longterm.service import LongtermService

_MAX_INPUT = 128 * 1024
_MAX_RESULT = 2 * 1024 * 1024
_TASK_OPERATIONS = {
    ("octop", "task"),
    ("octop", "node_result"),
    ("temporal", "status"),
    ("semantica", "receipt"),
}


def _invalid(reason: str) -> OctopError:
    return OctopError(
        ErrorCode.LONGTERM_INVALID, "invalid component session", details={"reason": reason}
    )


def _conflict(reason: str) -> OctopError:
    return OctopError(
        ErrorCode.LONGTERM_CONFLICT, "component session changed", details={"reason": reason}
    )


def operation_session_catalog() -> list[dict[str, Any]]:
    """Only R0 reads; writes retain their existing task approval flow."""
    result = [item for item in operation_catalog() if item["risk"] == "R0"]
    for component, operation in sorted(_TASK_OPERATIONS):
        result.append(
            {
                "component_id": component,
                "operation": operation,
                "risk": "R0",
                "input_example": {},
                "project_required": False,
            }
        )
    for item in result:
        item.update(
            task_required=(item["component_id"], item["operation"]) in _TASK_OPERATIONS,
            node_required=item["operation"] == "node_result",
        )
    return result


def _bounded(value: Any, maximum: int) -> str:
    from octop.infra.longterm.service import validate_public_data

    # Reject depth and credential fields before encoding or copying untrusted JSON.
    try:
        validate_public_data(value)
        encoded = canonical(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _invalid("finite_bounded_json_required") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise _invalid("session_data_too_large")
    return encoded


class OperationSessionService:
    def __init__(self, longterm_service: LongtermService) -> None:
        self.service = longterm_service
        self.repo = OperationSessionRepo(longterm_service.services.db)

    @staticmethod
    def _db(call: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, **kwargs)
        except AuthorityRevoked as exc:
            raise OctopError(ErrorCode.FORBIDDEN, "private session authority revoked") from exc
        except LookupError as exc:
            raise OctopError(ErrorCode.NOT_FOUND, "private operation session not found") from exc
        except RevisionConflict as exc:
            raise _conflict("session_revision_or_idempotency_conflict") from exc

    def _authority(self, tenant: str, space: str, user: User, *, write: bool) -> None:
        actor = self.service.services.user_repo.get(user.id)
        if actor is None or actor.disabled:
            raise OctopError(ErrorCode.FORBIDDEN, "session authority revoked")
        self.service.authorize(tenant, user, write=write)
        self.service.config(tenant, space, user)

    def _deployment(self, component: str) -> str:
        values = self.service.components.resolve(component)
        if component == "temporal":
            return digest(
                {key: values[key] for key in ("address", "namespace", "tls", "task_queue")}
            )
        if component == "octop":
            return digest({"kind": "octop_local", "root": str(self.service.services.paths.root)})
        return _deployment_digest(self.service, component)

    def _snapshot(self, tenant: str, user: User, component: str) -> dict[str, Any]:
        self.service.components.require_enabled(component)
        keys = {"longterm.component.octop", "longterm.component." + component}
        if component in {"beatdesign", "rna"}:
            keys.add("longterm.component_projects." + component)
        # Hash the raw settings too, and lock/check them during the final SQL CAS.
        guards = {key: digest(self.service.services.settings_repo.get(key)) for key in keys}
        result: dict[str, Any] = {
            "version": 1,
            "deployment_digest": self._deployment(component),
            "settings_digests": guards,
        }
        if component in {"beatdesign", "rna"}:
            binding = ProjectToolsService(self.service).binding(component, tenant, user)
            if not binding["project_id"]:
                raise _invalid("project_binding_required")
            result.update(project_id=binding["project_id"], binding_revision=binding["revision"])
        # Guards must describe the same read of the deployment/binding.
        if any(
            digest(self.service.services.settings_repo.get(key)) != value
            for key, value in guards.items()
        ):
            raise _conflict("component_configuration_changed")
        return result

    def _check(self, document: dict[str, Any], user: User) -> None:
        tenant, space = document["tenant_id"], document["space_id"]
        self._authority(tenant, space, user, write=True)
        if document["owner_user_id"] != user.id:
            raise OctopError(ErrorCode.NOT_FOUND, "operation session not found")
        current = self._snapshot(tenant, user, document["component_id"])
        frozen = {key: value for key, value in document["snapshot"].items() if key != "console"}
        if current != frozen:
            raise _conflict("frozen_component_or_binding_changed")
        if document["task_id"]:
            task = self.service.task(tenant, document["task_id"], user)
            self.service._assert_task_authority(tenant, document["task_id"], user)
            if task["space_id"] != space:
                raise OctopError(ErrorCode.FORBIDDEN, "private task scope changed")
            node_id = document["node_id"]
            if node_id and node_id not in {node["id"] for node in task["workflow"]["nodes"]}:
                raise _conflict("task_node_changed")
            if document["component_id"] == "temporal" and task["mode"] != "temporal":
                raise _invalid("temporal_task_required")

    def _view(self, document: dict[str, Any], user: User) -> dict[str, Any]:
        result = copy.deepcopy(document)
        component = result["component_id"]
        console = {
            "url": _INTERNAL[component],
            "kind": "internal",
            "label": "integration",
            "available": True,
        }
        # Global service consoles may contain other tenants: only current host
        # administrators receive those links. Members always retain Octop access.
        actor = self.service.services.user_repo.get(user.id)
        if actor is not None and not actor.disabled and actor.role == "admin":
            url = self.service.components.resolve(component).get("console_url")
            if url:
                with suppress(ValueError):
                    console.update(url=_console_url(url), kind="external", label="native")
        result["snapshot"]["console"] = console
        return result

    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key):
            raise _invalid("bounded_idempotency_key_required")
        return key

    def create(
        self, tenant: str, space: str, user: User, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._authority(tenant, space, user, write=True)
        if not isinstance(payload, dict) or set(payload) - {
            "component_id",
            "operation",
            "input",
            "task_id",
            "node_id",
            "idempotency_key",
        }:
            raise _invalid("unsupported_session_fields")
        key = self._key(payload)
        component, operation = payload.get("component_id"), payload.get("operation")
        if not isinstance(component, str) or not isinstance(operation, str):
            raise _invalid("component_operation_required")
        if (component, operation) not in {
            (item["component_id"], item["operation"]) for item in operation_session_catalog()
        }:
            raise _invalid("unsupported_read_operation")
        data = payload.get("input", {})
        if not isinstance(data, dict):
            raise _invalid("input_object_required")
        _bounded(data, _MAX_INPUT)
        self._screen(data)
        if (component, operation) in _TASK_OPERATIONS:
            if data:
                raise _invalid("task_read_arguments_not_supported")
        else:
            try:
                validate_component_call(
                    {
                        "skill": "component.call",
                        "component_id": component,
                        "operation": operation,
                        "risk": "R0",
                        "input": data,
                    }
                )
            except WorkflowValidationError as exc:
                raise _invalid("invalid_read_arguments") from exc
        task_id, node_id = payload.get("task_id"), payload.get("node_id")
        for identifier in (task_id, node_id):
            if identifier is not None and (
                not isinstance(identifier, str) or not 1 <= len(identifier) <= 200
            ):
                raise _invalid("invalid_task_or_node_id")
        if ((component, operation) in _TASK_OPERATIONS or node_id) and not task_id:
            raise _invalid("task_required")
        if operation == "node_result" and not node_id:
            raise _invalid("node_required")
        document = {
            "session_id": new_ulid(),
            "tenant_id": tenant,
            "space_id": space,
            "owner_user_id": user.id,
            "component_id": component,
            "operation": operation,
            "input": copy.deepcopy(data),
            "task_id": task_id,
            "node_id": node_id,
            "snapshot": self._snapshot(tenant, user, component),
            "candidate": None,
            "result": None,
            "result_digest": None,
            "source": None,
            "draft_only": True,
        }
        self._check(document, user)
        created = self._db(
            self.repo.create, document, key, digest({"action": "create", "payload": payload})
        )
        self._check(created, user)
        return self._view(created, user)

    def list(self, tenant: str, space: str, user: User) -> list[dict[str, Any]]:
        self._authority(tenant, space, user, write=False)
        return [self._view(item, user) for item in self._db(self.repo.list, tenant, space, user.id)]

    def get(self, tenant: str, space: str, user: User, session_id: str) -> dict[str, Any]:
        self._authority(tenant, space, user, write=False)
        return self._view(self._db(self.repo.get, tenant, space, user.id, session_id), user)

    def _action(
        self,
        tenant: str,
        space: str,
        user: User,
        session_id: str,
        payload: dict[str, Any],
        action: str,
    ) -> tuple[dict[str, Any], str, str, dict[str, Any] | None]:
        extras = (
            {"result"} if action == "import" else {"result_digest"} if action == "commit" else set()
        )
        if not isinstance(payload, dict) or set(payload) - (
            {"expected_revision", "idempotency_key"} | extras
        ):
            raise _invalid("unsupported_action_fields")
        expected = payload.get("expected_revision")
        if type(expected) is not int or not 1 <= expected < 2**31:
            raise _invalid("expected_revision_required")
        key = self._key(payload)
        document = self._db(self.repo.get, tenant, space, user.id, session_id)
        self._check(document, user)
        request = digest({"action": action, "session_id": session_id, "payload": payload})
        prior = self._db(self.repo.prior, tenant, space, user.id, key, request)
        if prior is None and (
            document["revision"] != expected or document["status"] in {"committed", "cancelled"}
        ):
            raise _conflict("session_revision_or_closed")
        return document, key, request, prior

    def _screen(self, result: Any, configs: Sequence[dict[str, Any]] | None = None) -> None:
        encoded = _bounded(result, _MAX_RESULT)
        # Imported and upstream output must not persist known device credentials.
        pairs = [(name, self.service.components.resolve(name)) for name in FIELDS]
        for component, values in pairs:
            for field, spec in FIELDS[component].items():
                if spec[0] != "secret":
                    continue
                candidates = [values.get(field)]
                if configs is not None:
                    candidates.extend(config.get(field) for config in configs)
                for secret in candidates:
                    if (
                        isinstance(secret, str)
                        and secret
                        and json.dumps(secret, ensure_ascii=False)[1:-1] in encoded
                    ):
                        raise _invalid("component_result_contains_credentials")

    def _envelope(
        self, document: dict[str, Any], output: Any, source: str, *, verified: bool
    ) -> dict[str, Any]:
        return {
            "source": source,
            "component_id": document["component_id"],
            "operation": document["operation"],
            "output": output,
            "verified": verified,
            "collected_at": now_ts(),
            "draft_only": True,
            "promotion_allowed": False,
            "usage": {"total_tokens": 0},
        }

    def _change(
        self,
        document: dict[str, Any],
        user: User,
        payload: dict[str, Any],
        key: str,
        request: str,
        action: str,
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._check(document, user)
        if candidate is not None:
            self._screen(candidate)
        result = self._db(
            self.repo.change,
            document,
            expected_revision=payload["expected_revision"],
            key=key,
            request=request,
            action=action,
            candidate=candidate,
            result_digest=payload.get("result_digest"),
        )
        return self._view(result, user)

    async def readback(
        self, tenant: str, space: str, user: User, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        document, key, request, prior = await asyncio.to_thread(
            self._action, tenant, space, user, session_id, payload, "readback"
        )
        if prior is not None:
            return await asyncio.to_thread(self._view, prior, user)
        original = await asyncio.to_thread(
            self.service.components.resolve, document["component_id"]
        )
        try:
            # No transaction remains open while waiting on an upstream service.
            async with asyncio.timeout(30):
                output, source = await self._read(document, user)
        except OctopError:
            raise
        except Exception as exc:
            raise OctopError(
                ErrorCode.LONGTERM_UNAVAILABLE, "component readback unavailable"
            ) from exc
        candidate = self._envelope(document, output, source, verified=True)
        await asyncio.to_thread(self._check, document, user)
        await asyncio.to_thread(self._screen, candidate, [original])
        return await asyncio.to_thread(
            self._change, document, user, payload, key, request, "readback", candidate
        )

    def import_result(
        self, tenant: str, space: str, user: User, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or "result" not in payload:
            raise _invalid("import_result_required")
        self._authority(tenant, space, user, write=True)
        self._screen(payload["result"])
        document, key, request, prior = self._action(
            tenant, space, user, session_id, payload, "import"
        )
        if prior is not None:
            return self._view(prior, user)
        candidate = self._envelope(
            document, copy.deepcopy(payload["result"]), "manual_unverified", verified=False
        )
        return self._change(document, user, payload, key, request, "import", candidate)

    def commit(
        self, tenant: str, space: str, user: User, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("result_digest"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", payload["result_digest"])
        ):
            raise _invalid("result_digest_required")
        document, key, request, prior = self._action(
            tenant, space, user, session_id, payload, "commit"
        )
        if prior is not None:
            return self._view(prior, user)
        self._screen(document["candidate"])
        return self._change(document, user, payload, key, request, "commit")

    def cancel(
        self, tenant: str, space: str, user: User, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        document, key, request, prior = self._action(
            tenant, space, user, session_id, payload, "cancel"
        )
        if prior is not None:
            return self._view(prior, user)
        return self._change(document, user, payload, key, request, "cancel")

    async def _read(self, document: dict[str, Any], user: User) -> tuple[Any, str]:
        component, operation = document["component_id"], document["operation"]
        tenant, space, task_id = document["tenant_id"], document["space_id"], document["task_id"]
        data = document["input"]

        def check() -> None:
            self._check(document, user)

        if component in {"beatdesign", "rna"}:
            values = await asyncio.to_thread(self.service.components.resolve, component)
            project = {tenant: document["snapshot"]["project_id"]}
            await asyncio.to_thread(check)
            if component == "beatdesign":
                if not values.get("endpoint"):
                    raise _invalid("component_not_configured")
                beat = BeatDesignAdapter(
                    values["endpoint"],
                    project,
                    token=values.get("token"),
                    authorization_check=check,
                )
                return await beat.call_tool(tenant, _PROJECT_READS[operation], {}), "beatdesign_mcp"
            if not values.get("base_url"):
                raise _invalid("component_not_configured")
            rna = RnaAdapter(
                values["base_url"], project, token=values.get("token"), authorization_check=check
            )
            output = await (
                rna.genes(tenant) if operation == "genes" else rna.observability(tenant)
            )
            return output, "rna_http"
        if component == "hindsight":
            values = await asyncio.to_thread(self.service.components.resolve, component)
            if not values.get("base_url"):
                raise _invalid("component_not_configured")
            adapter = HindsightAdapter(
                values["base_url"], api_key=values.get("api_key"), authorization_check=check
            )
            try:
                output = await adapter.recall(
                    tenant_id=tenant + ":space:" + space,
                    query=data["query"],
                    max_tokens=data.get("max_tokens", 2048),
                )
            finally:
                await adapter.close()
            return output, "hindsight"
        if component == "commerceagentbench":

            def report() -> dict[str, Any]:
                check()
                root = self.service.components.directory(component, "results_subdirectory")
                private = root / tenant_namespace(tenant) / space
                resolved = private.resolve()
                if (
                    private != resolved
                    or not resolved.is_relative_to(root.resolve())
                    or not resolved.is_dir()
                ):
                    raise _invalid("private_report_directory_unavailable")
                check()
                return CommerceBenchAdapter(resolved).load_run(
                    data["run_dir"], expected_task_id=data.get("expected_task_id")
                )

            return await asyncio.to_thread(report), "CommerceAgentBench"
        if component == "octop":
            task = await asyncio.to_thread(self.service.task, tenant, task_id, user)
            if operation == "task":
                return {
                    name: task.get(name)
                    for name in (
                        "task_id",
                        "space_id",
                        "goal",
                        "mode",
                        "status",
                        "revision",
                        "checkpoint",
                        "result",
                    )
                }, "octop_task"
            result = task.get("checkpoint", {}).get("node_results", {}).get(document["node_id"])
            if result is None:
                raise _invalid("node_result_unavailable")
            return {
                "task_id": task_id,
                "node_id": document["node_id"],
                "task_revision": task["revision"],
                "node_result": result,
            }, "octop_node_result"
        if component == "semantica":

            def receipt() -> dict[str, Any]:
                evidence = self.service.evidence(tenant, task_id, user)
                if not evidence["valid"]:
                    raise _invalid("invalid_evidence_chain")
                receipts: list[dict[str, Any]] = []
                cursor = 0
                while cursor < evidence["last_sequence"]:
                    page = self.service.repo.events(tenant, task_id, cursor)
                    if not page:
                        break
                    for event in page:
                        if (
                            event["sequence"] <= evidence["last_sequence"]
                            and event["event_type"] == "provenance.exported"
                        ):
                            receipts.append(
                                {
                                    "sequence": event["sequence"],
                                    "digest": event["digest"],
                                    "receipt": event["payload"],
                                }
                            )
                            receipts = receipts[-100:]
                    cursor = page[-1]["sequence"]
                if not receipts:
                    raise _invalid("indexed_receipt_unavailable")
                return {
                    "task_id": task_id,
                    "chain_valid": True,
                    "head_digest": evidence["head_digest"],
                    "last_sequence": evidence["last_sequence"],
                    "receipts": receipts,
                }

            return await asyncio.to_thread(receipt), "octop_semantica_receipt"
        if component == "temporal":
            from octop.infra.longterm.temporal_runtime import connect, workflow_id

            values = await asyncio.to_thread(self.service.components.resolve, component)
            if not values.get("address"):
                raise _invalid("temporal_not_configured")
            await asyncio.to_thread(check)
            client = await connect(
                address=values["address"],
                namespace=values["namespace"],
                tls=values["tls"],
                api_key=values.get("api_key") or None,
            )
            await asyncio.to_thread(check)
            derived_id = workflow_id(tenant, task_id)
            handle = client.get_workflow_handle(derived_id)
            description = await handle.describe()
            # Closed workflows cannot answer queries; describe remains an authentic
            # read of their persisted Temporal execution state.
            status = getattr(description.status, "name", str(description.status))
            result = {"task_id": task_id, "workflow_id": derived_id, "status": status}
            if status == "RUNNING":
                await asyncio.to_thread(check)
                result["runtime_status"] = await handle.query("status")
            return result, "temporal_sdk"
        raise _invalid("unsupported_read_operation")
