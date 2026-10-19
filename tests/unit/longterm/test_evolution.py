"""Frozen paired evaluations and explicit publication authority."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from octop.infra.longterm.evolution import EvolutionService


@pytest.fixture
def service() -> EvolutionService:
    return EvolutionService()


@pytest.fixture
def candidate(service: EvolutionService) -> dict[str, Any]:
    return service.propose(
        tenant_id="tenant-a",
        task_id="goal-1",
        baseline_revision="baseline-sha256",
        benchmark_revision="cases-sha256",
        evidence_refs=["evidence:failed-run"],
        proposal={
            "summary": "改进规划",
            "hypothesis": "更早检查必需输入可以减少失败",
            "strategy": "先核实再执行",
            "skillUpdates": [],
        },
    )


def _evaluation(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: candidate[key]
            for key in ("baseline_revision", "benchmark_revision", "candidate_digest")
        },
        "pairs": [
            {
                "case_id": f"case-{i}",
                "baseline": {
                    "score": 0.5,
                    "passed": False,
                    "safety_passed": True,
                    "evidence_refs": [f"baseline:{i}"],
                },
                "candidate": {
                    "score": 1,
                    "passed": True,
                    "safety_passed": True,
                    "evidence_refs": [f"candidate:{i}"],
                },
            }
            for i in range(3)
        ],
        "hard_gates": {
            "tenant_isolation": True,
            "authorization": True,
            "external_side_effects": True,
        },
        "evidence_refs": ["report:fixed-evaluation"],
    }


def test_assessment_is_paired_immutable_and_does_not_publish(
    service: EvolutionService, candidate: dict[str, Any]
) -> None:
    result = service.assess(candidate, _evaluation(candidate))
    assert result["status"] == "evaluated"
    assert result["evaluation"]["gate"] == "GO"
    assert result["evaluation"]["case_count"] == 3
    assert result["evaluation"]["mean_improvement"] == 0.5
    assert candidate["status"] == "proposed"
    assert "active_revision" not in result


@pytest.mark.parametrize("gate", ["tenant_isolation", "authorization", "external_side_effects"])
def test_quality_cannot_offset_hard_gate_failure(
    service: EvolutionService, candidate: dict[str, Any], gate: str
) -> None:
    evaluation = _evaluation(candidate)
    evaluation["hard_gates"][gate] = False
    result = service.assess(candidate, evaluation)
    assert result["status"] == "rejected"
    assert f"hard_gate_failed:{gate}" in result["evaluation"]["reasons"]
    with pytest.raises(ValueError):
        service.transition(
            result, "shadow", active_baseline_revision="baseline-sha256", evidence_refs=["shadow:1"]
        )


def test_insufficient_cases_and_case_regression_block_release(
    service: EvolutionService, candidate: dict[str, Any]
) -> None:
    evaluation = _evaluation(candidate)
    evaluation["pairs"] = evaluation["pairs"][:2]
    evaluation["pairs"][0]["candidate"]["score"] = 0.1
    result = service.assess(candidate, evaluation)
    assert "insufficient_independent_cases" in result["evaluation"]["reasons"]
    assert "case_regression:case-0" in result["evaluation"]["reasons"]


@pytest.mark.parametrize("field", ["baseline_revision", "benchmark_revision", "candidate_digest"])
def test_comparison_cannot_change_frozen_version(
    service: EvolutionService, candidate: dict[str, Any], field: str
) -> None:
    evaluation = _evaluation(candidate)
    evaluation[field] = "changed"
    with pytest.raises(ValueError, match="frozen"):
        service.assess(candidate, evaluation)


def test_duplicate_cases_cannot_inflate_sample_count(
    service: EvolutionService, candidate: dict[str, Any]
) -> None:
    evaluation = _evaluation(candidate)
    evaluation["pairs"][1]["case_id"] = "case-0"
    with pytest.raises(ValueError, match="distinct IDs"):
        service.assess(candidate, evaluation)


def test_candidate_mutation_and_missing_evidence_are_rejected(
    service: EvolutionService, candidate: dict[str, Any]
) -> None:
    altered = copy.deepcopy(candidate)
    altered["proposal"]["strategy"] = "changed after evaluation"
    with pytest.raises(ValueError, match="frozen content"):
        service.assess(altered, _evaluation(candidate))
    evaluation = _evaluation(candidate)
    evaluation["pairs"][0]["candidate"]["evidence_refs"] = []
    with pytest.raises(ValueError, match="evidence"):
        service.assess(candidate, evaluation)


def test_lifecycle_requires_canary_gates_admin_and_fresh_baseline(
    service: EvolutionService, candidate: dict[str, Any]
) -> None:
    evaluated = service.assess(candidate, _evaluation(candidate))
    shadow = service.transition(
        evaluated,
        "shadow",
        active_baseline_revision="baseline-sha256",
        evidence_refs=["shadow:report"],
    )
    with pytest.raises(PermissionError):
        service.transition(
            shadow,
            "canary",
            active_baseline_revision="baseline-sha256",
            evidence_refs=["approval:1"],
        )
    with pytest.raises(ValueError, match="Canary requires"):
        service.transition(
            shadow,
            "canary",
            active_baseline_revision="baseline-sha256",
            actor_is_admin=True,
            evidence_refs=["approval:1"],
        )
    ready = dict.fromkeys(
        ("authorization", "idempotency", "readback", "protection_state", "recovery"), True
    )
    canary = service.transition(
        shadow,
        "canary",
        active_baseline_revision="baseline-sha256",
        actor_is_admin=True,
        evidence_refs=["approval:1"],
        canary_readiness=ready,
    )
    with pytest.raises(ValueError, match="Active behavior changed"):
        service.transition(
            canary,
            "promoted",
            active_baseline_revision="other-version",
            actor_is_admin=True,
            evidence_refs=["canary:report"],
        )
    promoted = service.transition(
        canary,
        "promoted",
        active_baseline_revision="baseline-sha256",
        actor_is_admin=True,
        evidence_refs=["canary:report"],
    )
    assert promoted["active_revision"] == candidate["candidate_digest"]
    rolled_back = service.transition(
        promoted,
        "rolled_back",
        active_baseline_revision=candidate["candidate_digest"],
        actor_is_admin=True,
        evidence_refs=["incident:report"],
    )
    assert rolled_back["active_revision"] == "baseline-sha256"
    assert len(rolled_back["history"]) == 5
    assert candidate["status"] == "proposed"


def test_canary_can_be_rolled_back_without_full_promotion(
    service: EvolutionService, candidate: dict[str, Any]
) -> None:
    evaluated = service.assess(candidate, _evaluation(candidate))
    shadow = service.transition(
        evaluated, "shadow", active_baseline_revision="baseline-sha256", evidence_refs=["shadow:1"]
    )
    ready = dict.fromkeys(
        ("authorization", "idempotency", "readback", "protection_state", "recovery"), True
    )
    canary = service.transition(
        shadow,
        "canary",
        active_baseline_revision="baseline-sha256",
        actor_is_admin=True,
        evidence_refs=["approval:1"],
        canary_readiness=ready,
    )
    result = service.transition(
        canary,
        "rolled_back",
        active_baseline_revision="baseline-sha256",
        actor_is_admin=True,
        evidence_refs=["canary:failure"],
    )
    assert result["active_revision"] == "baseline-sha256"


def test_evaluation_tampering_blocks_shadow(
    service: EvolutionService, candidate: dict[str, Any]
) -> None:
    evaluated = service.assess(candidate, _evaluation(candidate))
    evaluated["evaluation"]["case_count"] = 100
    with pytest.raises(ValueError, match="Matching successful"):
        service.transition(
            evaluated,
            "shadow",
            active_baseline_revision="baseline-sha256",
            evidence_refs=["shadow:1"],
        )


@pytest.mark.parametrize("field", ["permissions", "grants", "tools", "evaluationSuite", "policy"])
def test_candidate_cannot_grant_capabilities(service: EvolutionService, field: str) -> None:
    with pytest.raises(ValueError, match="expand authority"):
        service.propose(
            tenant_id="a",
            task_id="goal",
            baseline_revision="b",
            benchmark_revision="d",
            evidence_refs=["e:1"],
            proposal={"summary": "a", "hypothesis": "b", field: {}},
        )


def test_nested_and_alternate_spelling_cannot_expand_authority(service: EvolutionService) -> None:
    with pytest.raises(ValueError, match="expand authority"):
        service.propose(
            tenant_id="a",
            task_id="goal",
            baseline_revision="b",
            benchmark_revision="d",
            evidence_refs=["e:1"],
            proposal={
                "summary": "a",
                "hypothesis": "b",
                "genome": {"unrelated": [{"tool_ids": ["write"]}]},
            },
        )
