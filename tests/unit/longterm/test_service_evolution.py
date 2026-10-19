"""Authenticated platform integration for trusted evaluation and inert RNA proposals."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.longterm import digest
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.longterm.adapters import tenant_namespace
from octop.infra.longterm.evolution import EvolutionService, RnaSdkAdapter
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def platform(tmp_path: Path) -> Iterator[tuple[LongtermService, User, dict[str, Any]]]:
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    database = SqlitePool(paths.db)
    run_migrations(database)
    shared = build_shared_services(db=database, paths=paths, config=OctopConfig())
    user_id = shared.user_repo.create(username="owner", role="user")
    owner = User(user_id, "owner", "user", None)
    service = LongtermService(shared)
    tenant = service.create_tenant(owner, "Tenant A")
    space = service.create_space(tenant["tenant_id"], owner, "Private A")
    task = service.create_task(
        tenant["tenant_id"],
        owner,
        {
            "space_id": space["space_id"],
            "goal": "Verify output",
            "workflow": {"nodes": [{"id": "read", "skill": "local.echo", "input": {"value": 1}}]},
        },
    )
    yield service, owner, task
    database.close()


async def _propose(service: LongtermService, owner: User, task: dict[str, Any]) -> dict[str, Any]:
    return await service.evolve(
        task["tenant_id"],
        task["task_id"],
        owner,
        {
            "expected_revision": task["revision"],
            "action": "propose",
            "benchmark_revision": "fixed-cases-v1",
            "proposal": {
                "summary": "更早核实",
                "hypothesis": "提前检查输入可减少遗漏",
                "strategy": "先检查必要输入",
            },
        },
    )


def _report(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: candidate[key]
            for key in ("baseline_revision", "benchmark_revision", "candidate_digest")
        },
        "pairs": [
            {
                "case_id": f"independent-{i}",
                "baseline": {
                    "score": 0.5,
                    "passed": False,
                    "safety_passed": True,
                    "evidence_refs": [f"test-baseline:{i}"],
                },
                "candidate": {
                    "score": 1,
                    "passed": True,
                    "safety_passed": True,
                    "evidence_refs": [f"test-candidate:{i}"],
                },
            }
            for i in range(3)
        ],
        "hard_gates": dict.fromkeys(
            ("tenant_isolation", "authorization", "external_side_effects"), True
        ),
        "evidence_refs": ["test-report:complete"],
    }


@pytest.mark.asyncio
async def test_candidate_and_trusted_report_are_persisted_with_valid_evidence(
    platform: tuple[LongtermService, User, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, owner, task = platform
    proposed = await _propose(service, owner, task)
    candidate = proposed["record"]
    assert candidate["status"] == "proposed"
    assert candidate["tenant_id"] == task["tenant_id"]
    assert candidate["baseline_revision"] == task["plan_digest"]
    root = tmp_path / "trusted-reports"
    directory = root / tenant_namespace(task["tenant_id"]) / task["space_id"]
    directory.mkdir(parents=True)
    (directory / (candidate["id"] + ".json")).write_text(
        json.dumps(_report(candidate)), encoding="utf-8"
    )
    monkeypatch.setenv("OCTOP_EVOLUTION_REPORTS_ROOT", str(root))
    main_thread = threading.get_ident()
    evaluation_threads: list[int] = []
    assess = EvolutionService.assess

    def capture_assessment(self: EvolutionService, *args: Any) -> dict[str, Any]:
        evaluation_threads.append(threading.get_ident())
        return assess(self, *args)

    monkeypatch.setattr(EvolutionService, "assess", capture_assessment)
    result = await service.evolve(
        task["tenant_id"],
        task["task_id"],
        owner,
        {
            "expected_revision": proposed["task"]["revision"],
            "action": "assess",
            "candidate_id": candidate["id"],
        },
    )
    assert result["record"]["status"] == "evaluated"
    persisted = service.task(task["tenant_id"], task["task_id"], owner)
    assert persisted["artifacts"]["evolution"][-1]["evaluation"]["gate"] == "GO"
    assert "active_behavior_revision" not in persisted
    assert service.evidence(task["tenant_id"], task["task_id"], owner)["valid"] is True
    assert evaluation_threads and all(thread != main_thread for thread in evaluation_threads)


@pytest.mark.asyncio
async def test_global_admin_without_membership_cannot_propose(
    platform: tuple[LongtermService, User, dict[str, Any]],
) -> None:
    service, _owner, task = platform
    outsider_id = service.services.user_repo.create(username="global-admin", role="admin")
    outsider = User(outsider_id, "global-admin", "admin", None)
    with pytest.raises(OctopError):
        await _propose(service, outsider, task)


@pytest.mark.asyncio
async def test_candidate_id_does_not_open_another_users_private_space(
    platform: tuple[LongtermService, User, dict[str, Any]],
) -> None:
    service, owner, task = platform
    proposed = await _propose(service, owner, task)
    other_id = service.services.user_repo.create(username="tenant-admin", role="user")
    other = User(other_id, "tenant-admin", "user", None)
    service.set_member(task["tenant_id"], owner, other_id, "admin")
    with pytest.raises(OctopError):
        await service.evolve(
            task["tenant_id"],
            task["task_id"],
            other,
            {
                "expected_revision": proposed["task"]["revision"],
                "action": "assess",
                "candidate_id": proposed["record"]["id"],
            },
        )


@pytest.mark.asyncio
async def test_benchmark_success_cannot_qualify_a_memory(
    platform: tuple[LongtermService, User, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, owner, task = platform
    task["artifacts"] = {
        "evaluation": [{"source": "CommerceAgentBench", "goal_outcome": "achieved", "score": 1}]
    }
    service.repo.command(
        task,
        expected_revision=task["revision"],
        command_key="test:benchmark",
        request_digest="synthetic-digest",
        command="test",
    )
    monkeypatch.setenv("OCTOP_HINDSIGHT_URL", "http://127.0.0.1:1")
    with pytest.raises(OctopError) as caught:
        await service.memory(
            task["tenant_id"],
            task["task_id"],
            owner,
            {
                "action": "retain",
                "consent": True,
                "content": "This must not become successful business memory",
                "ttl_seconds": 60,
            },
        )
    assert caught.value.details["reason"] == "verified_readback_required_for_memory"


@pytest.mark.asyncio
async def test_rna_proposal_wrapper_maps_to_inert_platform_candidate(
    platform: tuple[LongtermService, User, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, owner, task = platform
    captured: list[dict[str, Any]] = []
    proposal = {
        "summary": "改进来源核实",
        "hypothesis": "先核对来源可减少遗漏",
        "strategy": "先核对来源",
        "skillUpdates": [],
        "expectedImprovement": "减少遗漏",
        "riskNotes": ["待评测"],
    }

    async def sdk_propose(self: RnaSdkAdapter, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {
            "source": "rna_dev",
            "status": "proposed",
            "proposal": proposal,
            "usageReported": True,
            "usage": {"totalTokens": 80},
            "baseline_digest": digest(kwargs["baseline"]),
            "sdkVersion": "0.59.0",
        }

    monkeypatch.setattr(RnaSdkAdapter, "propose", sdk_propose)
    monkeypatch.setenv("OCTOP_RNA_SDK_ROOT", "test-sdk-root")
    monkeypatch.setenv("OCTOP_RNA_MODEL_JSON", '{"providerId":"test","modelId":"test"}')
    monkeypatch.setenv("OCTOP_RNA_API_KEY_ENV", "OCTOP_SYNTHETIC_RNA_KEY")
    baseline = {
        "strategy": "verify",
        "skills": [],
        "assignments": {"skillIds": [], "toolIds": [], "mcpServerIds": []},
    }
    result = await service.evolve(
        task["tenant_id"],
        task["task_id"],
        owner,
        {
            "expected_revision": task["revision"],
            "action": "rna_propose",
            "benchmark_revision": "cases-v1",
            "proposal": {"baseline": baseline},
        },
    )
    assert result["record"]["proposal"] == proposal
    assert result["record"]["status"] == "proposed"
    generation = result["record"]["generation"]
    assert generation == {
        "source": "rna_dev",
        "baseline": baseline,
        "baseline_digest": digest(baseline),
        "baseline_revision": task["plan_digest"],
        "sdkVersion": "0.59.0",
        "usageReported": True,
        "usage": {"totalTokens": 80},
    }
    assert "octop-rna-generation:" + digest(generation) in result["record"]["evidence_refs"]
    assert captured[0]["baseline"] == baseline
    assert (
        captured[0]["project"]["id"] == tenant_namespace(task["tenant_id"]) + "-" + task["space_id"]
    )
    assert captured[0]["evidence"][0]["plan_digest"] == task["plan_digest"]
    assert "active_behavior_revision" not in result["task"]

    # Even dropping the extra audit object must not bypass its frozen reference.
    persisted = result["task"]
    persisted["artifacts"]["evolution"][-1].pop("generation")
    changed = service.repo.command(
        persisted,
        expected_revision=persisted["revision"],
        command_key="test:alter-generation",
        request_digest="synthetic-digest",
        command="test",
    )
    with pytest.raises(OctopError) as caught:
        await service.evolve(
            task["tenant_id"],
            task["task_id"],
            owner,
            {
                "expected_revision": changed["revision"],
                "action": "assess",
                "candidate_id": result["record"]["id"],
            },
        )
    assert caught.value.details["reason"] == "rna_generation_audit_changed"


@pytest.mark.asyncio
async def test_rna_cannot_return_a_different_baseline_digest(
    platform: tuple[LongtermService, User, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, owner, task = platform

    async def sdk_propose(self: RnaSdkAdapter, **kwargs: Any) -> dict[str, Any]:
        return {"status": "proposed", "baseline_digest": "different-input"}

    monkeypatch.setattr(RnaSdkAdapter, "propose", sdk_propose)
    monkeypatch.setenv("OCTOP_RNA_SDK_ROOT", "test-sdk-root")
    monkeypatch.setenv("OCTOP_RNA_MODEL_JSON", '{"providerId":"test","modelId":"test"}')
    monkeypatch.setenv("OCTOP_RNA_API_KEY_ENV", "OCTOP_SYNTHETIC_RNA_KEY")
    with pytest.raises(OctopError) as caught:
        await service.evolve(
            task["tenant_id"],
            task["task_id"],
            owner,
            {
                "expected_revision": task["revision"],
                "action": "rna_propose",
                "benchmark_revision": "cases-v1",
                "proposal": {"baseline": {}},
            },
        )
    assert caught.value.details["reason"] == "rna_baseline_digest_mismatch"


@pytest.mark.asyncio
async def test_next_candidate_freezes_the_current_active_behavior(
    platform: tuple[LongtermService, User, dict[str, Any]],
) -> None:
    service, owner, task = platform
    task["active_behavior_revision"] = "promoted-behavior-digest"
    updated = service.repo.command(
        task,
        expected_revision=task["revision"],
        command_key="test:active-behavior",
        request_digest="synthetic-digest",
        command="test",
    )
    proposed = await _propose(service, owner, updated)
    assert proposed["record"]["baseline_revision"] == "promoted-behavior-digest"
    assert proposed["record"]["baseline_revision"] != task["plan_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_report", [[], {"candidate_digest": "another-candidate"}, "oversize"]
)
async def test_trusted_report_must_be_bounded_and_match_the_frozen_candidate(
    platform: tuple[LongtermService, User, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_report: Any,
) -> None:
    service, owner, task = platform
    proposed = await _propose(service, owner, task)
    root = tmp_path / "reports"
    directory = root / tenant_namespace(task["tenant_id"]) / task["space_id"]
    directory.mkdir(parents=True)
    content = " " * 524289 if invalid_report == "oversize" else json.dumps(invalid_report)
    (directory / (proposed["record"]["id"] + ".json")).write_text(content, encoding="utf-8")
    monkeypatch.setenv("OCTOP_EVOLUTION_REPORTS_ROOT", str(root))
    with pytest.raises(OctopError) as caught:
        await service.evolve(
            task["tenant_id"],
            task["task_id"],
            owner,
            {
                "expected_revision": proposed["task"]["revision"],
                "action": "assess",
                "candidate_id": proposed["record"]["id"],
            },
        )
    assert caught.value.details["reason"] == "evolution_input_or_trusted_report_invalid"
