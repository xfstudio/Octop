"""Private evaluation campaigns using the ordinary frozen DAG execution path.

Scores describe recorded checks. They never replace a task's independent goal
verification, and synthetic checks cannot authorize a production evolution.
"""

from __future__ import annotations

import asyncio
import copy
import math
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from octop.infra.db.repos.evaluation_center import EvaluationRepo
from octop.infra.db.repos.longterm import AuthorityRevoked, RevisionConflict, canonical, digest
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.execution import compile_workflow, validate_workflow
from octop.infra.users.identity import User
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop.infra.longterm.service import LongtermService

_FINAL = {"verified_pass", "verified_fail", "infra_error", "unverified", "cancelled"}
_TASK_FINAL = {"completed", "failed", "cancelled", "uncertain"}
_GRADERS = {
    "json_equals",
    "contains",
    "required_operation",
    "forbidden_operation",
    "execution_status",
    "max_duration_ms",
    "max_tokens",
}
_MISSING = object()


def _invalid(reason: str, status: int = 422) -> OctopError:
    return OctopError(
        ErrorCode.LONGTERM_INVALID,
        "invalid evaluation operation",
        status=status,
        details={"reason": reason},
    )


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _invalid(f"{name}_must_be_{low}_to_{high}")
    return value


def _pointer(value: Any, path: str) -> Any:
    if not path:
        return value
    for part in path.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif (
            isinstance(value, list)
            and key.isascii()
            and key.isdecimal()
            and len(key) < 9
            and (key == "0" or not key.startswith("0"))
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            return _MISSING
    return value


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _operation(node: dict[str, Any], payload: dict[str, Any]) -> str:
    recorded = payload.get("operation")
    if isinstance(recorded, str):
        return recorded
    metadata = recorded if isinstance(recorded, dict) else node
    skill = metadata.get("skill", node.get("skill", "agent.analyze"))
    if skill == "component.call":
        return f"component.{metadata.get('component_id', node.get('component_id', ''))}.{metadata.get('operation', node.get('operation', ''))}"
    return str(skill)


def native_trace(task: dict[str, Any], evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose real dispatch records, never pretend model-internal tools were observed."""
    nodes = {node["id"]: node for node in task["workflow"]["nodes"]}
    result = []
    for event in evidence.get("events", []):
        payload = event.get("payload", {})
        if payload.get("kind") != "action_completed":
            continue
        node = nodes.get(payload.get("node_id"), {})
        result.append(
            {
                "node_id": payload.get("node_id"),
                "cycle": payload.get("cycle"),
                "operation": _operation(node, payload),
                "trace_kind": "node_operation",
                "status": payload.get("status"),
                "arguments": payload.get("arguments"),
                "input_digest": payload.get("input_digest"),
                "input_keys": payload.get("input_keys"),
                "result": payload.get("result"),
                "error_type": payload.get("error_type"),
                "started_at": payload.get("started_at"),
                "finished_at": payload.get("finished_at", payload.get("completed_at")),
                "duration_ms": _finite(payload.get("duration_ms")),
                "total_tokens": payload.get("token_measured"),
                "evidence_ref": f"octop-evidence:{task['task_id']}:{event['sequence']}:{event['digest']}",
            }
        )
    return result


def grade_case(
    case: dict[str, Any],
    envelope: dict[str, Any],
    *,
    evidence_refs: list[str],
    trace_complete: bool,
) -> dict[str, Any]:
    checks = []
    for rule in case["graders"]:
        kind, status, reason = rule["type"], "unverified", "required_observation_missing"
        value = _pointer(envelope, rule.get("path", ""))
        passed: bool | None = None
        if kind in {"json_equals", "contains"} and value is not _MISSING:
            expected = rule.get("expected")
            if kind == "json_equals":
                passed = canonical(value) == canonical(expected)
            elif isinstance(value, str) and isinstance(expected, str) or isinstance(value, list):
                passed = expected in value
            else:
                passed = False
        elif kind in {"required_operation", "forbidden_operation"}:
            matches = [
                row for row in envelope.get("trace", []) if row["operation"] == rule["operation"]
            ]
            if kind == "required_operation":
                if any(row.get("status") == "succeeded" for row in matches):
                    passed = True
                elif trace_complete:
                    passed = False
            elif matches:
                passed = False
            elif trace_complete:
                passed = True
        elif kind == "execution_status":
            actual = envelope.get("task", {}).get("status")
            passed = actual == rule.get("expected") if actual else None
        elif kind in {"max_duration_ms", "max_tokens"}:
            measured = _finite(
                envelope.get("metrics", {}).get(
                    "duration_ms" if kind == "max_duration_ms" else "total_tokens"
                )
            )
            if measured is not None:
                passed = measured <= rule["expected"]
        if passed is not None:
            status = "passed" if passed else "failed"
            reason = "rule_satisfied" if passed else "rule_not_satisfied"
        checks.append(
            {
                "id": rule["id"],
                "type": kind,
                "required": rule.get("required", True),
                "safety": rule.get("safety", False),
                "status": status,
                "reason": reason,
                "evidence_refs": evidence_refs,
            }
        )
    required = [row for row in checks if row["required"]]
    unknown = any(row["status"] == "unverified" for row in required)
    failed = any(row["status"] == "failed" for row in required)
    passed = False if failed else None if unknown else True
    safety = [row for row in checks if row["safety"]]
    safety_passed = (
        False
        if any(row["status"] == "failed" for row in safety)
        else None
        if any(row["status"] == "unverified" for row in safety)
        else True
    )
    return {
        "passed": passed,
        "score": None
        if unknown
        else sum(row["status"] == "passed" for row in required) / len(required),
        "evaluation_outcome": "passed"
        if passed is True
        else "failed"
        if passed is False
        else "unverified",
        "checks": checks,
        "checks_total": len(required),
        "checks_passed": sum(row["status"] == "passed" for row in required),
        "safety_passed": safety_passed,
        "metrics": envelope.get("metrics", {}),
        "evidence_refs": evidence_refs,
    }


def aggregate_results(
    cases: list[dict[str, Any]], repeats: int, attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fixed planned matrix; missing observations stay unknown instead of disappearing."""
    by_key = {(row["case_id"], row["repeat_index"]): row for row in attempts}
    per_case = []
    for case in cases:
        rows = [by_key.get((case["case_id"], repeat), {}) for repeat in range(1, repeats + 1)]
        good = sum(row.get("status") == "verified_pass" for row in rows)
        bad = sum(row.get("status") == "verified_fail" for row in rows)
        complete = good + bad == repeats
        capacities = [
            row.get("result", {}).get("score")
            if row.get("status") in {"verified_pass", "verified_fail"}
            else None
            for row in rows
        ]
        per_case.append(
            {
                "case_id": case["case_id"],
                "planned": repeats,
                "passed": good,
                "failed": bad,
                "unknown": repeats - good - bad,
                "trial_pass_rate": good / repeats if complete else None,
                "pass_rate_lower": good / repeats,
                "pass_rate_upper": (repeats - bad) / repeats,
                "at_least_one_passed": True if good else False if complete else None,
                "all_trials_passed": False if bad else True if complete else None,
                "capacity": sum(float(value) for value in capacities if value is not None) / repeats
                if all(_finite(value) is not None for value in capacities)
                else None,
                "pass_at_k": {
                    str(k): 1 - math.comb(repeats - good, k) / math.comb(repeats, k)
                    if repeats - good >= k
                    else 1.0
                    for k in range(1, repeats + 1)
                }
                if complete
                else {},
            }
        )
    count, planned = len(cases), len(cases) * repeats
    verified = sum(row["passed"] + row["failed"] for row in per_case)
    complete = verified == planned

    def mean(key: str) -> float | None:
        values = [row[key] for row in per_case]
        return sum(values) / count if count and all(value is not None for value in values) else None

    return {
        "protocol": "octop-evaluation-v1",
        "status": "complete" if complete else "incomplete",
        "planned_cases": count,
        "planned_attempts": planned,
        "verified_attempts": verified,
        "coverage": verified / planned if planned else None,
        "pass_rate": mean("trial_pass_rate"),
        "pass_rate_lower": mean("pass_rate_lower"),
        "pass_rate_upper": mean("pass_rate_upper"),
        "at_least_one_success_rate": mean("at_least_one_passed"),
        "all_trials_success_rate": mean("all_trials_passed"),
        "capacity": mean("capacity"),
        "pass_at_k": {
            str(k): sum(row["pass_at_k"][str(k)] for row in per_case) / count
            for k in range(1, repeats + 1)
        }
        if complete and count
        else {},
        "status_counts": {
            status: sum(row.get("status") == status for row in attempts)
            for status in sorted(_FINAL | {"pending", "running"})
        },
        "per_case": per_case,
        "limitations": [
            "Missing-result bounds are not confidence intervals.",
            "Repeated trials do not increase the number of independent cases.",
        ],
    }


def builtin_suite() -> dict[str, Any]:
    cases = [
        {
            "case_id": "unicode-echo",
            "title": "离线结构化输出保真",
            "goal": "原样保留中文和边界数值",
            "input_overrides": {"echo": {"message": "离线可用", "value": 0}},
            "graders": [
                {
                    "id": "message",
                    "type": "json_equals",
                    "path": "/nodes/echo/result/output/message",
                    "expected": "离线可用",
                },
                {"id": "path", "type": "required_operation", "operation": "local.echo"},
            ],
        },
        {
            "case_id": "dependency-binding",
            "title": "多节点依赖传递",
            "goal": "通过声明的依赖传递结构化结果",
            "workflow": {
                "nodes": [
                    {
                        "id": "seed",
                        "skill": "local.echo",
                        "depends_on": [],
                        "input": {"items": ["A", "B"]},
                    },
                    {
                        "id": "review",
                        "skill": "local.echo",
                        "depends_on": ["seed"],
                        "input": {"items": {"$node": "seed", "pointer": "/result/output/items"}},
                    },
                ]
            },
            "graders": [
                {
                    "id": "bound-value",
                    "type": "json_equals",
                    "path": "/nodes/review/result/output/items",
                    "expected": ["A", "B"],
                },
                {"id": "dispatch", "type": "execution_status", "expected": "completed"},
            ],
        },
        {
            "case_id": "boundary-preservation",
            "title": "空值与布尔边界",
            "goal": "保留false、null、空列表的真实类型",
            "input_overrides": {"echo": {"flag": False, "missing": None, "items": []}},
            "graders": [
                {
                    "id": "types",
                    "type": "json_equals",
                    "path": "/nodes/echo/result/output",
                    "expected": {"flag": False, "missing": None, "items": []},
                },
                {
                    "id": "no-external-write",
                    "type": "forbidden_operation",
                    "operation": "component.semantica.index",
                    "safety": True,
                },
            ],
        },
    ]
    value = {
        "suite_id": "builtin-contract-v1",
        "name": "本机 DAG 合同回归",
        "split": "regression",
        "version": 1,
        "revision": 1,
        "cases": cases,
        "source_class": "synthetic",
        "description": "三项真实本机工作流检查，不调用模型，不代表模型能力或商业目标达成。",
    }
    value["digest"] = digest(value)
    return value


def load_cab_report(root: Path, directory: str) -> dict[str, Any]:
    """Reuse containment checks and enrich only allowlisted upstream metadata."""
    from octop.infra.longterm.evaluation import CommerceBenchAdapter

    adapter = CommerceBenchAdapter(root)
    report = adapter.load_run(directory)
    run_dir = (adapter.results_root / directory).resolve()
    provenance = report["provenance"]
    extra: list[dict[str, str]] = []
    manifest = adapter._read(run_dir, "manifest.json", extra, required=True) or {}
    reward = adapter._read(run_dir, "verifier/final_reward.json", extra) or manifest.get(
        "reward", {}
    )
    reward = reward if isinstance(reward, dict) else {}
    raw_reward = adapter._read(run_dir, "verifier/reward.json", extra) or {}
    for item in extra:
        if item not in provenance:
            provenance.append(item)
    experiment = report["experiment"]
    for key in ("llm_judge_provider", "llm_judge_model", "runtime_os"):
        experiment[key] = manifest.get(key)
    alternative = manifest.get("alternative_harness", {})
    if isinstance(alternative, dict) and isinstance(alternative.get("openclaw_model"), str):
        experiment["model_name"] = alternative["openclaw_model"]
    report["synthetic"] = (
        manifest.get("llm_judge_provider") == "mock"
        or raw_reward.get("provider") == "mock"
        or reward.get("provider") == "mock"
    )
    counts: tuple[int, int] | None = None
    if str(reward.get("schema_version", "")).startswith("2."):
        passed, total = reward.get("checks_passed"), reward.get("checks_total")
        if type(passed) is int and type(total) is int and 0 <= passed <= total and total > 0:
            counts = passed, total
        elif isinstance(reward.get("checks_breakdown"), list) and reward["checks_breakdown"]:
            rows = reward["checks_breakdown"]
            if all(isinstance(row, dict) and isinstance(row.get("passed"), bool) for row in rows):
                counts = sum(row["passed"] for row in rows), len(rows)
    elif isinstance(reward.get("validation_checks"), list):
        rows = [
            row
            for row in reward["validation_checks"]
            if isinstance(row, dict) and row.get("id") != "llm_rubric_judge"
        ]
        if rows and all(isinstance(row.get("passed"), bool) for row in rows):
            counts = sum(row["passed"] for row in rows), len(rows)
    report["capacity"] = counts[0] / counts[1] if counts else None
    if counts:
        report["long_horizon_metrics"].update(checks_passed=counts[0], checks_total=counts[1])
    metrics = report["long_horizon_metrics"]
    if metrics.get("duration_ms") is None:
        try:
            start = datetime.fromisoformat(manifest["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(manifest["finished_at"].replace("Z", "+00:00"))
            metrics["duration_ms"] = _finite((end - start).total_seconds() * 1000)
        except (KeyError, TypeError, ValueError):
            pass
    return report


class EvaluationCenterService:
    def __init__(self, longterm_service: LongtermService) -> None:
        self.longterm = longterm_service
        self.repo = EvaluationRepo(longterm_service.services.db)
        self._jobs: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._closed = False

    def _authorize(
        self, tenant: str, user: User, space: str | None = None, *, write: bool = False
    ) -> None:
        account = self.longterm.services.user_repo.get(user.id)
        if account is None or account.disabled:
            raise OctopError(ErrorCode.FORBIDDEN, "evaluation account unavailable")
        self.longterm.authorize(tenant, user, write=write)
        if space:
            self.longterm.config(tenant, space, user)

    def _get(
        self, tenant: str, user: User, kind: str, identifier: str, *, write: bool = False
    ) -> dict[str, Any]:
        self._authorize(tenant, user, write=write)
        value = self.repo.get(tenant, kind, identifier, user.id)
        if value is None:
            raise OctopError(ErrorCode.NOT_FOUND, "evaluation not found")
        self._authorize(tenant, user, value["space_id"], write=write)
        return value

    def suites(self, tenant: str, user: User, space: str) -> dict[str, Any]:
        self._authorize(tenant, user, space)
        return {
            "suites": self.repo.list(tenant, "suite", user.id, space),
            "builtin": builtin_suite(),
        }

    @staticmethod
    def validate_cases(cases: Any) -> list[dict[str, Any]]:
        from octop.infra.longterm.service import validate_public_data

        if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
            raise _invalid("cases_must_contain_1_to_64_items")
        validate_public_data(cases)
        if len(canonical(cases).encode()) > 512 * 1024:
            raise _invalid("cases_too_large")
        result, seen = [], set()
        for case in cases:
            if not isinstance(case, dict) or set(case) - {
                "case_id",
                "title",
                "goal",
                "input_overrides",
                "workflow",
                "graders",
                "tags",
                "risk",
            }:
                raise _invalid("invalid_case_fields")
            identifier = case.get("case_id")
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or len(identifier) > 120
                or identifier in seen
            ):
                raise _invalid("case_ids_must_be_distinct_bounded_strings")
            seen.add(identifier)
            if not isinstance(case.get("goal"), str) or not 1 <= len(case["goal"]) <= 8000:
                raise _invalid("case_requires_goal")
            rules = case.get("graders")
            if not isinstance(rules, list) or not 1 <= len(rules) <= 32:
                raise _invalid("case_requires_1_to_32_graders")
            rule_ids = set()
            for rule in rules:
                if not isinstance(rule, dict) or set(rule) - {
                    "id",
                    "type",
                    "path",
                    "expected",
                    "operation",
                    "required",
                    "safety",
                }:
                    raise _invalid("invalid_grader_fields")
                if (
                    not isinstance(rule.get("id"), str)
                    or not 1 <= len(rule["id"]) <= 120
                    or rule["id"] in rule_ids
                    or rule.get("type") not in _GRADERS
                ):
                    raise _invalid("invalid_grader_identity")
                rule_ids.add(rule["id"])
                if (
                    type(rule.get("required", True)) is not bool
                    or type(rule.get("safety", False)) is not bool
                ):
                    raise _invalid("grader_flags_must_be_booleans")
                path = rule.get("path", "")
                if (
                    not isinstance(path, str)
                    or len(path) > 1024
                    or (path and not path.startswith("/"))
                ):
                    raise _invalid("grader_requires_json_pointer")
                if rule["type"] in {"required_operation", "forbidden_operation"}:
                    if (
                        not isinstance(rule.get("operation"), str)
                        or not 1 <= len(rule["operation"]) <= 200
                    ):
                        raise _invalid("grader_requires_operation")
                elif "expected" not in rule:
                    raise _invalid("grader_requires_expected_value")
                if (
                    rule["type"] in {"max_duration_ms", "max_tokens"}
                    and _finite(rule["expected"]) is None
                ):
                    raise _invalid("grader_limit_requires_finite_number")
            if not any(rule.get("required", True) for rule in rules):
                raise _invalid("at_least_one_required_grader")
            overrides = case.get("input_overrides", {})
            if not isinstance(overrides, dict) or any(
                not isinstance(key, str) or not isinstance(value, dict)
                for key, value in overrides.items()
            ):
                raise _invalid("invalid_input_overrides")
            if "workflow" in case:
                nodes = validate_workflow(case["workflow"])
                if any(node.get("skill") != "local.echo" for node in nodes):
                    raise _invalid("fixture_workflows_only_allow_local_echo")
            if not isinstance(case.get("tags", []), list) or any(
                not isinstance(tag, str) or len(tag) > 100 for tag in case.get("tags", [])
            ):
                raise _invalid("invalid_case_tags")
            result.append(copy.deepcopy(case))
        return result

    def save_suite(self, tenant: str, user: User, data: dict[str, Any]) -> dict[str, Any]:
        space = data["space_id"]
        self._authorize(tenant, user, space, write=True)
        name = data.get("name")
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 200
            or data.get("split", "capability") not in {"capability", "regression", "holdout"}
        ):
            raise _invalid("invalid_suite_name_or_split")
        cases = self.validate_cases(data["cases"])
        identifier, expected = data.get("suite_id") or new_ulid(), data.get("expected_revision", 0)
        prior = (
            self._get(tenant, user, "suite", identifier, write=True)
            if data.get("suite_id")
            else None
        )
        if prior and (prior["space_id"] != space or prior["revision"] != expected):
            raise RevisionConflict("suite revision conflict")
        if prior is None and expected != 0:
            raise RevisionConflict("new suite revision must be zero")
        value = {
            "kind": "suite",
            "document_id": identifier,
            "suite_id": identifier,
            "tenant_id": tenant,
            "space_id": space,
            "creator_user_id": user.id,
            "name": name.strip(),
            "split": data.get("split", "capability"),
            "cases": cases,
            "version": prior["version"] + 1 if prior else 1,
            "status": "active",
        }
        value["digest"] = digest(
            {key: value[key] for key in ("suite_id", "version", "split", "cases")}
        )
        return self.repo.save(value, expected=expected, actor=user.id)

    def suite(
        self, tenant: str, user: User, identifier: str, version: int | None = None
    ) -> dict[str, Any]:
        if identifier == "builtin-contract-v1":
            if version not in {None, 1}:
                raise _invalid("builtin_version_not_found", 404)
            self._authorize(tenant, user)
            return builtin_suite()
        latest = self._get(tenant, user, "suite", identifier)
        result = self.repo.suite_version(tenant, identifier, version) if version else latest
        if result is None or result["creator_user_id"] != user.id:
            raise _invalid("suite_version_not_found", 404)
        return result

    def _check_subject(self, workflow: dict[str, Any], user: User) -> None:
        for node in validate_workflow(workflow):
            if node.get("skill") in {"local.echo", "component.call"}:
                if node.get("skill") == "component.call" and not isinstance(
                    node.get("component_snapshot"), dict
                ):
                    raise _invalid("frozen_component_snapshot_required")
                continue
            row = self.longterm.services.agent_repo.get(str(node.get("agent_id") or ""))
            if row is None or row.user_id != user.id or not row.enabled:
                raise OctopError(ErrorCode.FORBIDDEN, "evaluation agent not available")
            snapshot = node.get("behavior_snapshot")
            if not isinstance(snapshot, dict) or not snapshot.get("model_ref"):
                raise _invalid("frozen_agent_snapshot_required")

    def create_run(self, tenant: str, user: User, data: dict[str, Any]) -> dict[str, Any]:
        space = data["space_id"]
        self._authorize(tenant, user, space, write=True)
        suite = self.suite(
            tenant, user, data.get("suite_id", "builtin-contract-v1"), data.get("suite_version")
        )
        if suite.get("space_id", space) != space:
            raise _invalid("suite_space_mismatch")
        repeats = _integer(data.get("repeats", 3), "repeats", 1, 5)
        maximum = _integer(data.get("max_total_tokens", 100000), "max_total_tokens", 1, 2000000)
        parallel = _integer(data.get("max_parallel", 2), "max_parallel", 1, 4)
        mode = data.get("mode", "local")
        if mode not in {"local", "temporal"}:
            raise _invalid("invalid_runtime_mode")
        subject = data.get("subject", {"kind": "builtin"})
        if (
            not isinstance(subject, dict)
            or subject.get("kind") not in {"builtin", "source_task"}
            or set(subject) - {"kind", "task_id"}
        ):
            raise _invalid("invalid_subject")
        subject = copy.deepcopy(subject)
        if subject["kind"] == "source_task":
            source = self.longterm.task(tenant, str(subject.get("task_id", "")), user)
            if source["space_id"] != space:
                raise _invalid("source_task_space_mismatch")
            self._check_subject(source["workflow"], user)
            subject.update(
                workflow=copy.deepcopy(source["workflow"]),
                acceptance=copy.deepcopy(source.get("acceptance", {})),
                source_plan_digest=source["plan_digest"],
            )
        subject["digest"] = digest(
            {key: value for key, value in subject.items() if key != "source_plan_digest"}
        )
        identifier = new_ulid()
        synthetic = subject["kind"] == "builtin" or all(
            node.get("skill") == "local.echo"
            for node in subject.get("workflow", {}).get("nodes", [])
        )
        value = {
            "kind": "run",
            "document_id": identifier,
            "run_id": identifier,
            "tenant_id": tenant,
            "space_id": space,
            "creator_user_id": user.id,
            "suite": copy.deepcopy(suite),
            "subject": subject,
            "source_class": "synthetic" if synthetic else "native",
            "harness": "octop-native",
            "repeats": repeats,
            "mode": mode,
            "max_total_tokens": maximum,
            "max_parallel": parallel,
            "status": "running",
        }
        attempts, reserved = [], 0
        for case in suite["cases"]:
            task = self._task_document(value, case, user, new_ulid())
            reserved += compile_workflow(task)["max_tokens"] * repeats
            for repeat in range(1, repeats + 1):
                attempts.append(
                    {
                        "attempt_id": new_ulid(),
                        "case_id": case["case_id"],
                        "repeat_index": repeat,
                        "native_task_id": new_ulid(),
                        "status": "pending",
                        "revision": 1,
                    }
                )
        if reserved > maximum:
            raise _invalid("planned_token_reservations_exceed_total_budget")
        value["reserved_tokens"] = reserved
        value["aggregate"] = aggregate_results(suite["cases"], repeats, attempts)
        saved = self.repo.save(
            value,
            actor=user.id,
            command_key=f"{user.id}:run:{data.get('idempotency_key') or uuid4().hex}",
            request_digest=digest(data),
            attempts=attempts,
        )
        self.schedule(tenant, saved["run_id"])
        return saved

    def _task_document(
        self, run: dict[str, Any], case: dict[str, Any], user: User, identifier: str
    ) -> dict[str, Any]:
        if run["subject"]["kind"] == "source_task":
            workflow = copy.deepcopy(run["subject"]["workflow"])
        else:
            workflow = copy.deepcopy(
                case.get(
                    "workflow",
                    {
                        "nodes": [
                            {
                                "id": "echo",
                                "skill": "local.echo",
                                "risk": "R0",
                                "depends_on": [],
                                "input": {},
                            }
                        ]
                    },
                )
            )
            workflow.update(max_tokens=1024, max_cycles=1, timeout_seconds=60, max_actions=32)
        nodes = {node["id"]: node for node in workflow["nodes"]}
        for node_id, inputs in case.get("input_overrides", {}).items():
            if node_id not in nodes:
                raise _invalid("case_input_override_node_not_found")
            nodes[node_id]["input"] = copy.deepcopy(inputs)
        value = {
            "tenant_id": run["tenant_id"],
            "task_id": identifier,
            "space_id": run["space_id"],
            "creator_user_id": user.id,
            "goal": case["goal"],
            "mode": run["mode"],
            "workflow": workflow,
            "acceptance": copy.deepcopy(run["subject"].get("acceptance", {})),
            "control": {},
            "artifacts": {},
            "evaluation_context": {
                "run_id": run["run_id"],
                "case_id": case["case_id"],
                "subject_digest": run["subject"]["digest"],
            },
        }
        value["plan_digest"] = compile_workflow(value)["digest"]
        if len(canonical(value).encode()) > 524288:
            raise _invalid("frozen_evaluation_task_too_large")
        return value

    def runs(self, tenant: str, user: User, space: str) -> dict[str, Any]:
        self._authorize(tenant, user, space)
        return {"runs": self.repo.list(tenant, "run", user.id, space)}

    def run(self, tenant: str, user: User, identifier: str) -> dict[str, Any]:
        value = self._get(tenant, user, "run", identifier)
        return {
            **value,
            "attempts": self.repo.attempts(tenant, identifier),
            "badcases": [
                row
                for row in self.repo.list(tenant, "badcase", user.id, value["space_id"])
                if row["run_id"] == identifier
            ],
        }

    def attempt(self, tenant: str, user: User, run_id: str, identifier: str) -> dict[str, Any]:
        run = self._get(tenant, user, "run", run_id)
        row = next(
            (row for row in self.repo.attempts(tenant, run_id) if row["attempt_id"] == identifier),
            None,
        )
        if row is None:
            raise _invalid("attempt_not_found", 404)
        if row.get("native_task_id") and run["source_class"] != "external_import":
            task = self.longterm.task(tenant, row["native_task_id"], user)
            evidence = self._evidence(tenant, task["task_id"], user)
            row = {
                **row,
                "trace": native_trace(task, evidence),
                "evidence_valid": evidence["valid"],
                "task": {
                    key: task.get(key)
                    for key in ("task_id", "status", "plan_digest", "result", "checkpoint")
                },
            }
        return row

    def _evidence(self, tenant: str, task_id: str, user: User) -> dict[str, Any]:
        """Read the complete frozen prefix, not just the first API page."""
        first = self.longterm.evidence(tenant, task_id, user)
        head, limit = first["head_digest"], first["last_sequence"]
        rows, valid = list(first["events"]), first.get("valid") is True
        cursor = rows[-1]["sequence"] if rows else 0
        while cursor < limit and len(rows) <= 65536:
            page = self.longterm.evidence(tenant, task_id, user, after=cursor)
            selected = [row for row in page["events"] if row["sequence"] <= limit]
            valid = valid and page.get("valid") is True
            if not selected or selected[0]["sequence"] != cursor + 1:
                valid = False
                break
            rows.extend(selected)
            cursor = rows[-1]["sequence"]
        valid = (
            valid
            and cursor == limit
            and len(rows) == limit
            and (rows[-1]["digest"] if rows else "0" * 64) == head
        )
        return {"events": rows, "head_digest": head, "last_sequence": limit, "valid": valid}

    def schedule(self, tenant: str, identifier: str) -> None:
        key = tenant, identifier
        if not self._closed and (key not in self._jobs or self._jobs[key].done()):
            self._jobs[key] = asyncio.create_task(
                self._run(tenant, identifier), name=f"evaluation-{identifier}"
            )

    async def recover(self) -> None:
        for tenant, identifier in self.repo.recoverable():
            self.schedule(tenant, identifier)

    async def close(self) -> None:
        self._closed = True
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()

    async def _run(self, tenant: str, identifier: str) -> None:
        owner = uuid4().hex
        try:
            while not self._closed:
                if not self.repo.claim(tenant, identifier, owner):
                    await asyncio.sleep(0.5)
                    continue
                run = self.repo.get(tenant, "run", identifier)
                if run is None:
                    return
                if run["status"] != "running":
                    if run["status"] in {"paused", "cancelled"}:
                        user = User(run["creator_user_id"], "evaluation-worker", "user", None)
                        try:
                            settled = self._control_tasks(
                                run, user, "pause" if run["status"] == "paused" else "cancel"
                            )
                        except (OctopError, AuthorityRevoked):
                            return
                        if not settled:
                            await asyncio.sleep(0.25)
                            continue
                        if run["status"] == "cancelled":
                            await self.tick(tenant, identifier)
                    return
                try:
                    await self.tick(tenant, identifier)
                except RevisionConflict:
                    pass
                except (OctopError, AuthorityRevoked) as exc:
                    run = self.repo.get(tenant, "run", identifier)
                    if run and run["status"] == "running":
                        run.update(status="paused", last_error=type(exc).__name__)
                        with suppress(RevisionConflict):
                            self.repo.save(run, expected=run["revision"])
                    return
                await asyncio.sleep(0.25)
        finally:
            self.repo.release(tenant, identifier, owner)

    async def tick(self, tenant: str, identifier: str) -> None:
        run = self.repo.get(tenant, "run", identifier)
        if run is None:
            return
        user = User(run["creator_user_id"], "evaluation-worker", "user", None)
        if run["status"] in {"paused", "cancelled"}:
            self._control_tasks(run, user, "pause" if run["status"] == "paused" else "cancel")
            if run["status"] == "cancelled":
                latest = self.repo.get(tenant, "run", identifier)
                if latest and latest["status"] == "cancelled":
                    aggregate = aggregate_results(
                        run["suite"]["cases"],
                        run["repeats"],
                        self.repo.attempts(tenant, identifier),
                    )
                    if aggregate != latest["aggregate"]:
                        latest["aggregate"] = aggregate
                        self.repo.save(latest, expected=latest["revision"])
            return
        if run["status"] != "running" or run["mode"] == "import":
            return
        self._authorize(tenant, user, run["space_id"], write=True)
        if run.get("desired_control") == "resume":
            if not self._control_tasks(run, user, "resume"):
                return
            run.pop("desired_control", None)
            run = self.repo.save(run, expected=run["revision"], actor=user.id)
        attempts = self.repo.attempts(tenant, identifier)
        cases = {case["case_id"]: case for case in run["suite"]["cases"]}
        active = 0
        for attempt in attempts:
            if attempt["status"] in _FINAL:
                continue
            task = self.longterm.repo.task(tenant, attempt["native_task_id"])
            if task and task["status"] in _TASK_FINAL:
                self._finalize(run, attempt, cases[attempt["case_id"]], task, user)
                continue
            if task and task["status"] != "draft":
                active += 1
                if attempt["status"] != "running":
                    attempt["status"] = "running"
                    self.repo.save_attempt(tenant, identifier, attempt)
                if task["status"] in {"queued", "running", "sleeping"}:
                    self.longterm.schedule(tenant, task["task_id"])
        for attempt in self.repo.attempts(tenant, identifier):
            if attempt["status"] != "pending" or active >= run["max_parallel"]:
                continue
            current = self.repo.get(tenant, "run", identifier)
            if current is None or current["status"] != "running":
                return
            case = cases[attempt["case_id"]]
            task = self.longterm.repo.task(tenant, attempt["native_task_id"])
            if task is None:
                document = self._task_document(run, case, user, attempt["native_task_id"])
                self._check_subject(document["workflow"], user)
                key = f"{user.id}:eval-create:{identifier}:{attempt['attempt_id']}"
                task = self.longterm.repo.create_task(document, key, digest(document))
            current = self.repo.get(tenant, "run", identifier)
            if current is None or current["status"] != "running":
                if current and current["status"] in {"paused", "cancelled"}:
                    self._control_tasks(
                        current, user, "pause" if current["status"] == "paused" else "cancel"
                    )
                return
            if task["status"] == "draft":
                task = self.longterm.command(
                    tenant,
                    task["task_id"],
                    user,
                    {
                        "command": "start",
                        "expected_revision": task["revision"],
                        "idempotency_key": f"eval-start:{identifier}:{attempt['attempt_id']}",
                    },
                )
            if task["status"] in _TASK_FINAL:
                self._finalize(run, attempt, case, task, user)
            else:
                attempt["status"] = "running"
                self.repo.save_attempt(tenant, identifier, attempt)
                self.longterm.schedule(tenant, task["task_id"])
                active += 1
        latest = self.repo.get(tenant, "run", identifier)
        if latest is None or latest["status"] != "running":
            return
        attempts = self.repo.attempts(tenant, identifier)
        aggregate = aggregate_results(run["suite"]["cases"], run["repeats"], attempts)
        status = (
            "completed" if all(attempt["status"] in _FINAL for attempt in attempts) else "running"
        )
        if aggregate != latest.get("aggregate") or latest["status"] != status:
            latest.update(aggregate=aggregate, status=status)
            self.repo.save(latest, expected=latest["revision"])

    def _finalize(
        self,
        run: dict[str, Any],
        attempt: dict[str, Any],
        case: dict[str, Any],
        task: dict[str, Any],
        user: User,
    ) -> None:
        evidence = self._evidence(run["tenant_id"], task["task_id"], user)
        trace = native_trace(task, evidence)
        refs = [row["evidence_ref"] for row in trace]
        if not refs and evidence.get("head_digest"):
            refs = [
                f"octop-evidence:{task['task_id']}:{evidence['last_sequence']}:{evidence['head_digest']}"
            ]
        checkpoint = task.get("checkpoint", {})
        duration = None
        if task.get("updated_at") is not None and task.get("created_at") is not None:
            duration = max(0, task["updated_at"] - task["created_at"]) * 1000
        metrics = {
            "duration_ms": duration,
            "duration_source": "database_wall_time",
            "total_tokens": checkpoint.get("token_usage", {}).get("total_tokens"),
            "node_operations": len(trace),
            "tool_calls": None,
        }
        envelope = {
            "nodes": checkpoint.get("node_results", {}),
            "task": {
                "status": task["status"],
                "goal_outcome": (task.get("result") or {}).get("goal_outcome"),
            },
            "trace": trace,
            "metrics": metrics,
        }
        result = grade_case(
            case, envelope, evidence_refs=refs, trace_complete=evidence.get("valid") is True
        )
        if evidence.get("valid") is not True:
            result.update(passed=None, score=None, evaluation_outcome="unverified")
        status = (
            "verified_pass"
            if result["passed"] is True
            else "verified_fail"
            if result["passed"] is False
            else "unverified"
        )
        if task["status"] == "cancelled":
            status = "cancelled"
        elif task["status"] in {"failed", "uncertain"} and result["passed"] is not False:
            status = "infra_error"
        attempt.update(
            status=status,
            result=result,
            task_status=task["status"],
            task_goal_outcome=envelope["task"]["goal_outcome"],
            plan_digest=task["plan_digest"],
        )
        self.repo.save_attempt(run["tenant_id"], run["run_id"], attempt)
        if status != "verified_pass":
            self._badcase(run, attempt)

    def _badcase(self, run: dict[str, Any], attempt: dict[str, Any]) -> None:
        identifier = f"{run['run_id']}-{attempt['attempt_id']}"
        if self.repo.get(run["tenant_id"], "badcase", identifier):
            return
        self.repo.save(
            {
                "kind": "badcase",
                "document_id": identifier,
                "badcase_id": identifier,
                "tenant_id": run["tenant_id"],
                "space_id": run["space_id"],
                "creator_user_id": run["creator_user_id"],
                "run_id": run["run_id"],
                "attempt_id": attempt["attempt_id"],
                "case_id": attempt["case_id"],
                "suite_digest": run["suite"]["digest"],
                "status": "open",
                "root_cause": "unclassified",
                "fix_ref": "",
                "failed_rules": [
                    check["id"]
                    for check in attempt.get("result", {}).get("checks", [])
                    if check["status"] != "passed"
                ],
            }
        )

    def command(
        self, tenant: str, user: User, identifier: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        run = self._get(tenant, user, "run", identifier, write=True)
        key = (
            f"{user.id}:control:{identifier}:{data['idempotency_key']}"
            if data.get("idempotency_key")
            else None
        )
        if key:
            prior = self.repo.prior(tenant, key, digest(data), user.id)
            if prior:
                return {**prior, "attempts": self.repo.attempts(tenant, identifier)}
        if run["revision"] != data["expected_revision"]:
            raise RevisionConflict("evaluation run revision conflict")
        command = data["command"]
        if command == "pause" and run["status"] == "running":
            run["status"] = "paused"
        elif command == "resume" and run["status"] == "paused":
            run["status"] = "running"
        elif command == "cancel" and run["status"] in {"running", "paused"}:
            run["status"] = "cancelled"
        else:
            raise _invalid("command_not_valid_for_current_state", 409)
        run["desired_control"] = command
        # Persist the campaign command first. Recovery can finish task commands after a crash.
        saved = self.repo.save(
            run,
            expected=run["revision"],
            actor=user.id,
            command_key=key,
            request_digest=digest(data),
        )
        self._control_tasks(saved, user, command)
        self.schedule(tenant, identifier)
        attempts = self.repo.attempts(tenant, identifier)
        if command == "cancel":
            saved["aggregate"] = aggregate_results(
                saved["suite"]["cases"], saved["repeats"], attempts
            )
            saved = self.repo.save(saved, expected=saved["revision"], actor=user.id)
        return {**saved, "attempts": attempts}

    def _control_tasks(self, run: dict[str, Any], user: User, command: str) -> bool:
        settled = True
        for attempt in self.repo.attempts(run["tenant_id"], run["run_id"]):
            # A cancelled attempt may precede a late admission from another
            # coordinator. Its preallocated task identity must still be checked.
            if attempt["status"] in _FINAL and not (
                command == "cancel" and attempt["status"] == "cancelled"
            ):
                continue
            task = self.longterm.repo.task(run["tenant_id"], attempt.get("native_task_id", ""))
            if task and task["status"] not in _TASK_FINAL:
                valid = (
                    command == "cancel"
                    or (
                        command == "pause"
                        and task["status"] in {"queued", "running", "waiting_approval", "sleeping"}
                    )
                    or (command == "resume" and task["status"] in {"paused", "sleeping"})
                )
                if valid:
                    try:
                        self.longterm.command(
                            run["tenant_id"],
                            task["task_id"],
                            user,
                            {
                                "command": command,
                                "expected_revision": task["revision"],
                                "idempotency_key": f"eval-control:{run['run_id']}:{run['revision']}:{command}:{attempt['attempt_id']}:{task['revision']}",
                            },
                        )
                    except RevisionConflict:
                        settled = False
                        continue
                    task = self.longterm.repo.task(run["tenant_id"], task["task_id"])
            if command == "cancel":
                if task is None or task["status"] in _TASK_FINAL:
                    attempt["status"] = "cancelled"
                    with suppress(RevisionConflict):
                        self.repo.save_attempt(run["tenant_id"], run["run_id"], attempt)
                else:
                    settled = False
        return settled

    def badcases(self, tenant: str, user: User, space: str) -> dict[str, Any]:
        self._authorize(tenant, user, space)
        return {"badcases": self.repo.list(tenant, "badcase", user.id, space)}

    def update_badcase(
        self, tenant: str, user: User, identifier: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        value = self._get(tenant, user, "badcase", identifier, write=True)
        if value["revision"] != data["expected_revision"]:
            raise RevisionConflict("badcase revision conflict")
        if data.get("status", value["status"]) not in {"open", "triaged", "fixed", "verified"}:
            raise _invalid("invalid_badcase_status")
        for key in ("root_cause", "fix_ref"):
            if key in data:
                if not isinstance(data[key], str) or len(data[key]) > 1000:
                    raise _invalid("invalid_badcase_note")
                value[key] = data[key]
        if data.get("verification_run_id"):
            run = self._get(tenant, user, "run", data["verification_run_id"])
            original = self._get(tenant, user, "run", value["run_id"])
            trials = [
                row
                for row in self.repo.attempts(tenant, run["run_id"])
                if row["case_id"] == value["case_id"]
            ]
            if (
                run["space_id"] != value["space_id"]
                or run["suite"]["digest"] != value["suite_digest"]
                or run["status"] != "completed"
                or run["source_class"] != original["source_class"]
                or len(trials) != run["repeats"]
                or any(row["status"] != "verified_pass" for row in trials)
            ):
                raise _invalid("matching_complete_passing_reassessment_required")
            value.update(status="verified", verification_run_id=run["run_id"])
        elif data.get("status") == "verified":
            raise _invalid("verified_requires_real_reassessment")
        else:
            value["status"] = data.get("status", value["status"])
        from octop.infra.longterm.service import validate_public_data

        validate_public_data(value)
        return self.repo.save(value, expected=value["revision"], actor=user.id)

    def graduate_badcase(
        self, tenant: str, user: User, identifier: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        value = self._get(tenant, user, "badcase", identifier, write=True)
        if value["revision"] != data["expected_revision"]:
            raise RevisionConflict("badcase revision conflict")
        if value["status"] != "verified":
            raise _invalid("only_verified_badcase_can_graduate")
        run = self._get(tenant, user, "run", value["run_id"])
        case = next(case for case in run["suite"]["cases"] if case["case_id"] == value["case_id"])
        return self.save_suite(
            tenant,
            user,
            {
                "space_id": value["space_id"],
                "name": data.get("name") or f"回归：{case.get('title', case['case_id'])}",
                "split": "regression",
                "cases": [case],
                "expected_revision": 0,
            },
        )

    def compare(self, tenant: str, user: User, data: dict[str, Any]) -> dict[str, Any]:
        baseline = self._get(tenant, user, "run", data["baseline_run_id"], write=True)
        candidate = self._get(tenant, user, "run", data["candidate_run_id"])
        threshold = _finite(data.get("min_improvement", 0.0))
        if threshold is None or threshold > 1:
            raise _invalid("invalid_improvement_threshold")
        reasons, pairs = [], []
        comparable = (
            baseline["space_id"] == candidate["space_id"]
            and baseline["suite"]["digest"] == candidate["suite"]["digest"]
            and baseline["repeats"] == candidate["repeats"]
            and baseline["harness"] == candidate["harness"]
            and baseline["source_class"] == candidate["source_class"]
        )
        if not comparable:
            reasons.append("incompatible_experiment_contract")
        base_rows = {
            (row["case_id"], row["repeat_index"]): row
            for row in self.repo.attempts(tenant, baseline["run_id"])
        }
        cand_rows = {
            (row["case_id"], row["repeat_index"]): row
            for row in self.repo.attempts(tenant, candidate["run_id"])
        }
        complete = (
            baseline["aggregate"]["status"] == candidate["aggregate"]["status"] == "complete"
            and base_rows.keys() == cand_rows.keys()
        )
        if not complete:
            reasons.append("complete_paired_matrix_required")
        if complete and comparable:
            for case in baseline["suite"]["cases"]:
                identifier = case["case_id"]
                left = [
                    base_rows[(identifier, repeat)] for repeat in range(1, baseline["repeats"] + 1)
                ]
                right = [
                    cand_rows[(identifier, repeat)] for repeat in range(1, candidate["repeats"] + 1)
                ]
                a = sum(row["status"] == "verified_pass" for row in left) / len(left)
                b = sum(row["status"] == "verified_pass" for row in right) / len(right)
                safety = all(
                    row.get("result", {}).get("safety_passed") is True for row in left + right
                )
                if not safety:
                    reasons.append(f"safety_not_verified:{identifier}")
                if b < a:
                    reasons.append(f"case_regression:{identifier}")
                pairs.append(
                    {
                        "case_id": identifier,
                        "baseline_pass_rate": a,
                        "candidate_pass_rate": b,
                        "improvement": b - a,
                        "safety_passed": safety,
                        "repeat_count": len(left),
                    }
                )
        improvement = sum(row["improvement"] for row in pairs) / len(pairs) if pairs else None
        if improvement is not None and improvement + 1e-12 < threshold:
            reasons.append("insufficient_improvement")
        gate = "INCOMPLETE" if not complete else "NO_GO" if reasons else "GO"
        identifier = new_ulid()
        # User-authored rules alone do not establish the evolution subsystem's
        # independent tenant, authorization and external side-effect hard gates.
        release_eligible = False
        value = {
            "kind": "comparison",
            "document_id": identifier,
            "comparison_id": identifier,
            "tenant_id": tenant,
            "space_id": baseline["space_id"],
            "creator_user_id": user.id,
            "status": "completed",
            "baseline_run_id": baseline["run_id"],
            "candidate_run_id": candidate["run_id"],
            "gate": gate,
            "release_eligible": release_eligible,
            "reasons": reasons,
            "mean_improvement": improvement,
            "case_pairs": pairs,
            "policy": {"min_improvement": threshold, "no_case_regression": True},
            "limitations": [
                "Synthetic and imported harness results cannot authorize native evolution publication.",
                "Release still requires the existing evolution safety and administrator gates.",
            ],
        }
        return self.repo.save(value, actor=user.id)

    def import_cab(self, tenant: str, user: User, data: dict[str, Any]) -> dict[str, Any]:
        from octop.infra.longterm.adapters import tenant_namespace

        self._authorize(tenant, user, data["space_id"], write=True)
        self.longterm.components.require_enabled("commerceagentbench")
        directories = data["run_dirs"]
        if (
            not isinstance(directories, list)
            or not 1 <= len(directories) <= 64
            or any(not isinstance(row, str) or len(row) > 400 for row in directories)
        ):
            raise _invalid("import_requires_1_to_64_relative_directories")
        base = self.longterm.components.directory("commerceagentbench", "results_subdirectory")
        root = base / tenant_namespace(tenant) / data["space_id"]
        if root.resolve() != root or not root.is_relative_to(base) or not root.is_dir():
            raise _invalid("private_report_directory_unavailable")
        reports = [load_cab_report(root, directory) for directory in directories]
        self._authorize(tenant, user, data["space_id"], write=True)
        self.longterm.components.require_enabled("commerceagentbench")
        if (
            self.longterm.components.directory("commerceagentbench", "results_subdirectory") != base
            or root.resolve() != root
            or not root.is_dir()
        ):
            raise RevisionConflict("benchmark result scope changed while importing")
        configurations = {
            canonical(
                {
                    key: report["experiment"].get(key)
                    for key in (
                        "harness",
                        "image",
                        "model_name",
                        "llm_judge_provider",
                        "llm_judge_model",
                    )
                }
            )
            for report in reports
        }
        if len(configurations) != 1:
            raise _invalid("import_requires_matching_harness_and_model_configuration")
        identities = [report["task_id"] for report in reports]
        if len(identities) != len(set(identities)):
            raise _invalid("import_task_ids_must_be_distinct")
        identifier, cases, attempts = new_ulid(), [], []
        for report in reports:
            case = {
                "case_id": report["task_id"],
                "title": report["task_id"],
                "goal": "External CAB verifier result",
                "graders": [
                    {"id": "external-verifier", "type": "execution_status", "expected": "verified"}
                ],
            }
            cases.append(case)
            passed = (
                True
                if report["goal_outcome"] == "achieved"
                else False
                if report["goal_outcome"] == "not_achieved"
                else None
            )
            attempts.append(
                {
                    "attempt_id": new_ulid(),
                    "case_id": report["task_id"],
                    "repeat_index": 1,
                    "status": "verified_pass"
                    if passed is True
                    else "verified_fail"
                    if passed is False
                    else "unverified",
                    "result": {
                        "passed": passed,
                        "score": report.get("capacity"),
                        "evaluation_outcome": "passed"
                        if passed is True
                        else "failed"
                        if passed is False
                        else "unverified",
                        "safety_passed": None,
                        "checks": [],
                        "metrics": report["long_horizon_metrics"],
                        "evidence_refs": [
                            f"sha256:{item['sha256']}" for item in report["provenance"]
                        ],
                    },
                    "external_report": report,
                    "revision": 1,
                }
            )
        suite = {
            "suite_id": "cab-import",
            "version": 1,
            "split": "capability",
            "name": "CommerceAgentBench 导入",
            "cases": cases,
            "digest": digest(identities),
        }
        value = {
            "kind": "run",
            "document_id": identifier,
            "run_id": identifier,
            "tenant_id": tenant,
            "space_id": data["space_id"],
            "creator_user_id": user.id,
            "status": "completed",
            "suite": suite,
            "subject": {
                "kind": "external",
                "digest": digest([report["experiment"] for report in reports]),
            },
            "source_class": "synthetic_import"
            if any(report["synthetic"] for report in reports)
            else "external_import",
            "harness": reports[0]["experiment"].get("harness") or "unknown",
            "repeats": 1,
            "mode": "import",
            "aggregate": aggregate_results(cases, 1, attempts),
            "limitations": [
                "Imported files do not independently attest the external execution.",
                "OpenClaw results do not measure the Octop native agent.",
            ],
        }
        saved = self.repo.save(
            value,
            actor=user.id,
            command_key=f"{user.id}:import:{data.get('idempotency_key') or uuid4().hex}",
            request_digest=digest(data),
            attempts=attempts,
        )
        return self.run(tenant, user, saved["run_id"])
