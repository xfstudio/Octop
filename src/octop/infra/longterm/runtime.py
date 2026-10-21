"""One bounded, resumable BM cycle shared by offline and durable execution."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from .execution import (
    DECISIONS,
    STAGES,
    WorkflowValidationError,
    authorized,
    canonical_json,
    compile_workflow,
    execution_key,
    parse_utc,
    ready_batch,
    token_reservation,
)
from .input_bindings import InputBindingError, resolve_input_bindings

JsonObject = dict[str, Any]
NodeExecutor = Callable[[JsonObject, JsonObject], Awaitable[JsonObject]]
StageHook = Callable[[JsonObject, JsonObject], Awaitable[JsonObject]]
EvidenceWriter = Callable[[JsonObject], Awaitable[Any]]


class DispatchBlocked(RuntimeError):
    def __init__(self, status: str, *, error_type: str | None = None) -> None:
        self.status = status
        self.error_type = error_type
        super().__init__(status)


@dataclass
class RuntimeResult:
    status: str
    checkpoint: JsonObject
    result: JsonObject | None = None
    events: list[JsonObject] = field(default_factory=list)

    def to_dict(self) -> JsonObject:
        return asdict(self)


class LongTermRuntime:
    """Advance exactly one stage or one Act batch, leaving persistence to the service.

    Executors receive a stable ``task.execution.idempotency_key``. They must use it
    at each side-effect boundary. Write attempts are not automatically retried.
    Hooks are optional integrations; their absence never proves goal achievement.
    """

    def __init__(
        self,
        node_executor: NodeExecutor | None = None,
        *,
        observer: StageHook | None = None,
        verifier: StageHook | None = None,
        evaluator: StageHook | None = None,
        evidence_writer: EvidenceWriter | None = None,
        control_checker: StageHook | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.node_executor = node_executor
        self.observer = observer
        self.verifier = verifier
        self.evaluator = evaluator
        self.evidence_writer = evidence_writer
        self.control_checker = control_checker
        self.clock = clock or (lambda: datetime.now(UTC))

    async def advance(
        self, task: JsonObject, checkpoint: JsonObject | None = None
    ) -> RuntimeResult:
        plan = compile_workflow(task)
        now = self.clock()
        state: JsonObject = (
            copy.deepcopy(checkpoint)
            if checkpoint
            else {
                "schema_version": 1,
                "tenant_id": task["tenant_id"],
                "task_id": task["task_id"],
                "plan_digest": plan["digest"],
                "phase": "Observe",
                "cycle": 1,
                "actions": 0,
                "node_results": {},
                "started_at": now.isoformat(),
                "execution_health": "pending",
                "goal_outcome": "inconclusive",
                "decision": "continue",
                "token_usage": {
                    "total_tokens": 0,
                    "known_total_tokens": 0,
                    "unknown_actions": 0,
                    "budget_consumed": 0,
                },
            }
        )
        if state.get("schema_version") != 1 or any(
            state.get(key) != task[key] for key in ("tenant_id", "task_id")
        ):
            raise WorkflowValidationError("checkpoint identity/schema mismatch")
        if state.get("plan_digest") != plan["digest"]:
            raise WorkflowValidationError("checkpoint plan digest mismatch")
        if "token_usage" not in state:
            # Older checkpoints cannot reconstruct missing usage as zero.
            state["token_usage"] = {
                "total_tokens": None if state["actions"] else 0,
                "known_total_tokens": 0,
                "unknown_actions": state["actions"],
                "budget_consumed": plan["max_tokens"] if state["actions"] else 0,
            }
        if state.get("terminal_status"):
            return self._result(
                str(state["terminal_status"]), state, **state.get("terminal_details", {})
            )
        control = task.get("control", {})
        if control.get("cancelled"):
            state.update(decision="stopped", execution_health="cancelled")
            return self._terminal("cancelled", state)
        if control.get("paused"):
            return self._result("paused", state)
        if state.get("sleep_until"):
            if parse_utc(state["sleep_until"]) > now:
                return self._result("sleeping", state)
            state.pop("sleep_until")
        if (now - parse_utc(state["started_at"])).total_seconds() >= plan["timeout_seconds"]:
            state.update(decision="stopped", execution_health="budget_exhausted")
            return self._terminal("failed", state, reason="wall_time_budget_exhausted")
        if (
            state.get("phase") == "Evaluate"
            and state.get("evaluation", {}).get("decision") == "human_required"
        ):
            # A service resume acknowledges the pause; refresh facts in a new
            # bounded cycle instead of evaluating the same stale readback again.
            if state["cycle"] >= plan["max_cycles"]:
                state.update(decision="stopped", goal_outcome="inconclusive")
                return self._terminal("completed", state, reason="cycle_budget_exhausted")
            state.update(
                phase="Observe", cycle=state["cycle"] + 1, node_results={}, decision="continue"
            )
            state.pop("verification", None)
            state.pop("evaluation", None)
        phase = state.get("phase")
        if phase not in STAGES:
            raise WorkflowValidationError("unknown checkpoint phase")
        event = self._event(task, state, "stage_completed")
        try:
            if phase == "Observe":
                state["observation"] = (
                    await self._hook(self.observer, task, state, plan)
                    if self.observer
                    else {"source": "task_input", "goal": task["goal"], "evidence_refs": []}
                )
            elif phase == "Diagnose":
                state["diagnosis"] = {
                    "constraints": task.get("constraints", {}),
                    "acceptance": task.get("acceptance", {}),
                    "missing_observer": self.observer is None,
                    "missing_verifier": self.verifier is None,
                    "observation_quality": state["observation"].get("quality", "unrated"),
                    "evidence_refs": state["observation"].get("evidence_refs", []),
                }
            elif phase == "Plan":
                state["plan"] = plan
            elif phase == "Compile":
                disabled = [node["id"] for node in plan["nodes"] if node["risk"] == "R3"]
                if disabled:
                    return self._terminal("failed", state, reason="R3_disabled", nodes=disabled)
            elif phase == "Act":
                return await self._act(task, state, plan)
            elif phase == "Verify":
                state["verification"] = (
                    await self._hook(self.verifier, task, state, plan)
                    if self.verifier
                    else {
                        "goal_outcome": "inconclusive",
                        "evidence_refs": [],
                        "reason": "independent_readback_not_configured",
                    }
                )
                # A successful tool return is execution telemetry, not acceptance evidence.
                state["execution_health"] = "succeeded"
            elif phase == "Evaluate":
                return await self._evaluate(task, state, plan)
            state["phase"] = STAGES[STAGES.index(phase) + 1]
            canonical_json(state)
            return self._result("running", state, events=[event])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            uncertain = bool(state.get("inflight_writes"))
            state.update(
                execution_health="uncertain" if uncertain else "failed", decision="stopped"
            )
            # Do not persist exception strings that could contain credentials/provider payloads.
            return self._terminal(
                "uncertain" if uncertain else "failed",
                state,
                reason="stage_failed",
                error_type=type(exc).__name__,
            )

    async def _act(self, task: JsonObject, state: JsonObject, plan: JsonObject) -> RuntimeResult:
        batch = ready_batch(plan, state["node_results"])
        if not batch:
            state["phase"] = "Verify"
            return self._result("running", state)
        if state["actions"] + len(batch) > plan["max_actions"]:
            state.update(execution_health="budget_exhausted", decision="stopped")
            return self._terminal("failed", state, reason="action_budget_exhausted")
        blocked = [
            node["id"]
            for offset, node in enumerate(batch)
            if not authorized(
                node, task, {**state, "actions": state["actions"] + offset}, now=self.clock()
            )
        ]
        if blocked:
            state.update(waiting_nodes=blocked, decision="human_required")
            return self._result(
                "waiting_approval",
                state,
                events=[self._event(task, state, "approval_required", node_ids=blocked)],
            )
        state.pop("waiting_nodes", None)
        state["decision"] = "continue"
        if any(node["risk"] != "R0" for node in batch) and self.evidence_writer is None:
            return self._terminal("failed", state, reason="durable_evidence_writer_required")
        # Resolve only completed dependencies, keeping the frozen plan unchanged.
        # Do this before any intent or dispatch, including token reservations.
        try:
            dispatch_nodes = {
                node["id"]: {
                    **node,
                    "input": resolve_input_bindings(
                        node.get("input", {}),
                        {key: state["node_results"][key] for key in node["depends_on"]},
                    ),
                }
                for node in batch
            }
        except InputBindingError:
            state.update(execution_health="failed", decision="stopped")
            return self._terminal("failed", state, reason="node_input_reference_invalid")
        reservations = {
            node["id"]: token_reservation(
                dispatch_nodes[node["id"]], task, state["node_results"], state
            )
            for node in batch
        }
        if (
            state["token_usage"]["budget_consumed"] + sum(reservations.values())
            > plan["max_tokens"]
        ):
            return self._terminal("failed", state, reason="token_budget_exhausted")
        intents: list[JsonObject] = []
        trace: dict[str, JsonObject] = {}
        for node in batch:
            operation = {"skill": node.get("skill", "unspecified")}
            if node.get("skill") == "component.call":
                operation.update(component_id=node["component_id"], operation=node["operation"])
            trace[node["id"]] = {
                "operation": operation,
                "input_digest": hashlib.sha256(
                    canonical_json(dispatch_nodes[node["id"]]["input"]).encode("utf-8")
                ).hexdigest(),
            }
            intent = self._event(
                task,
                state,
                "action_intent",
                node_id=node["id"],
                risk=node["risk"],
                authorized=True,
                token_reserved=reservations[node["id"]],
                idempotency_key=execution_key(task, plan["digest"], state["cycle"], node["id"]),
                **trace[node["id"]],
            )
            # Persist *all* intents before concurrent execution. A sink failure dispatches nothing.
            if self.evidence_writer:
                await self.evidence_writer(intent)
            intents.append(intent)

        async def execute(node: JsonObject) -> JsonObject:
            scoped_task = copy.deepcopy(task)
            scoped_task["execution"] = {
                "idempotency_key": execution_key(task, plan["digest"], state["cycle"], node["id"]),
                "cycle": state["cycle"],
                "plan_digest": plan["digest"],
                "dependency_results": {
                    key: state["node_results"][key] for key in node["depends_on"]
                },
                "observation": copy.deepcopy(state.get("observation", {})),
                "diagnosis": copy.deepcopy(state.get("diagnosis", {})),
            }
            try:
                if self.control_checker:
                    latest = await self.control_checker(task, copy.deepcopy(state))
                    if not isinstance(latest, dict):
                        raise DispatchBlocked("paused")
                    if latest.get("cancelled"):
                        raise DispatchBlocked("cancelled")
                    if latest.get("paused"):
                        raise DispatchBlocked("paused")
                    scoped_task["control"] = latest
                    if not authorized(node, scoped_task, state, now=self.clock()):
                        raise DispatchBlocked("waiting_approval")
                remaining = (
                    plan["timeout_seconds"]
                    - (self.clock() - parse_utc(state["started_at"])).total_seconds()
                )
                if remaining <= 0:
                    raise DispatchBlocked("failed")
            except DispatchBlocked:
                raise
            except Exception as exc:
                raise DispatchBlocked("failed", error_type=type(exc).__name__) from exc
            trace[node["id"]]["started_at"] = self.clock().isoformat()
            started = perf_counter()
            try:
                async with asyncio.timeout(min(node["timeout_seconds"], remaining)):
                    node = dispatch_nodes[node["id"]]
                    if node.get("skill") == "local.echo":
                        return {
                            "output": copy.deepcopy(node.get("input", {})),
                            "source": "local.echo",
                            "usage": {"total_tokens": 0},
                        }
                    if self.node_executor is None:
                        raise DispatchBlocked("failed")
                    result = await self.node_executor(node, scoped_task)
                    if not isinstance(result, dict):
                        raise TypeError("executor result must be a JSON object")
                    canonical_json(result)
                    return result
            finally:
                trace[node["id"]].update(
                    finished_at=self.clock().isoformat(),
                    duration_ms=round((perf_counter() - started) * 1000, 3),
                )

        state["inflight_writes"] = [node["id"] for node in batch if node["risk"] != "R0"]
        outcomes = await asyncio.gather(*(execute(node) for node in batch), return_exceptions=True)
        events = intents.copy()
        failed = False
        uncertain = False
        deferred: list[str] = []
        for node, outcome in zip(batch, outcomes, strict=True):
            if isinstance(outcome, DispatchBlocked):
                deferred.append(outcome.status)
                event = self._event(
                    task,
                    state,
                    "action_not_dispatched",
                    node_id=node["id"],
                    reason=outcome.status,
                    **({"error_type": outcome.error_type} if outcome.error_type else {}),
                )
                if self.evidence_writer:
                    await self.evidence_writer(event)
                if node["id"] in state["inflight_writes"]:
                    state["inflight_writes"].remove(node["id"])
                events.append(event)
                continue
            state["actions"] += 1
            if isinstance(outcome, BaseException):
                failed = True
                uncertain = uncertain or node["risk"] != "R0"
                value: JsonObject = {
                    "status": "uncertain" if node["risk"] != "R0" else "failed",
                    "error_type": type(outcome).__name__,
                }
            else:
                value = {"status": "succeeded", "result": outcome}
            raw_usage = outcome.get("usage") if isinstance(outcome, dict) else None
            measured = raw_usage.get("total_tokens") if isinstance(raw_usage, dict) else None
            if isinstance(measured, bool) or not isinstance(measured, int) or measured < 0:
                measured = None
            usage = state["token_usage"]
            if measured is None:
                usage["unknown_actions"] += 1
                usage["total_tokens"] = None
                charge = reservations[node["id"]]
            else:
                usage["known_total_tokens"] += measured
                if usage["total_tokens"] is not None:
                    usage["total_tokens"] += measured
                charge = measured
            usage["budget_consumed"] += charge
            state["node_results"][node["id"]] = value
            finished_at = trace[node["id"]]["finished_at"]
            state["last_action_completed_at"] = max(
                finished_at, state.get("last_action_completed_at", finished_at)
            )
            completed = self._event(
                task,
                state,
                "action_completed",
                node_id=node["id"],
                idempotency_key=execution_key(task, plan["digest"], state["cycle"], node["id"]),
                token_reserved=reservations[node["id"]],
                token_measured=measured,
                token_charge=charge,
                completed_at=finished_at,
                **trace[node["id"]],
                **value,
            )
            if self.evidence_writer:
                await self.evidence_writer(completed)
            if node["id"] in state["inflight_writes"]:
                state["inflight_writes"].remove(node["id"])
            events.append(completed)
        if failed:
            # Unknown write outcomes require reconciliation, not an automatic retry.
            state.update(execution_health="failed", decision="stopped")
            return self._terminal(
                "uncertain" if uncertain else "failed",
                state,
                events=events,
                reason="write_outcome_requires_reconciliation"
                if uncertain
                else "node_execution_failed",
            )
        if deferred:
            status = next(
                (
                    item
                    for item in ("cancelled", "failed", "paused", "waiting_approval")
                    if item in deferred
                ),
                "paused",
            )
            if status in {"cancelled", "failed"}:
                return self._terminal(status, state, events=events, reason="dispatch_blocked")
            state["decision"] = "human_required"
            return self._result(status, state, events=events)
        if state["token_usage"]["budget_consumed"] > plan["max_tokens"]:
            return self._terminal(
                "failed", state, events=events, reason="reported_usage_exceeded_budget"
            )
        if len(state["node_results"]) == len(plan["nodes"]):
            state["phase"] = "Verify"
        return self._result("running", state, events=events)

    async def _evaluate(
        self, task: JsonObject, state: JsonObject, plan: JsonObject
    ) -> RuntimeResult:
        evaluation = (
            await self._hook(self.evaluator, task, state, plan)
            if self.evaluator
            else {
                "decision": "stopped",
                "goal_outcome": "inconclusive",
                "reason": "goal_evaluator_not_configured",
            }
        )
        decision = evaluation.get("decision", "stopped")
        if decision not in DECISIONS:
            raise WorkflowValidationError("unknown evaluation decision")
        verification = state.get("verification", {})
        if decision == "achieved" and not (
            verification.get("goal_outcome") == "achieved"
            and isinstance(verification.get("evidence_refs"), list)
            and verification["evidence_refs"]
            and state.get("observation", {}).get("evidence_refs")
        ):
            evaluation = {
                **evaluation,
                "decision": "stopped",
                "goal_outcome": "inconclusive",
                "reason": "independent_evidence_required",
            }
            decision = "stopped"
        state["evaluation"] = evaluation
        state["decision"] = decision
        state["goal_outcome"] = (
            "achieved" if decision == "achieved" else evaluation.get("goal_outcome", "inconclusive")
        )
        if state["goal_outcome"] == "achieved" and decision != "achieved":
            state["goal_outcome"] = "inconclusive"
        if decision == "human_required":
            # An ordinary node approval cannot acknowledge an evaluator's open question.
            return self._result("paused", state)
        if decision == "sleep_until":
            wake_at = evaluation.get("sleep_until")
            if not isinstance(wake_at, str) or parse_utc(wake_at) <= self.clock():
                raise WorkflowValidationError("sleep_until must be a future timestamp")
            if state["cycle"] >= plan["max_cycles"]:
                state.update(decision="stopped", goal_outcome="inconclusive")
                return self._terminal("completed", state, reason="cycle_budget_exhausted")
            state["sleep_until"] = wake_at
            state["cycle"] += 1
            state["node_results"] = {}
            state["phase"] = "Observe"
            return self._result("sleeping", state)
        if decision == "replan":
            # Changing an approved plan needs a new task revision and authorization.
            return self._result("paused", state)
        if decision == "continue" and state["cycle"] < plan["max_cycles"]:
            state["cycle"] += 1
            state["node_results"] = {}
            state["phase"] = "Observe"
            return self._result("running", state)
        if decision == "continue":
            state.update(decision="stopped", goal_outcome="inconclusive")
            evaluation["reason"] = "cycle_budget_exhausted"
        return self._terminal("completed", state)

    async def _hook(
        self, hook: StageHook, task: JsonObject, state: JsonObject, plan: JsonObject
    ) -> JsonObject:
        remaining = (
            plan["timeout_seconds"]
            - (self.clock() - parse_utc(state["started_at"])).total_seconds()
        )
        if remaining <= 0:
            raise TimeoutError("task wall time budget exhausted")
        async with asyncio.timeout(remaining):
            result = await hook(task, copy.deepcopy(state))
        if not isinstance(result, dict):
            raise TypeError("stage hook must return a JSON object")
        canonical_json(result)
        return result

    @staticmethod
    def _event(task: JsonObject, state: JsonObject, kind: str, **data: Any) -> JsonObject:
        return {
            "tenant_id": task["tenant_id"],
            "task_id": task["task_id"],
            "plan_digest": state["plan_digest"],
            "cycle": state["cycle"],
            "phase": state["phase"],
            "kind": kind,
            **data,
        }

    @staticmethod
    def _result(
        status: str, state: JsonObject, *, events: list[JsonObject] | None = None, **extra: Any
    ) -> RuntimeResult:
        return RuntimeResult(
            status,
            state,
            {
                "execution_health": state["execution_health"],
                "goal_outcome": state["goal_outcome"],
                "decision": state["decision"],
                "token_usage": state["token_usage"],
                **extra,
            },
            events or [],
        )

    def _terminal(
        self,
        status: str,
        state: JsonObject,
        *,
        events: list[JsonObject] | None = None,
        **extra: Any,
    ) -> RuntimeResult:
        state["terminal_status"] = status
        if status in {"failed", "uncertain"}:
            state.update(execution_health=status, decision="stopped")
        state["terminal_details"] = extra
        return self._result(status, state, events=events, **extra)
