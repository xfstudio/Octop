"""Authenticated offline-first control plane; SQL remains the source of authority."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from octop.infra.db.repos.longterm import AuthorityRevoked, canonical, digest
from octop.infra.db.repos.longterm import RevisionConflict as RevisionConflict
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.component_runtime import ComponentRuntimeManager
from octop.infra.longterm.components import ComponentManager
from octop.infra.longterm.execution import compile_workflow
from octop.infra.longterm.runtime import LongTermRuntime
from octop.infra.users.identity import User
from octop.infra.utils.ulid import new_ulid

_SENSITIVE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|credential|cookie|private[_-]?key)",
    re.I,
)
_TERMINAL = {"completed", "cancelled", "failed", "uncertain"}


def _invalid(reason: str, *, status: int = 422) -> OctopError:
    return OctopError(
        ErrorCode.LONGTERM_INVALID,
        "invalid longterm operation",
        status=status,
        details={"reason": reason},
    )


def validate_public_data(value: object, depth: int = 0) -> None:
    """Configuration sync is explicit; credentials and host paths never belong in it."""
    if depth > 24:
        raise _invalid("config_too_deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE.search(str(key)) and not (
                str(key) == "authorization" and isinstance(item, bool)
            ):
                raise _invalid("credentials_are_device_local")
            validate_public_data(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_public_data(item, depth + 1)
    elif isinstance(value, str):
        if re.search(r"(?:^|\s)(?:[A-Za-z]:[\\/]|/(?:home|Users|etc|var)/)", value):
            raise _invalid("absolute_host_paths_are_device_local")
        if re.search(r"\b(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{16,})", value, re.I):
            raise _invalid("credentials_are_device_local")
    elif isinstance(value, float) and not math.isfinite(value):
        raise _invalid("finite_numbers_required")


class LongtermService:
    def __init__(self, services: SharedServices, *, agent_registry: Any = None) -> None:
        self.services = services
        self.repo = services.longterm_repo
        self.agent_registry = agent_registry
        self.components = ComponentManager(services)
        self.component_runtime = ComponentRuntimeManager(self.components)
        self._jobs: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closed = False

    @staticmethod
    def _revoked(exc: AuthorityRevoked) -> OctopError:
        return OctopError(ErrorCode.FORBIDDEN, "private task authority revoked")

    def _assert_task_authority(
        self,
        tenant_id: str,
        task_id: str,
        user: User,
        *,
        require_admin: bool = False,
        write: bool = True,
    ) -> None:
        try:
            self.repo.assert_authority(
                tenant_id, task_id, user.id, require_admin=require_admin, write=write
            )
        except AuthorityRevoked as exc:
            raise self._revoked(exc) from exc

    def authorize(
        self, tenant_id: str, user: User, *, write: bool = False, admin: bool = False
    ) -> str:
        if write:
            self.components.require_enabled("octop")
        role = self.repo.role(tenant_id, user.id)
        # Global Octop administrators do not implicitly become tenant members.
        if (
            role is None
            or (write and role == "viewer")
            or (admin and role not in {"owner", "admin"})
        ):
            raise OctopError(ErrorCode.FORBIDDEN, "tenant membership required")
        return role

    def tenants(self, user: User) -> list[dict[str, Any]]:
        return self.repo.tenants(user.id)

    def create_tenant(self, user: User, name: str) -> dict[str, Any]:
        self.components.require_enabled("octop")
        return self.repo.create_tenant(new_ulid(), self._name(name), user.id)

    @staticmethod
    def _name(name: str) -> str:
        if not name.strip() or len(name.strip()) > 120:
            raise _invalid("name_required_max_120")
        return name.strip()

    def members(self, tenant_id: str, user: User) -> list[dict[str, Any]]:
        self.authorize(tenant_id, user, admin=True)
        return self.repo.members(tenant_id)

    def set_member(self, tenant_id: str, user: User, user_id: int, role: str | None) -> None:
        acting_role = self.authorize(tenant_id, user, admin=True)
        if role not in {None, "admin", "member", "viewer"}:
            raise _invalid("invalid_tenant_role")
        existing = self.repo.role(tenant_id, user_id)
        if existing == "owner" or (
            acting_role != "owner" and (role == "admin" or existing == "admin")
        ):
            raise OctopError(ErrorCode.FORBIDDEN, "owner permission required")
        if self.services.user_repo.get(user_id) is None:
            raise OctopError(ErrorCode.NOT_FOUND, "user not found")
        self.repo.set_member(tenant_id, user_id, role)

    def spaces(self, tenant_id: str, user: User) -> list[dict[str, Any]]:
        self.authorize(tenant_id, user)
        return self.repo.spaces(tenant_id, user.id)

    def create_space(self, tenant_id: str, user: User, name: str) -> dict[str, Any]:
        self.authorize(tenant_id, user, write=True)
        return self.repo.create_space(tenant_id, new_ulid(), self._name(name), user.id)

    def import_workflow(
        self, tenant_id: str, user: User, document: dict[str, Any]
    ) -> dict[str, Any]:
        from octop.infra.longterm.beatdesign_workflow import import_beatdesign_workflow

        self.authorize(tenant_id, user, write=True)
        self.components.require_enabled("beatdesign")
        validate_public_data(document)
        try:
            document_size = len(canonical(document).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise _invalid("canvas_document_must_be_finite_json") from exc
        if document_size > 1048576:
            raise _invalid("canvas_import_too_large")
        workflow = import_beatdesign_workflow(document)
        compile_workflow(
            {
                "tenant_id": tenant_id,
                "task_id": "workflow-import",
                "goal": "validate",
                "workflow": workflow,
            }
        )
        return {"workflow": workflow}

    def config(self, tenant_id: str, space_id: str, user: User) -> dict[str, Any]:
        self.authorize(tenant_id, user)
        result = self.repo.config(tenant_id, space_id, user.id)
        if result is None:
            raise OctopError(ErrorCode.NOT_FOUND, "space not found")
        return result

    def save_config(
        self,
        tenant_id: str,
        space_id: str,
        user: User,
        *,
        expected_revision: int,
        config: dict[str, Any],
        device_id: str,
    ) -> dict[str, Any]:
        self.authorize(tenant_id, user, write=True)
        self.config(tenant_id, space_id, user)
        if set(config) - {"schema_version", "workspace_name", "workflow", "defaults"}:
            raise _invalid("sync_config_fields_not_allowed")
        if config.get("schema_version") != 1:
            raise _invalid("config_schema_version_required")
        validate_public_data(config)
        if len(canonical(config).encode("utf-8")) > 131072:
            raise _invalid("config_too_large")
        if "workflow" in config and (
            not isinstance(config["workflow"], dict)
            or not isinstance(config["workflow"].get("nodes"), list)
        ):
            raise _invalid("config_workflow_nodes_required")
        if "workflow" in config and config["workflow"].get("nodes"):
            compile_workflow(
                {
                    "tenant_id": tenant_id,
                    "task_id": "configuration",
                    "goal": "validate",
                    "workflow": config["workflow"],
                }
            )
        return self.repo.save_config(
            tenant_id,
            space_id,
            user.id,
            expected_revision=expected_revision,
            config=config,
            device_id=device_id,
        )

    def task(self, tenant_id: str, task_id: str, user: User) -> dict[str, Any]:
        self.authorize(tenant_id, user)
        task = self.repo.task(tenant_id, task_id)
        if task is None or task["creator_user_id"] != user.id:
            raise OctopError(ErrorCode.NOT_FOUND, "task not found")
        self.config(tenant_id, task["space_id"], user)
        return task

    def tasks(self, tenant_id: str, user: User) -> list[dict[str, Any]]:
        self.authorize(tenant_id, user)
        return self.repo.tasks(tenant_id, user.id)

    async def freeze_skills(
        self, tenant_id: str, user: User, workflow: dict[str, Any]
    ) -> dict[str, str]:
        self.authorize(tenant_id, user, write=True)
        snapshots: dict[str, str] = {}
        if not isinstance(workflow.get("nodes"), list):
            raise _invalid("workflow_nodes_required")
        for node in workflow.get("nodes", []):
            if not isinstance(node, dict):
                raise _invalid("workflow_node_invalid")
            skill = str(node.get("skill") or "agent.analyze")
            if skill in {"local.echo", "agent.analyze", "component.call"}:
                continue
            agent_id = str(node.get("agent_id") or "")
            row = self.services.agent_repo.get(agent_id)
            if row is None or row.user_id != user.id:
                raise OctopError(ErrorCode.FORBIDDEN, "agent ownership required")
            if self.agent_registry is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", skill):
                raise _invalid("skill_reference_not_available")
            agent = self.agent_registry.get_agent(agent_id)
            text = await agent.workspace.aread_text(f"skills/{skill}/SKILL.md")
            if not text or len(text) > 32000:
                raise _invalid("skill_reference_not_available")
            snapshots[str(node.get("id"))] = text
        return snapshots

    def create_task(
        self,
        tenant_id: str,
        user: User,
        data: dict[str, Any],
        *,
        skill_snapshots: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.authorize(tenant_id, user, write=True)
        self.config(tenant_id, str(data["space_id"]), user)
        allowed = {
            "space_id",
            "goal",
            "workflow",
            "mode",
            "max_cycles",
            "acceptance",
            "idempotency_key",
        }
        if set(data) - allowed:
            raise _invalid("task_fields_not_allowed")
        if data.get("mode", "local") not in {"local", "temporal"}:
            raise _invalid("invalid_runtime_mode")
        validate_public_data(data)
        request_digest = digest({"actor": user.id, "data": data})
        key = f"{user.id}:create:{data.get('idempotency_key') or uuid4().hex}"
        prior = self.repo.replay(tenant_id, key, request_digest)
        if prior:
            return prior
        task = {
            "tenant_id": tenant_id,
            "task_id": new_ulid(),
            "creator_user_id": user.id,
            "space_id": data["space_id"],
            "goal": str(data["goal"]).strip(),
            "mode": data.get("mode", "local"),
            "workflow": copy.deepcopy(data["workflow"]),
            "acceptance": copy.deepcopy(data.get("acceptance", {})),
            "control": {},
            "artifacts": {},
        }
        if "max_cycles" in data:
            task["workflow"]["max_cycles"] = data["max_cycles"]
        if len(task["goal"]) > 8000 or len(canonical(task).encode()) > 262144:
            raise _invalid("task_too_large")
        plan = compile_workflow(task)
        # No arbitrary harness tool call is admitted as a read-only node.
        for node in plan["nodes"]:
            if node.get("skill") == "component.call":
                from octop.infra.longterm.component_calls import freeze_component_call

                node["component_snapshot"] = freeze_component_call(self, tenant_id, user, node)
                continue
            agent_id = node.get("agent_id")
            if agent_id:
                row = self.services.agent_repo.get(str(agent_id))
                if row is None or row.user_id != user.id:
                    raise OctopError(ErrorCode.FORBIDDEN, "agent ownership required")
                skill = str(node.get("skill") or "agent.analyze")
                skill_text = (skill_snapshots or {}).get(node["id"], "")
                if skill not in {"local.echo", "agent.analyze"} and not skill_text:
                    raise _invalid("frozen_skill_revision_required")
                provider_name = (row.default_model or "").partition("/")[0]
                provider = self.services.provider_repo.get_by_name(provider_name)
                node["behavior_snapshot"] = {
                    "system_prompt": row.system_prompt or "",
                    "model_ref": row.default_model,
                    "skill_text": skill_text,
                    "skill_digest": digest(skill_text),
                    "provider_digest": digest(
                        {"kind": provider.kind, "base_url": provider.base_url}
                    )
                    if provider
                    else None,
                }
            elif node.get("skill") != "local.echo":
                raise _invalid("agent_reference_required")
        task["workflow"]["nodes"] = plan["nodes"]
        validate_public_data(task)
        if len(canonical(task).encode()) > 524288:
            raise _invalid("frozen_task_too_large")
        task["plan_digest"] = compile_workflow(task)["digest"]
        return self.repo.create_task(task, key, request_digest)

    def command(
        self, tenant_id: str, task_id: str, user: User, data: dict[str, Any]
    ) -> dict[str, Any]:
        self.authorize(tenant_id, user, write=True, admin=data["command"] == "approve")
        task = self.task(tenant_id, task_id, user)
        command = data["command"]
        key = f"{user.id}:{task_id}:{data.get('idempotency_key') or uuid4().hex}"
        request_digest = digest({"actor": user.id, "task_id": task_id, "data": data})
        prior = self.repo.replay(tenant_id, key, request_digest)
        if prior:
            return prior
        if data["expected_revision"] != task["revision"]:
            raise RevisionConflict("task revision conflict")
        status = task["status"]
        control = copy.deepcopy(task.get("control", {}))
        if command == "start" and status == "draft":
            task["status"] = "queued"
        elif command == "pause" and status in {"queued", "running", "waiting_approval", "sleeping"}:
            control["paused"] = True
            task["status"] = "paused"
        elif command == "resume" and status in {"paused", "sleeping"}:
            if task.get("checkpoint", {}).get("decision") == "replan":
                raise _invalid("create_new_task_with_revised_plan", status=409)
            control["paused"] = False
            task["status"] = "queued"
        elif command in {"cancel", "reject"} and status not in _TERMINAL:
            control["cancelled"] = True
            task["status"] = "cancelled"
        elif command == "approve" and status == "waiting_approval":
            self.authorize(tenant_id, user, admin=True)
            node_id = data.get("node_id")
            waiting = task["checkpoint"].get("waiting_nodes", [])
            if data.get("plan_digest") != task["plan_digest"] or node_id not in waiting:
                raise _invalid("approval_must_match_exact_plan_and_waiting_node")
            node = next(n for n in task["workflow"]["nodes"] if n["id"] == node_id)
            if node["risk"] == "R3":
                raise _invalid("R3_disabled")
            grant = {
                "node_id": node_id,
                "plan_digest": task["plan_digest"],
                "approved": True,
                "tenant_id": tenant_id,
                "task_id": task_id,
                "scope": "local" if task["mode"] == "local" else "cloud",
                "cycle": task["checkpoint"].get("cycle", 1),
                "actor_user_id": user.id,
            }
            control.setdefault("approvals", []).append(grant)
            task["status"] = "queued"
        else:
            raise _invalid("command_not_valid_for_current_state", status=409)
        task["control"] = control
        try:
            response = self.repo.command(
                task,
                expected_revision=data["expected_revision"],
                command_key=key,
                request_digest=request_digest,
                command=command,
                actor_user_id=user.id,
                require_admin=command == "approve",
            )
        except AuthorityRevoked as exc:
            raise self._revoked(exc) from exc
        if response["status"] == "queued" or task["mode"] == "temporal":
            self.schedule(tenant_id, task_id)
        return response

    def evidence(
        self, tenant_id: str, task_id: str, user: User, *, after: int = 0
    ) -> dict[str, Any]:
        self.task(tenant_id, task_id, user)
        events: list[dict[str, Any]] = []
        previous = "0" * 64
        cursor, valid = 0, True
        while True:
            page = self.repo.events(tenant_id, task_id, cursor)
            for event in page:
                raw = {k: v for k, v in event.items() if k != "digest"}
                valid = (
                    valid
                    and event["sequence"] == cursor + 1
                    and event["previous_digest"] == previous
                    and digest(raw) == event["digest"]
                )
                previous, cursor = event["digest"], event["sequence"]
                if cursor > after and len(events) < 1000:
                    events.append(event)
            if len(page) < 1000:
                break
        return {
            "events": events,
            "valid": valid,
            "head_digest": previous,
            "last_sequence": cursor,
            "next_cursor": events[-1]["sequence"] if events else after,
        }

    async def recover(self) -> None:
        for tenant_id, task_id in self.repo.dispatchable():
            self.schedule(tenant_id, task_id)

    def schedule(self, tenant_id: str, task_id: str) -> None:
        key = (tenant_id, task_id)
        if self._closed or (key in self._jobs and not self._jobs[key].done()):
            return
        self._jobs[key] = asyncio.create_task(
            self._run(tenant_id, task_id), name=f"longterm-{task_id}"
        )

    async def close(self) -> None:
        self._closed = True
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()
        await self.component_runtime.close()

    async def _run(self, tenant_id: str, task_id: str) -> None:
        while not self._closed:
            task = self.repo.task(tenant_id, task_id)
            if task is None:
                return
            if task["mode"] == "temporal":
                await self._dispatch_temporal(task)
                return
            if task["status"] == "sleeping":
                self.repo.wake_due(tenant_id, task_id)
                await asyncio.sleep(2)
                continue
            if task["status"] not in {"queued", "running"}:
                return
            result = await self.step(tenant_id, task_id)
            if result is None:
                await asyncio.sleep(2)
            elif result not in {"queued", "running", "sleeping"}:
                return
            else:
                await asyncio.sleep(0)

    async def step(self, tenant_id: str, task_id: str) -> str | None:
        """One fenced database-backed activity; Temporal uses this same entry point."""
        self.repo.wake_due(tenant_id, task_id)
        owner = uuid4().hex
        task = self.repo.claim(tenant_id, task_id, owner)
        if task is None:
            return None

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                if not self.repo.renew(tenant_id, task_id, owner):
                    return

        keepalive = asyncio.create_task(heartbeat())
        try:
            role = self.repo.role(tenant_id, task["creator_user_id"])
            if role is None or role == "viewer":
                self.repo.checkpoint(
                    task,
                    owner,
                    task["checkpoint"],
                    status="paused",
                    result={"reason": "tenant_authority_revoked"},
                    events=[],
                )
                return "paused"
            # A process may die after dispatch but before committing its checkpoint.
            from octop.infra.longterm.recovery import recover_execution

            all_events: list[dict[str, Any]] = []
            cursor = 0
            while True:
                events = self.repo.events(tenant_id, task_id, cursor)
                for event in events:
                    cursor = event["sequence"]
                    all_events.append(event)
                if len(events) < 1000:
                    break
            restored, recovery_failure = recover_execution(task, all_events)
            task["checkpoint"] = restored
            if recovery_failure:
                self.repo.checkpoint(
                    task,
                    owner,
                    task["checkpoint"],
                    status=recovery_failure,
                    result={
                        "reason": "write_requires_reconciliation",
                        "goal_outcome": "inconclusive",
                    },
                    events=[],
                )
                return recovery_failure

            async def evidence_writer(event: dict[str, Any]) -> None:
                validate_public_data(event)
                if event.get("kind") == "action_intent":
                    self.repo.permit(task, owner, event)
                else:
                    self.repo.finish_event(task, owner, event)

            async def control_checker(
                current: dict[str, Any], checkpoint: dict[str, Any]
            ) -> dict[str, Any]:
                latest = self.repo.task(tenant_id, task_id)
                role = self.repo.role(tenant_id, task["creator_user_id"])
                if latest is None or role in {None, "viewer"}:
                    return {"paused": True}
                return dict(latest.get("control", {}))

            async def fact_hook(
                current: dict[str, Any], checkpoint: dict[str, Any]
            ) -> dict[str, Any]:
                from octop.infra.longterm.facts import read_fact_report

                root = self.components.directory("octop", "fact_reports_subdirectory")
                report = await asyncio.get_running_loop().run_in_executor(
                    None, read_fact_report, root, current
                )
                validate_public_data(report)
                event = self.repo.append(tenant_id, task_id, "facts.frozen", report)
                return {
                    **report,
                    "projection_digest": event["digest"],
                    "evidence_refs": [
                        f"octop-evidence:{task_id}:{event['sequence']}:{event['digest']}",
                        *report["evidence_refs"],
                    ],
                }

            async def evaluate_cycle(
                current: dict[str, Any], checkpoint: dict[str, Any]
            ) -> dict[str, Any]:
                report = checkpoint.get("verification", {})
                if report.get("goal_outcome") == "achieved":
                    return {
                        "decision": "achieved",
                        "goal_outcome": "achieved",
                        "evidence_grade": "trusted_readback",
                    }
                return {
                    "decision": report.get("cycle_decision", "continue"),
                    "goal_outcome": report.get("goal_outcome", "inconclusive"),
                    "sleep_until": report.get("sleep_until"),
                    "evidence_grade": "trusted_readback",
                }

            use_facts = bool(self.components.resolve("octop")["facts_enabled"])

            runtime = LongTermRuntime(
                self._execute_node,
                evidence_writer=evidence_writer,
                control_checker=control_checker,
                observer=fact_hook if use_facts else None,
                verifier=fact_hook if use_facts else None,
                evaluator=evaluate_cycle if use_facts else None,
            )
            outcome = await runtime.advance(task, task.get("checkpoint") or None)
            # Action events were durably written before dispatch/after return.
            events = [
                event
                for event in outcome.events
                if event.get("kind")
                not in {"action_intent", "action_completed", "action_not_dispatched"}
            ]
            self.repo.checkpoint(
                task,
                owner,
                outcome.checkpoint,
                status=outcome.status,
                result=outcome.result,
                events=events,
            )
            committed = self.repo.task(tenant_id, task_id)
            return committed["status"] if committed else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.repo.checkpoint(
                task,
                owner,
                task["checkpoint"],
                status="failed",
                result={"reason": "runtime_unavailable", "error_type": type(exc).__name__},
                events=[],
            )
            return "failed"
        finally:
            keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive
            self.repo.release(tenant_id, task_id, owner)

    async def _execute_node(self, node: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        """Dispatch registered component operations or tool-free agent analysis."""
        if node.get("skill") == "component.call":
            from octop.infra.longterm.component_calls import execute_component_call

            return await execute_component_call(self, node, task)
        if node["risk"] != "R0":
            raise _invalid("effect_adapter_not_registered")
        agent_id = str(node.get("agent_id") or "")
        row = self.services.agent_repo.get(agent_id)
        if row is None or row.user_id != task["creator_user_id"] or not row.enabled:
            raise OctopError(ErrorCode.FORBIDDEN, "agent not available")
        if self.repo.role(task["tenant_id"], task["creator_user_id"]) in {None, "viewer"}:
            raise OctopError(ErrorCode.FORBIDDEN, "tenant authority revoked")
        behavior = node.get("behavior_snapshot") or {}
        model_ref = behavior.get("model_ref") or ""
        provider_name, separator, model_id = model_ref.partition("/")
        if not separator:
            raise _invalid("agent_requires_explicit_model")
        provider = self.services.provider_repo.get_by_name(provider_name)
        if provider is None or not provider.enabled:
            raise _invalid("model_provider_not_available")
        if behavior.get("provider_digest") != digest(
            {"kind": provider.kind, "base_url": provider.base_url}
        ):
            raise _invalid("model_provider_revision_changed")
        from octop.infra.agents.providers.probe import build_probe_chat_model

        skill_text = behavior.get("skill_text", "")
        skill = str(node.get("skill") or "agent.analyze")
        if digest(skill_text) != behavior.get("skill_digest"):
            raise _invalid("frozen_skill_revision_changed")
        messages = [
            {
                "role": "system",
                "content": behavior.get("system_prompt", "")
                + "\nProduce analysis only. No tools are available. Do not claim an external action was executed.\n"
                + skill_text,
            },
            {
                "role": "user",
                "content": canonical(
                    {
                        "goal": task["goal"],
                        "input": node.get("input", {}),
                        "dependency_results": task.get("execution", {}).get(
                            "dependency_results", {}
                        ),
                        "observation": task.get("execution", {}).get("observation", {}),
                        "diagnosis": task.get("execution", {}).get("diagnosis", {}),
                    }
                ),
            },
        ]
        if len(canonical(messages)) > 64000:
            raise _invalid("model_context_budget_exhausted")
        model = build_probe_chat_model(provider, model_id=model_id)
        response = await model.ainvoke(messages, max_tokens=node.get("max_output_tokens", 2048))
        result = {
            "output": response.content,
            "model_ref": model_ref,
            "agent_id": agent_id,
            "skill": skill,
            "effect": "analysis",
            "usage": getattr(response, "usage_metadata", None),
        }
        validate_public_data(result)
        return result

    async def _dispatch_temporal(self, task: dict[str, Any]) -> None:
        from octop.infra.longterm.temporal_runtime import connect, start_task, wake_task

        try:
            self.components.require_enabled("temporal")
            temporal_config = self.components.resolve("temporal")
            address = temporal_config["address"]
            if not address:
                raise RuntimeError("temporal_not_configured")
            async with asyncio.timeout(15):
                client = await connect(
                    address=address,
                    namespace=temporal_config["namespace"],
                    tls=temporal_config["tls"],
                    api_key=temporal_config["api_key"] or None,
                )
                if task["status"] != "queued":
                    await wake_task(client, tenant_id=task["tenant_id"], task_id=task["task_id"])
                    return
                try:
                    workflow_id = await start_task(
                        client,
                        tenant_id=task["tenant_id"],
                        task_id=task["task_id"],
                        task_queue=temporal_config["task_queue"],
                    )
                    self.repo.append(
                        task["tenant_id"],
                        task["task_id"],
                        "temporal.started",
                        {"workflow_id": workflow_id},
                    )
                except Exception as exc:
                    if type(exc).__name__ != "WorkflowAlreadyStartedError":
                        raise
                    await wake_task(client, tenant_id=task["tenant_id"], task_id=task["task_id"])
        except Exception as exc:
            owner = uuid4().hex
            claimed = self.repo.claim(task["tenant_id"], task["task_id"], owner)
            if claimed:
                self.repo.checkpoint(
                    claimed,
                    owner,
                    claimed["checkpoint"],
                    status="failed",
                    result={"reason": "temporal_unavailable", "error_type": type(exc).__name__},
                    events=[],
                )

    def _record_artifact(
        self,
        task: dict[str, Any],
        user: User,
        group: str,
        value: dict[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        validate_public_data(value)
        task.setdefault("artifacts", {}).setdefault(group, []).append(value)
        if len(canonical(task).encode()) > 2097152:
            raise _invalid("artifact_storage_limit")
        try:
            response = self.repo.command(
                task,
                expected_revision=expected_revision,
                command_key=f"{user.id}:artifact:{uuid4().hex}",
                request_digest=digest(value),
                command=group,
                audit_payload=value,
                actor_user_id=user.id,
                require_admin=group == "evolution",
            )
        except AuthorityRevoked as exc:
            raise self._revoked(exc) from exc
        return {"task": response, "record": value}

    def evaluate(
        self,
        tenant_id: str,
        task_id: str,
        user: User,
        *,
        expected_revision: int,
        run_dir: str,
        benchmark_task_id: str | None = None,
    ) -> dict[str, Any]:
        from octop.infra.longterm.adapters import tenant_namespace
        from octop.infra.longterm.evaluation import CommerceBenchAdapter

        self.authorize(tenant_id, user, write=True)
        task = self.task(tenant_id, task_id, user)
        self.components.require_enabled("commerceagentbench")
        root = self.components.directory("commerceagentbench", "results_subdirectory")
        if not root.is_dir():
            raise OctopError(ErrorCode.LONGTERM_UNAVAILABLE, "benchmark result root not configured")
        # Operators put trusted harness outputs in each tenant's private space directory.
        result_root = root / tenant_namespace(tenant_id) / task["space_id"]
        try:
            report = CommerceBenchAdapter(result_root).load_run(
                run_dir, expected_task_id=benchmark_task_id
            )
        except (OSError, ValueError) as exc:
            raise _invalid("benchmark_bundle_invalid") from exc
        report["platform_task_id"] = task_id
        report["platform_plan_digest"] = task["plan_digest"]
        # A benchmark run is an evaluation of its recorded benchmark task, not proof
        # that an unrelated platform goal or observation window was satisfied.
        report["scope"] = "benchmark_run"
        return self._record_artifact(task, user, "evaluation", report, expected_revision)

    async def memory(
        self, tenant_id: str, task_id: str, user: User, data: dict[str, Any]
    ) -> dict[str, Any]:
        from octop.infra.longterm.adapters import HindsightAdapter, IntegrationUnavailable

        mutating = data["action"] in {"retain", "delete"}
        self.authorize(tenant_id, user, write=mutating)
        task = self.task(tenant_id, task_id, user)
        self.components.require_enabled("hindsight")
        memory_config = self.components.resolve("hindsight")
        address = memory_config["base_url"]
        if not address:
            raise OctopError(ErrorCode.LONGTERM_UNAVAILABLE, "memory not configured")
        validate_public_data(data)
        retaining = data["action"] == "retain"
        deleting = data["action"] == "delete"
        private_scope = tenant_id + ":space:" + task["space_id"]
        refs: list[str] = []
        if not mutating and not data["query"].strip():
            raise _invalid("memory_query_required")
        if mutating:
            self.authorize(tenant_id, user, admin=True)
            if not data["consent"]:
                raise _invalid("memory_consent_required")
        if deleting:
            document_id = data.get("document_id", "")
            if not re.fullmatch(r"octop-[a-f0-9]{64}", document_id):
                raise _invalid("memory_document_invalid")
            cursor, registered = 0, False
            while True:
                page = self.repo.events(tenant_id, task_id, cursor, 100)
                for event in page:
                    payload = event["payload"]
                    if (
                        event["event_type"] == "memory.retain"
                        and payload.get("document_id") == document_id
                        and payload.get("bank_id") == HindsightAdapter.bank_id(private_scope)
                    ):
                        registered = True
                if len(page) < 100:
                    break
                cursor = page[-1]["sequence"]
            if not registered:
                raise OctopError(ErrorCode.FORBIDDEN, "memory document not retained by this task")
            if not self.evidence(tenant_id, task_id, user)["valid"]:
                raise _invalid("invalid_evidence_chain")
        if retaining:
            # A benchmark or successful model response cannot manufacture success.
            verified = (
                task.get("result", {}).get("goal_outcome") == "achieved"
                if task.get("result")
                else False
            )
            refs = task.get("checkpoint", {}).get("verification", {}).get("evidence_refs", [])
            if not verified or not refs:
                raise _invalid("verified_readback_required_for_memory")
            if not self.evidence(tenant_id, task_id, user)["valid"]:
                raise _invalid("invalid_evidence_chain")
        operation_id = uuid4().hex
        dispatched = False

        def before_request() -> None:
            nonlocal dispatched
            self._assert_task_authority(
                tenant_id, task_id, user, require_admin=mutating, write=mutating
            )
            dispatched = True

        adapter = HindsightAdapter(
            address,
            api_key=memory_config["api_key"] or None,
            authorization_check=before_request,
        )
        try:
            try:
                self.repo.append(
                    tenant_id,
                    task_id,
                    "memory." + data["action"] + ".intent",
                    {
                        "operation_id": operation_id,
                        "actor_user_id": user.id,
                        "plan_digest": task["plan_digest"],
                        "request_digest": digest(data),
                        "evidence_refs": refs,
                        "consent": bool(data.get("consent")),
                    },
                    actor_user_id=user.id,
                    require_admin=mutating,
                    write=mutating,
                )
            except AuthorityRevoked as exc:
                raise self._revoked(exc) from exc
            try:
                if retaining:
                    result = await adapter.retain(
                        tenant_id=private_scope,
                        task_id=task_id,
                        content=data["content"],
                        evidence_refs=refs,
                        verified=True,
                        ttl_seconds=data["ttl_seconds"],
                        consent=True,
                    )
                elif deleting:
                    result = await adapter.delete(
                        tenant_id=private_scope, document_id=document_id, consent=True
                    )
                else:
                    result = await adapter.recall(
                        tenant_id=private_scope, query=data["query"], max_tokens=data["max_tokens"]
                    )
                validate_public_data(result)
                # Record the external fact even if authority was revoked in flight.
                self.repo.append(
                    tenant_id,
                    task_id,
                    "memory." + data["action"],
                    {**result, "operation_id": operation_id, "actor_user_id": user.id},
                )
            except (Exception, asyncio.CancelledError) as exc:
                self.repo.append(
                    tenant_id,
                    task_id,
                    "memory."
                    + data["action"]
                    + (".uncertain" if mutating and dispatched else ".failed"),
                    {
                        "operation_id": operation_id,
                        "actor_user_id": user.id,
                        "dispatched": dispatched,
                        "error_type": type(exc).__name__,
                    },
                )
                raise
        except (IntegrationUnavailable, ImportError) as exc:
            raise OctopError(
                ErrorCode.LONGTERM_UNAVAILABLE,
                "memory unavailable",
                details={"error_type": type(exc).__name__},
            ) from exc
        finally:
            await adapter.close()
        self._assert_task_authority(
            tenant_id, task_id, user, require_admin=mutating, write=mutating
        )
        return result

    async def provenance(
        self, tenant_id: str, task_id: str, user: User, *, consent: bool
    ) -> dict[str, Any]:
        from octop.infra.longterm.adapters import IntegrationUnavailable, SemanticaAdapter

        self.authorize(tenant_id, user, write=True)
        self.task(tenant_id, task_id, user)
        if not consent:
            raise _invalid("provenance_export_consent_required")
        evidence = self.evidence(tenant_id, task_id, user)
        if not evidence["valid"]:
            raise _invalid("invalid_evidence_chain")
        frozen_sequence = evidence["last_sequence"]
        frozen_head = evidence["head_digest"]
        operation_id = uuid4().hex
        try:
            self.repo.append(
                tenant_id,
                task_id,
                "provenance.intent",
                {
                    "operation_id": operation_id,
                    "actor_user_id": user.id,
                    "head_digest": frozen_head,
                    "count": frozen_sequence,
                    "consent": True,
                },
                actor_user_id=user.id,
            )
        except AuthorityRevoked as exc:
            raise self._revoked(exc) from exc
        self.components.require_enabled("semantica")
        adapter = SemanticaAdapter(
            self.components.directory("semantica", "storage_subdirectory"),
            authorization_check=lambda: self._assert_task_authority(tenant_id, task_id, user),
        )
        indexed = []
        cursor, previous = 0, "0" * 64
        try:
            while cursor < frozen_sequence:
                page = self.repo.events(tenant_id, task_id, cursor)
                page = [event for event in page if event["sequence"] <= frozen_sequence]
                if not page:
                    raise _invalid("frozen_evidence_cut_unavailable")
                for event in page:
                    raw = {key: value for key, value in event.items() if key != "digest"}
                    if (
                        event["sequence"] != cursor + 1
                        or event["previous_digest"] != previous
                        or digest(raw) != event["digest"]
                    ):
                        raise _invalid("invalid_evidence_chain")
                    self._assert_task_authority(tenant_id, task_id, user)
                    indexed.append(
                        await adapter.record(
                            event,
                            evidence_ref=f"octop-evidence:{tenant_id}:{task_id}:{event['sequence']}:{event['digest']}",
                        )
                    )
                    cursor, previous = event["sequence"], event["digest"]
            if previous != frozen_head:
                raise _invalid("frozen_evidence_head_changed")
        except (Exception, asyncio.CancelledError) as exc:
            self.repo.append(
                tenant_id,
                task_id,
                "provenance.uncertain",
                {
                    "operation_id": operation_id,
                    "actor_user_id": user.id,
                    "confirmed_count": len(indexed),
                    "head_digest": frozen_head,
                    "error_type": type(exc).__name__,
                },
            )
            if isinstance(exc, (IntegrationUnavailable, ImportError)):
                raise OctopError(
                    ErrorCode.LONGTERM_UNAVAILABLE, "provenance integration unavailable"
                ) from exc
            raise
        result = {
            "source": "semantica",
            "records": indexed,
            "count": len(indexed),
            "head_digest": frozen_head,
            "complete": len(indexed) == frozen_sequence,
        }
        self.repo.append(
            tenant_id,
            task_id,
            "provenance.exported",
            {
                "operation_id": operation_id,
                "actor_user_id": user.id,
                "count": len(indexed),
                "head_digest": frozen_head,
            },
        )
        self._assert_task_authority(tenant_id, task_id, user)
        return result

    async def evolve(
        self, tenant_id: str, task_id: str, user: User, data: dict[str, Any]
    ) -> dict[str, Any]:
        from octop.infra.longterm.adapters import tenant_namespace
        from octop.infra.longterm.evolution import EvolutionService, RnaSdkAdapter

        self.authorize(tenant_id, user, write=True, admin=True)
        task = self.task(tenant_id, task_id, user)
        if task["revision"] != data["expected_revision"]:
            raise RevisionConflict("task revision conflict")
        validate_public_data(data)
        evidence = self.evidence(tenant_id, task_id, user)
        if not evidence["valid"]:
            raise _invalid("invalid_evidence_chain")
        references = [f"octop-evidence:{task_id}:{evidence['head_digest']}"]
        controller = EvolutionService()
        action = data["action"]
        try:
            if action in {"propose", "rna_propose"}:
                proposal = data.get("proposal", {})
                baseline_revision = task.get("active_behavior_revision", task["plan_digest"])
                generation = None
                if action == "rna_propose":
                    self.components.require_enabled("rna")
                    rna_config = self.components.resolve("rna")
                    sdk_root = os.getenv("OCTOP_RNA_SDK_ROOT") or str(
                        Path(__file__).resolve().parents[5] / "rna_dev/packages/sdk"
                    )
                    model_json = rna_config["model_json"]
                    if not model_json:
                        raise OctopError(
                            ErrorCode.LONGTERM_UNAVAILABLE, "RNA SDK model not configured"
                        )
                    model = json.loads(model_json)
                    adapter = RnaSdkAdapter(
                        Path(sdk_root), node_binary=os.getenv("OCTOP_NODE_BINARY", "node")
                    )
                    baseline = proposal.get("baseline", {})
                    proposed = await adapter.propose(
                        project={"id": tenant_namespace(tenant_id) + "-" + task["space_id"]},
                        baseline=baseline,
                        evidence=[
                            {
                                "task_id": task_id,
                                "plan_digest": task["plan_digest"],
                                "baseline_revision": baseline_revision,
                                "result": task.get("result"),
                                "evidence_refs": references,
                            }
                        ],
                        model=model,
                        api_key_env="RNA_ADAPTER_API_KEY",
                        api_key=rna_config["api_key"],
                        authorization_check=lambda: self._assert_task_authority(
                            tenant_id, task_id, user, require_admin=True
                        ),
                    )
                    self._assert_task_authority(tenant_id, task_id, user, require_admin=True)
                    if proposed.get("status") != "proposed":
                        return proposed
                    if proposed.get("baseline_digest") != digest(baseline):
                        raise _invalid("rna_baseline_digest_mismatch")
                    generation = {
                        "source": "rna_dev",
                        "baseline": copy.deepcopy(baseline),
                        "baseline_digest": proposed["baseline_digest"],
                        "baseline_revision": baseline_revision,
                        "sdkVersion": proposed.get("sdkVersion"),
                        "usageReported": proposed.get("usageReported"),
                        "usage": copy.deepcopy(proposed.get("usage")),
                    }
                    # This reference is part of the controller's frozen candidate
                    # digest, so generation cost/version evidence cannot drift.
                    references.append("octop-rna-generation:" + digest(generation))
                    proposal = proposed.get("proposal", proposed)
                candidate = controller.propose(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    baseline_revision=baseline_revision,
                    benchmark_revision=data["benchmark_revision"],
                    evidence_refs=references,
                    proposal=proposal,
                )
                if generation is not None:
                    candidate["generation"] = generation
            else:
                snapshots = task.get("artifacts", {}).get("evolution", [])
                found_candidate = next(
                    (
                        value
                        for value in reversed(snapshots)
                        if value.get("id") == data.get("candidate_id")
                    ),
                    None,
                )
                if found_candidate is None:
                    raise _invalid("evolution_candidate_not_found")
                candidate = found_candidate
                generation = candidate.get("generation")
                generation_refs = [
                    ref
                    for ref in candidate["evidence_refs"]
                    if ref.startswith("octop-rna-generation:")
                ]
                if (generation is not None or generation_refs) and (
                    not isinstance(generation, dict)
                    or generation_refs != ["octop-rna-generation:" + digest(generation)]
                ):
                    raise _invalid("rna_generation_audit_changed")
                self.components.require_enabled("commerceagentbench")
                root = self.components.directory(
                    "commerceagentbench", "evolution_reports_subdirectory"
                )
                if not root.is_dir():
                    raise OctopError(
                        ErrorCode.LONGTERM_UNAVAILABLE,
                        "trusted evolution evaluation not configured",
                    )

                def evaluate_report() -> dict[str, Any]:
                    source_root = (
                        Path(root) / tenant_namespace(tenant_id) / task["space_id"]
                    ).resolve()
                    report_path = (source_root / (candidate["id"] + ".json")).resolve()
                    if (
                        not report_path.is_relative_to(source_root)
                        or not report_path.is_file()
                        or report_path.stat().st_size > 524288
                    ):
                        raise ValueError("trusted evolution report unavailable")
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    if not isinstance(report, dict) or any(
                        report.get(key) != candidate[key]
                        for key in ("baseline_revision", "benchmark_revision", "candidate_digest")
                    ):
                        raise ValueError("evolution report does not match frozen candidate")
                    if action == "assess":
                        return controller.assess(candidate, report)
                    return controller.transition(
                        candidate,
                        action,
                        active_baseline_revision=task.get(
                            "active_behavior_revision", task["plan_digest"]
                        ),
                        actor_is_admin=True,
                        evidence_refs=references,
                        canary_readiness=report.get("canary_readiness"),
                    )

                candidate = await asyncio.get_running_loop().run_in_executor(None, evaluate_report)
                if action != "assess":
                    if action == "promoted":
                        task["active_behavior_revision"] = candidate["candidate_digest"]
                    elif action == "rolled_back":
                        task["active_behavior_revision"] = candidate["baseline_revision"]
            return self._record_artifact(
                task, user, "evolution", candidate, data["expected_revision"]
            )
        except (ValueError, OSError) as exc:
            raise _invalid("evolution_input_or_trusted_report_invalid") from exc

    def integrations(self) -> list[dict[str, Any]]:
        return [dict(component) for component in self.components.catalog()["components"]]
