"""Tenant-bound read access to upstream project resources from the workbench."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from octop.infra.db.repos.longterm import AuthorityRevoked
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.adapters import BeatDesignAdapter, RnaAdapter
from octop.infra.longterm.service import validate_public_data
from octop.infra.users.identity import User

if TYPE_CHECKING:
    from octop.infra.longterm.service import LongtermService

_BEATDESIGN_READS = {
    "project": "bdesign_project_get",
    "canvas": "bdesign_canvas_get",
    "assets": "bdesign_asset_list",
    "skills": "bdesign_skill_list",
    "generation_models": "bdesign_generation_models",
    "generation_history": "bdesign_generation_history",
    "editor": "bdesign_editor_get",
    "editor_diagnostics": "bdesign_editor_diagnostics",
}


def _invalid(reason: str) -> OctopError:
    return OctopError(
        ErrorCode.LONGTERM_INVALID, "invalid project operation", details={"reason": reason}
    )


class ProjectToolsService:
    def __init__(self, service: LongtermService) -> None:
        self.service = service

    @staticmethod
    def _key(component_id: str) -> str:
        if component_id not in {"beatdesign", "rna"}:
            raise _invalid("unsupported_project_component")
        return "longterm.component_projects." + component_id

    def binding(self, component_id: str, tenant_id: str, user: User) -> dict[str, Any]:
        role = self.service.authorize(tenant_id, user)
        raw = self.service.services.settings_repo.get(self._key(component_id))
        state: dict[str, Any] = json.loads(raw) if raw else {"revision": 0, "projects": {}}
        return {
            "component_id": component_id,
            "tenant_id": tenant_id,
            "project_id": state["projects"].get(tenant_id),
            "revision": state["revision"],
            "can_manage": user.is_admin and role in {"owner", "admin"},
        }

    def save_binding(
        self,
        component_id: str,
        tenant_id: str,
        user: User,
        project_id: str | None,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.service.authorize(tenant_id, user, admin=True)
        if not user.is_admin:
            raise OctopError(ErrorCode.FORBIDDEN, "project operation unavailable")
        key = self._key(component_id)
        if project_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", project_id):
            raise _invalid("invalid_project_id")
        settings = self.service.services.settings_repo
        raw = settings.get(key)
        state: dict[str, Any] = json.loads(raw) if raw else {"revision": 0, "projects": {}}
        if state["revision"] != expected_revision:
            raise OctopError(ErrorCode.LONGTERM_CONFLICT, "project operation unavailable")
        projects = dict(state["projects"])
        if project_id and any(
            tid != tenant_id and value == project_id for tid, value in projects.items()
        ):
            raise _invalid("project_already_bound")
        if project_id is None:
            projects.pop(tenant_id, None)
        else:
            projects[tenant_id] = project_id
        value = json.dumps(
            {"revision": expected_revision + 1, "projects": projects}, sort_keys=True
        )
        try:
            changed = self.service.repo.bind_project(
                tenant_id,
                user.id,
                key,
                raw,
                value,
                json.dumps({"component_id": component_id, "project_id": project_id}),
            )
        except AuthorityRevoked as exc:
            raise OctopError(ErrorCode.FORBIDDEN, "project binding authority revoked") from exc
        if not changed:
            raise OctopError(ErrorCode.LONGTERM_CONFLICT, "project operation unavailable")
        return self.binding(component_id, tenant_id, user)

    async def read(
        self,
        component_id: str,
        tenant_id: str,
        space_id: str,
        user: User,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        # Reuse private-space checks before any upstream connection.
        await asyncio.to_thread(self.service.config, tenant_id, space_id, user)
        binding = await asyncio.to_thread(self.binding, component_id, tenant_id, user)
        project_id = binding["project_id"]
        if not project_id:
            raise _invalid("project_binding_required")
        if arguments:
            raise _invalid("project_arguments_not_supported")
        if (component_id == "beatdesign" and operation not in _BEATDESIGN_READS) or (
            component_id == "rna" and operation not in {"genes", "observability"}
        ):
            raise _invalid("unsupported_read_operation")
        await asyncio.to_thread(self.service.components.require_enabled, component_id)
        config = await asyncio.to_thread(self.service.components.resolve, component_id)

        def check_authority() -> None:
            self.service.config(tenant_id, space_id, user)
            self.service.components.require_enabled(component_id)
            if self.binding(component_id, tenant_id, user)["revision"] != binding["revision"]:
                raise OctopError(ErrorCode.LONGTERM_CONFLICT, "project binding changed")

        try:
            await asyncio.to_thread(check_authority)
            async with asyncio.timeout(15):
                if component_id == "beatdesign":
                    endpoint = config.get("endpoint")
                    if not endpoint:
                        raise _invalid("component_not_configured")
                    adapter = BeatDesignAdapter(
                        endpoint,
                        {tenant_id: project_id},
                        token=config.get("token"),
                        authorization_check=check_authority,
                    )
                    response = await adapter.call_tool(tenant_id, _BEATDESIGN_READS[operation], {})
                else:
                    base_url = config.get("base_url")
                    if not base_url:
                        raise _invalid("component_not_configured")
                    rna = RnaAdapter(
                        base_url,
                        {tenant_id: project_id},
                        token=config.get("token"),
                        authorization_check=check_authority,
                    )
                    response = await (
                        rna.genes(tenant_id)
                        if operation == "genes"
                        else rna.observability(tenant_id)
                    )
        except OctopError:
            raise
        except Exception as exc:
            # Never return upstream exception bodies, URLs, or credentials.
            raise OctopError(
                ErrorCode.LONGTERM_UNAVAILABLE, "project operation unavailable"
            ) from exc
        if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode()) > 2097152:
            raise _invalid("project_result_too_large")
        validate_public_data(response)
        # A membership or binding change during the request revokes the result.
        await asyncio.to_thread(check_authority)
        await asyncio.to_thread(
            self.service.services.audit_repo.user_event,
            user.username,
            "longterm.project.read",
            target=tenant_id,
            payload=json.dumps({"component_id": component_id, "operation": operation}),
        )
        return {
            "source": "beatdesign_mcp" if component_id == "beatdesign" else "rna_http",
            "operation": operation,
            "project_id": project_id,
            "result": response,
        }
