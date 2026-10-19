"""Revocation fences and durable integration facts across real asynchronous waits."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.longterm import AuthorityRevoked, canonical, digest
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.longterm.adapters import HindsightAdapter, SemanticaAdapter, tenant_namespace
from octop.infra.longterm.evolution import EvolutionService, RnaSdkAdapter
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout


@dataclass
class AuthorityPlane:
    service: LongtermService
    owner: User
    actor: User
    task: dict[str, Any]


@pytest.fixture
def authority_plane(tmp_path: Path) -> Iterator[AuthorityPlane]:
    paths = PathLayout(tmp_path / "authority")
    paths.ensure_root()
    database = SqlitePool(paths.db)
    run_migrations(database)
    shared = build_shared_services(db=database, paths=paths, config=OctopConfig())
    owner = User(shared.user_repo.create(username="owner", role="user"), "owner", "user", None)
    actor = User(shared.user_repo.create(username="actor", role="user"), "actor", "user", None)
    service = LongtermService(shared)
    tenant_id = service.create_tenant(owner, "Authority tenant")["tenant_id"]
    service.set_member(tenant_id, owner, actor.id, "admin")
    space_id = service.create_space(tenant_id, actor, "Private actor space")["space_id"]
    task = service.create_task(
        tenant_id,
        actor,
        {
            "space_id": space_id,
            "goal": "review",
            "workflow": {"nodes": [{"id": "read", "skill": "local.echo"}]},
        },
    )
    yield AuthorityPlane(service, owner, actor, task)
    database.close()


def _role(plane: AuthorityPlane, role: str | None) -> None:
    plane.service.set_member(plane.task["tenant_id"], plane.owner, plane.actor.id, role)


@pytest.mark.parametrize("role", [None, "viewer", "member"])
async def test_rna_wait_cannot_commit_after_revocation_or_admin_demotion(
    authority_plane: AuthorityPlane, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    plane = authority_plane
    entered, release = asyncio.Event(), asyncio.Event()

    async def propose(self: RnaSdkAdapter, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {
            "status": "proposed",
            "baseline_digest": digest(kwargs["baseline"]),
            "proposal": {"summary": "review", "hypothesis": "review"},
        }

    monkeypatch.setattr(RnaSdkAdapter, "propose", propose)
    monkeypatch.setenv("OCTOP_RNA_SDK_ROOT", "test-sdk")
    monkeypatch.setenv("OCTOP_RNA_MODEL_JSON", "{}")
    monkeypatch.setenv("OCTOP_RNA_API_KEY_ENV", "SYNTHETIC_TEST_KEY")
    pending = asyncio.create_task(
        plane.service.evolve(
            plane.task["tenant_id"],
            plane.task["task_id"],
            plane.actor,
            {
                "expected_revision": 1,
                "action": "rna_propose",
                "proposal": {"baseline": {}},
                "benchmark_revision": "fixed-cases",
            },
        )
    )
    await entered.wait()
    _role(plane, role)
    release.set()
    with pytest.raises(OctopError) as caught:
        await pending
    assert caught.value.status == 403
    saved = plane.service.repo.task(plane.task["tenant_id"], plane.task["task_id"])
    assert saved is not None and saved["revision"] == 1 and saved["artifacts"] == {}


async def test_assessment_executor_cannot_commit_after_admin_demotion(
    authority_plane: AuthorityPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = authority_plane
    proposed = await plane.service.evolve(
        plane.task["tenant_id"],
        plane.task["task_id"],
        plane.actor,
        {
            "expected_revision": 1,
            "action": "propose",
            "proposal": {"summary": "review", "hypothesis": "review"},
            "benchmark_revision": "fixed-cases",
        },
    )
    candidate = proposed["record"]
    root = tmp_path / "reports"
    directory = root / tenant_namespace(plane.task["tenant_id"]) / plane.task["space_id"]
    directory.mkdir(parents=True)
    (directory / (candidate["id"] + ".json")).write_text(
        canonical(
            {
                key: candidate[key]
                for key in ("baseline_revision", "benchmark_revision", "candidate_digest")
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OCTOP_EVOLUTION_REPORTS_ROOT", str(root))
    entered, release = threading.Event(), threading.Event()

    def assess(
        self: EvolutionService, current: dict[str, Any], report: dict[str, Any]
    ) -> dict[str, Any]:
        entered.set()
        assert release.wait(5)
        return {**current, "status": "evaluated"}

    monkeypatch.setattr(EvolutionService, "assess", assess)
    pending = asyncio.create_task(
        plane.service.evolve(
            plane.task["tenant_id"],
            plane.task["task_id"],
            plane.actor,
            {
                "expected_revision": proposed["task"]["revision"],
                "action": "assess",
                "candidate_id": candidate["id"],
            },
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    _role(plane, "member")
    release.set()
    with pytest.raises(OctopError) as caught:
        await pending
    assert caught.value.status == 403
    saved = plane.service.repo.task(plane.task["tenant_id"], plane.task["task_id"])
    assert saved is not None and saved["revision"] == proposed["task"]["revision"]
    assert saved["artifacts"]["evolution"][-1]["status"] == "proposed"


def test_repo_actor_check_fences_membership_and_private_space_atomically(
    authority_plane: AuthorityPlane,
) -> None:
    plane = authority_plane
    arguments = {
        "expected_revision": 1,
        "command_key": "test-authority",
        "request_digest": "synthetic",
        "command": "evolution",
        "require_admin": True,
    }
    with pytest.raises(AuthorityRevoked):
        plane.service.repo.command(plane.task, actor_user_id=plane.owner.id, **arguments)
    _role(plane, "member")
    with pytest.raises(AuthorityRevoked):
        plane.service.repo.command(plane.task, actor_user_id=plane.actor.id, **arguments)
    saved = plane.service.repo.task(plane.task["tenant_id"], plane.task["task_id"])
    assert saved is not None and saved["revision"] == 1


async def test_memory_recall_is_not_disclosed_after_revocation_in_flight(
    authority_plane: AuthorityPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = authority_plane
    entered, release = asyncio.Event(), asyncio.Event()
    scope = plane.task["tenant_id"] + ":space:" + plane.task["space_id"]

    async def recall(**kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {
            "results": [
                {
                    "text": "private memory",
                    "metadata": {
                        "octop_tenant": tenant_namespace(scope),
                        "octop_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                        "octop_evidence_refs": '["e:1"]',
                    },
                }
            ]
        }

    client = SimpleNamespace(arecall=recall, aclose=AsyncMock())

    def get_client(self: HindsightAdapter) -> Any:
        self._client = client
        return client

    monkeypatch.setattr(HindsightAdapter, "_get_client", get_client)
    monkeypatch.setenv("OCTOP_HINDSIGHT_URL", "http://memory.example.test")
    pending = asyncio.create_task(
        plane.service.memory(
            plane.task["tenant_id"],
            plane.task["task_id"],
            plane.actor,
            {"action": "recall", "query": "private", "max_tokens": 128},
        )
    )
    await entered.wait()
    _role(plane, None)
    release.set()
    with pytest.raises(OctopError) as caught:
        await pending
    assert caught.value.status == 403
    events = plane.service.repo.events(plane.task["tenant_id"], plane.task["task_id"])
    assert [event["event_type"] for event in events][-2:] == [
        "memory.recall.intent",
        "memory.recall",
    ]
    assert events[-1]["payload"]["results"][0]["text"] == "private memory"
    client.aclose.assert_awaited_once()


def _verified_task(plane: AuthorityPlane) -> None:
    with plane.service.services.db.transaction() as connection:
        connection.execute(
            "UPDATE bm_tasks SET status='completed',result_json=?,checkpoint_json=? WHERE tenant_id=? AND task_id=?",
            (
                canonical({"goal_outcome": "achieved"}),
                canonical({"verification": {"evidence_refs": ["trusted-readback:1"]}}),
                plane.task["tenant_id"],
                plane.task["task_id"],
            ),
        )


@pytest.mark.parametrize("revoke_before_dispatch", [False, True])
async def test_memory_write_has_durable_intent_and_uncertain_or_blocked_audit(
    authority_plane: AuthorityPlane, monkeypatch: pytest.MonkeyPatch, revoke_before_dispatch: bool
) -> None:
    plane = authority_plane
    _verified_task(plane)

    async def retain(**kwargs: Any) -> dict[str, Any]:
        events = plane.service.repo.events(plane.task["tenant_id"], plane.task["task_id"])
        assert events[-1]["event_type"] == "memory.retain.intent"
        raise TimeoutError("response lost after the request was accepted")

    client = SimpleNamespace(aretain=AsyncMock(side_effect=retain), aclose=AsyncMock())

    def get_client(self: HindsightAdapter) -> Any:
        self._client = client
        if revoke_before_dispatch:
            _role(plane, None)
        return client

    monkeypatch.setattr(HindsightAdapter, "_get_client", get_client)
    monkeypatch.setenv("OCTOP_HINDSIGHT_URL", "http://memory.example.test")
    expected = OctopError if revoke_before_dispatch else TimeoutError
    with pytest.raises(expected):
        await plane.service.memory(
            plane.task["tenant_id"],
            plane.task["task_id"],
            plane.actor,
            {"action": "retain", "content": "verified lesson", "consent": True, "ttl_seconds": 60},
        )
    events = plane.service.repo.events(plane.task["tenant_id"], plane.task["task_id"])
    assert events[-1]["event_type"] == (
        "memory.retain.failed" if revoke_before_dispatch else "memory.retain.uncertain"
    )
    assert events[-1]["payload"]["dispatched"] is not revoke_before_dispatch
    assert events[-1]["payload"]["operation_id"] == events[-2]["payload"]["operation_id"]
    assert client.aretain.await_count == (0 if revoke_before_dispatch else 1)


async def test_provenance_revocation_stops_later_exports_and_preserves_partial_fact(
    authority_plane: AuthorityPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = authority_plane
    plane.service.repo.append(plane.task["tenant_id"], plane.task["task_id"], "extra", {})
    entered, release = asyncio.Event(), asyncio.Event()
    exported: list[int] = []

    async def record(
        self: SemanticaAdapter, event: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert self._authorization_check is not None
        self._authorization_check()
        exported.append(event["sequence"])
        entered.set()
        await release.wait()
        return {"source": "semantica", "indexed": True}

    monkeypatch.setattr(SemanticaAdapter, "record", record)
    pending = asyncio.create_task(
        plane.service.provenance(
            plane.task["tenant_id"], plane.task["task_id"], plane.actor, consent=True
        )
    )
    await entered.wait()
    _role(plane, None)
    release.set()
    with pytest.raises(OctopError) as caught:
        await pending
    assert caught.value.status == 403
    assert exported == [1]
    events = plane.service.repo.events(plane.task["tenant_id"], plane.task["task_id"])
    assert events[-1]["event_type"] == "provenance.uncertain"
    assert events[-1]["payload"]["confirmed_count"] == 1


async def test_provenance_pages_beyond_1000_and_excludes_events_after_the_frozen_cut(
    authority_plane: AuthorityPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = authority_plane
    with plane.service.repo.scoped(plane.task["tenant_id"]) as connection:
        for index in range(1005):
            plane.service.repo._append(
                connection, plane.task["tenant_id"], plane.task["task_id"], "bulk", {"index": index}
            )
    frozen = plane.service.evidence(plane.task["tenant_id"], plane.task["task_id"], plane.actor)
    assert frozen["last_sequence"] == 1006 and len(frozen["events"]) == 1000
    exported: list[int] = []

    async def record(
        self: SemanticaAdapter, event: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert self._authorization_check is not None
        self._authorization_check()
        if not exported:
            plane.service.repo.append(
                plane.task["tenant_id"], plane.task["task_id"], "concurrent.after_cut", {}
            )
        exported.append(event["sequence"])
        return {"source": "semantica", "sequence": event["sequence"], "indexed": True}

    monkeypatch.setattr(SemanticaAdapter, "record", record)
    result = await plane.service.provenance(
        plane.task["tenant_id"], plane.task["task_id"], plane.actor, consent=True
    )
    assert result["complete"] is True and result["count"] == 1006
    assert result["head_digest"] == frozen["head_digest"]
    assert exported == list(range(1, 1007))
    assert len(result["records"]) == 1006
