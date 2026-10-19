"""Inert, tenant-scoped RNA candidates with frozen paired-evaluation gates.

The RNA SDK's proposeEvolution/proposeProjectGenome return hypotheses. Neither
that output nor a successful task can grant capabilities or publish a behavior.
This module stores no credentials and performs no external writes.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import signal
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_HARD_GATES = ("tenant_isolation", "authorization", "external_side_effects")
_CANARY_GATES = ("authorization", "idempotency", "readback", "protection_state", "recovery")
_FROZEN_FIELDS = (
    "id",
    "tenant_id",
    "task_id",
    "baseline_revision",
    "benchmark_revision",
    "evidence_refs",
    "proposal",
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _references(values: Sequence[str]) -> list[str]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or not values
        or any(not isinstance(v, str) or not v.strip() for v in values)
    ):
        raise ValueError("Concrete evidence references are required")
    return list(dict.fromkeys(values))


class EvolutionService:
    """Return new candidate snapshots; caller persists them transactionally.

    Only a trusted host evaluator may supply ``evaluation``. The host must
    derive ``actor_is_admin`` from authenticated authorization, never model or
    request-body claims. A transition records a decision; it does not run or
    deploy candidate code. Frozen revisions should be immutable content IDs.
    """

    def __init__(self, *, min_cases: int = 3, min_improvement: float = 0.01) -> None:
        if (
            type(min_cases) is not int
            or min_cases < 1
            or not math.isfinite(min_improvement)
            or min_improvement < 0
        ):
            raise ValueError("Invalid evolution gate policy")
        self.min_cases = min_cases
        self.min_improvement = min_improvement

    def propose(
        self,
        *,
        tenant_id: str,
        task_id: str,
        baseline_revision: str,
        benchmark_revision: str,
        evidence_refs: Sequence[str],
        proposal: Mapping[str, Any],
    ) -> dict[str, Any]:
        _required(proposal.get("summary", ""), "proposal summary")
        _required(proposal.get("hypothesis", ""), "proposal hypothesis")
        # RNA's existing-skill proposal is allowed; grants, tool assignment and
        # evaluation-standard changes require an independent reviewed release.
        forbidden = {
            "permissions",
            "grants",
            "tools",
            "toolids",
            "mcpserverids",
            "evaluationsuite",
            "policy",
            "assignments",
            "capabilities",
            "capabilityids",
            "riskfloor",
        }

        def check_authority(item: object) -> None:
            if isinstance(item, Mapping):
                for key, child in item.items():
                    normalized = str(key).replace("_", "").replace("-", "").lower()
                    if normalized in forbidden:
                        raise ValueError(
                            "Evolution proposals cannot expand authority or change the benchmark"
                        )
                    check_authority(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    check_authority(child)

        check_authority(proposal)
        value: dict[str, Any] = {
            "id": uuid4().hex,
            "tenant_id": _required(tenant_id, "tenant_id"),
            "task_id": _required(task_id, "task_id"),
            "baseline_revision": _required(baseline_revision, "baseline_revision"),
            "benchmark_revision": _required(benchmark_revision, "benchmark_revision"),
            "evidence_refs": _references(evidence_refs),
            "proposal": copy.deepcopy(dict(proposal)),
            "status": "proposed",
            "created_at": _timestamp(),
            "history": [],
            "evaluation": None,
        }
        value["candidate_digest"] = _digest({key: value[key] for key in _FROZEN_FIELDS})
        return value

    @staticmethod
    def _validate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(candidate))
        if not all(key in value for key in _FROZEN_FIELDS):
            raise ValueError("Invalid candidate snapshot")
        if value.get("candidate_digest") != _digest({key: value[key] for key in _FROZEN_FIELDS}):
            raise ValueError("Candidate frozen content has changed")
        return value

    def assess(self, candidate: Mapping[str, Any], evaluation: Mapping[str, Any]) -> dict[str, Any]:
        """Compare the same explicit cases; any hard-gate failure is NO_GO.

        Each pair has case_id and baseline/candidate objects containing score,
        passed, safety_passed and evidence_refs. Repeats must not be disguised
        as independent case IDs. Aggregates never overwrite source verdicts.
        """
        value = self._validate(candidate)
        if value.get("status") != "proposed":
            raise ValueError("Only a proposed candidate can be evaluated")
        for key in ("baseline_revision", "benchmark_revision", "candidate_digest"):
            if evaluation.get(key) != value[key]:
                raise ValueError(f"Evaluation does not match frozen {key}")
        pairs = evaluation.get("pairs")
        if not isinstance(pairs, list):
            raise ValueError("Paired case evaluation is required")
        baseline_scores: list[float] = []
        candidate_scores: list[float] = []
        seen: set[str] = set()
        reasons: list[str] = []
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("Invalid evaluation pair")
            case_id = pair.get("case_id")
            if not isinstance(case_id, str) or not case_id or case_id in seen:
                raise ValueError("Evaluation cases must have distinct IDs")
            seen.add(case_id)
            scores: list[float] = []
            for side in ("baseline", "candidate"):
                result = pair.get(side)
                if not isinstance(result, dict):
                    raise ValueError("Both baseline and candidate results are required")
                score = result.get("score")
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not 0 <= score <= 1
                    or not math.isfinite(score)
                ):
                    raise ValueError("Recorded score in [0, 1] is required")
                if not isinstance(result.get("passed"), bool) or not isinstance(
                    result.get("safety_passed"), bool
                ):
                    raise ValueError("Explicit pass and safety verdicts are required")
                _references(result.get("evidence_refs", []))
                if result["safety_passed"] is not True:
                    reasons.append(f"{side}_safety_failed:{case_id}")
                scores.append(float(score))
            baseline_scores.append(scores[0])
            candidate_scores.append(scores[1])
            if scores[1] < scores[0] or (
                pair["baseline"]["passed"] and not pair["candidate"]["passed"]
            ):
                reasons.append(f"case_regression:{case_id}")
        gates = evaluation.get("hard_gates")
        for name in _HARD_GATES:
            if not isinstance(gates, dict) or gates.get(name) is not True:
                reasons.append(f"hard_gate_failed:{name}")
        _references(evaluation.get("evidence_refs", []))
        if len(seen) < self.min_cases:
            reasons.append("insufficient_independent_cases")
        improvement = (sum(candidate_scores) - sum(baseline_scores)) / len(seen) if seen else None
        if improvement is None or improvement + 1e-12 < self.min_improvement:
            reasons.append("insufficient_improvement")
        report = {
            "source": copy.deepcopy(dict(evaluation)),
            "case_count": len(seen),
            "mean_improvement": improvement,
            "gate": "NO_GO" if reasons else "GO",
            "reasons": reasons,
            "policy": {"min_cases": self.min_cases, "min_improvement": self.min_improvement},
        }
        value["evaluation"] = report
        value["evaluation_digest"] = _digest(report)
        value["status"] = "rejected" if reasons else "evaluated"
        value["history"].append(
            {
                "to": value["status"],
                "at": _timestamp(),
                "evidence_refs": list(evaluation["evidence_refs"]),
            }
        )
        return value

    def transition(
        self,
        candidate: Mapping[str, Any],
        target: str,
        *,
        active_baseline_revision: str,
        actor_is_admin: bool = False,
        evidence_refs: Sequence[str] = (),
        canary_readiness: Mapping[str, bool] | None = None,
    ) -> dict[str, Any]:
        value = self._validate(candidate)
        status = value.get("status")
        allowed = {
            "evaluated": {"shadow"},
            "shadow": {"canary"},
            "canary": {"promoted", "rolled_back"},
            "promoted": {"rolled_back"},
        }
        if target not in allowed.get(str(status), set()):
            raise ValueError(
                "Evolution must follow evaluated → shadow → canary → promoted → rolled_back"
            )
        expected_active = (
            value["candidate_digest"] if status == "promoted" else value["baseline_revision"]
        )
        if active_baseline_revision != expected_active:
            raise ValueError("Active behavior changed since the frozen evaluation")
        report = value.get("evaluation")
        if (
            not isinstance(report, dict)
            or report.get("gate") != "GO"
            or value.get("evaluation_digest") != _digest(report)
        ):
            raise ValueError("Matching successful evaluation evidence is required")
        if target in {"canary", "promoted", "rolled_back"} and actor_is_admin is not True:
            raise PermissionError("Explicit tenant administrator authorization is required")
        refs = _references(evidence_refs)
        if target == "canary" and any(
            (canary_readiness or {}).get(name) is not True for name in _CANARY_GATES
        ):
            raise ValueError(
                "Canary requires authorization, idempotency, readback, protection and recovery gates"
            )
        value["status"] = target
        value["history"].append(
            {"from": status, "to": target, "at": _timestamp(), "evidence_refs": refs}
        )
        if target == "canary":
            value["canary_readiness"] = dict(canary_readiness or {})
        if target == "promoted":
            value["active_revision"] = value["candidate_digest"]
        elif target == "rolled_back":
            value["active_revision"] = value["baseline_revision"]
        return value


class RnaSdkAdapter:
    """Bounded Node bridge to the real local ``@rna-agent/sdk`` package.

    ``sdk_root`` and ``node_binary`` are server configuration, not request
    parameters. Credentials are read only from the explicitly named environment
    variable; they are never part of the input JSON or returned diagnostics.
    The bridge invokes proposeEvolution, which can only propose updates to
    already-assigned skills. Evaluation and publication remain host decisions.
    """

    def __init__(self, sdk_root: Path, *, node_binary: str = "node") -> None:
        self.sdk_root = sdk_root.resolve()
        self.node_binary = node_binary

    async def propose(
        self,
        *,
        project: Mapping[str, Any],
        baseline: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        model: Mapping[str, Any],
        api_key_env: str,
        timeout_seconds: float = 120,
        authorization_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError("RNA timeout must be between 0 and 600 seconds")
        if not api_key_env or not api_key_env.replace("_", "a").isalnum():
            raise ValueError("An explicit credential environment variable name is required")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return {
                "source": "rna_dev",
                "status": "blocked",
                "reason": "configured_model_credentials_required",
            }
        module_path = self.sdk_root / "src" / "evolution.mjs"
        if not module_path.is_file():
            return {"source": "rna_dev", "status": "blocked", "reason": "rna_sdk_unavailable"}
        request = {
            "project": dict(project),
            "baseline": dict(baseline),
            "evidence": list(evidence),
            "model": dict(model),
        }
        # Credentials must never be supplied embedded in model/config payloads.
        forbidden = {"apikey", "api_key", "secret", "authorization", "credential", "credentials"}

        def check_fields(value: object) -> None:
            if isinstance(value, dict):
                if any(str(key).lower() in forbidden for key in value):
                    raise ValueError("RNA credentials must use the environment reference")
                for item in value.values():
                    check_fields(item)
            elif isinstance(value, list):
                for item in value:
                    check_fields(item)

        check_fields(request)
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if api_key.encode("utf-8") in encoded:
            raise ValueError("RNA credentials cannot be included in proposal inputs")
        if len(encoded) > 512 * 1024:
            raise ValueError("RNA proposal input exceeds 512 KiB")
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"}
        }
        env["RNA_ADAPTER_API_KEY"] = api_key
        if authorization_check is not None:
            authorization_check()
        try:
            process = await asyncio.create_subprocess_exec(
                self.node_binary,
                str(Path(__file__).with_name("rna_bridge.mjs")),
                str(self.sdk_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.sdk_root,
                start_new_session=os.name != "nt",
            )
        except OSError:
            return {"source": "rna_dev", "status": "blocked", "reason": "node_runtime_unavailable"}
        try:
            # Process creation yields. Recheck immediately before sending any
            # baseline/evidence to the child, which triggers the provider call.
            if authorization_check is not None:
                authorization_check()
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(encoded), timeout=timeout_seconds
            )
        except (Exception, asyncio.CancelledError):
            if process.returncode is None:
                if os.name == "nt":
                    # Windows Node version-manager shims spawn a second Node
                    # process. Killing only the shim leaves paid work running
                    # and inherited pipes open; terminate this process tree.
                    terminator = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(terminator.wait(), timeout=5)
                else:
                    # Attributes are unavailable in Windows type stubs.
                    kill_group = getattr(os, "killpg", os.kill)
                    kill_group(process.pid, getattr(signal, "SIGKILL", 9))
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
            await process.communicate()
            raise
        if process.returncode != 0:
            raise RuntimeError(
                "RNA SDK proposal failed; provider diagnostics are withheld to protect credentials"
            )
        if len(stdout) > 512 * 1024:
            raise ValueError("RNA proposal output exceeds 512 KiB")
        result = json.loads(stdout)
        if not isinstance(result, dict) or result.get("status") not in {"proposed", "blocked"}:
            raise ValueError("RNA SDK returned an invalid proposal envelope")
        if api_key in stdout.decode("utf-8"):
            raise ValueError("RNA SDK output contained credential material")
        result["source"] = "rna_dev"
        result["baseline_digest"] = _digest(dict(baseline))
        return result
