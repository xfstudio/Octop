"""Control-plane invariants spanning tenancy, sync, persistence and recovery."""

from __future__ import annotations

import asyncio
import json

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.longterm import AuthorityRevoked, LongtermRepo, RevisionConflict, digest
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def platform(tmp_path):
    db = SqlitePool(tmp_path / "control.db")
    run_migrations(db)
    shared = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    uid = shared.user_repo.create(username="alice", role="user")
    other = shared.user_repo.create(username="bob", role="admin")
    user = User(uid, "alice", "user", None)
    stranger = User(other, "bob", "admin", None)
    svc = LongtermService(shared)
    tenant = svc.create_tenant(user, "商家 A")["tenant_id"]
    space = svc.create_space(tenant, user, "工作配置")["space_id"]
    yield svc, user, stranger, tenant, space
    db.close()


def task_body(space):
    return {
        "space_id": space,
        "goal": "检查本机编排",
        "mode": "local",
        "idempotency_key": "create-one",
        "workflow": {
            "nodes": [
                {
                    "id": "observe",
                    "skill": "local.echo",
                    "risk": "R0",
                    "depends_on": [],
                    "input": {"message": "离线可用"},
                },
                {
                    "id": "review",
                    "skill": "local.echo",
                    "risk": "R0",
                    "depends_on": ["observe"],
                    "input": {},
                },
            ]
        },
    }


def public_config():
    return {
        "schema_version": 1,
        "workspace_name": "本机",
        "workflow": {
            "nodes": [
                {"id": "echo", "skill": "local.echo", "depends_on": [], "risk": "R0", "input": {}}
            ]
        },
        "defaults": {"mode": "local", "max_cycles": 1},
    }


def test_global_admin_has_no_implicit_tenant_membership(platform):
    svc, _, stranger, tenant, space = platform
    assert svc.tenants(stranger) == []
    with pytest.raises(OctopError) as err:
        svc.config(tenant, space, stranger)
    assert err.value.status == 403


def test_spaces_remain_private_from_other_tenant_members(platform):
    svc, user, other, tenant, space = platform
    svc.set_member(tenant, user, other.id, "admin")
    assert svc.spaces(tenant, other) == []
    with pytest.raises(OctopError) as err:
        svc.config(tenant, space, other)
    assert err.value.status == 404


def test_viewer_cannot_create_or_sync(platform):
    svc, user, other, tenant, _ = platform
    svc.set_member(tenant, user, other.id, "viewer")
    with pytest.raises(OctopError) as err:
        svc.create_space(tenant, other, "禁止")
    assert err.value.status == 403


def test_owner_cannot_be_removed_or_demoted(platform):
    svc, user, _, tenant, _ = platform
    with pytest.raises(OctopError):
        svc.set_member(tenant, user, user.id, None)
    assert svc.repo.role(tenant, user.id) == "owner"


def test_two_devices_conflict_without_overwrite(platform):
    svc, user, _, tenant, space = platform
    first = svc.save_config(
        tenant, space, user, expected_revision=0, config=public_config(), device_id="device-one"
    )
    with pytest.raises(RevisionConflict):
        svc.save_config(
            tenant,
            space,
            user,
            expected_revision=0,
            config={**public_config(), "workspace_name": "覆盖"},
            device_id="device-two",
        )
    saved = svc.config(tenant, space, user)
    assert saved["revision"] == first["revision"] == 1
    assert saved["device_id"] == "device-one"
    assert saved["config"]["workspace_name"] == "本机"


@pytest.mark.parametrize(
    "input_value",
    [
        {"nested": {"api_key": "synthetic-credential"}},
        {"cookie": "synthetic-cookie"},
        {"file": "C:\\Users\\private\\account.txt"},
        {"headers": {"Authorization": "Bearer synthetic-value"}},
        {"numeric": float("nan")},
        {"numeric": float("inf")},
    ],
)
def test_sync_rejects_nested_credentials_and_host_paths(platform, input_value):
    svc, user, _, tenant, space = platform
    config = public_config()
    config["workflow"]["nodes"][0]["input"] = input_value
    with pytest.raises(OctopError):
        svc.save_config(tenant, space, user, expected_revision=0, config=config, device_id="device")
    assert svc.config(tenant, space, user)["revision"] == 0


def test_task_creation_idempotency_is_bound_to_request(platform):
    svc, user, _, tenant, space = platform
    one = svc.create_task(tenant, user, task_body(space))
    two = svc.create_task(tenant, user, task_body(space))
    assert one == two
    changed = {**task_body(space), "goal": "不同目标"}
    with pytest.raises(RevisionConflict):
        svc.create_task(tenant, user, changed)
    assert len(svc.tasks(tenant, user)) == 1


def test_cannot_attach_another_users_agent(platform):
    svc, user, other, tenant, space = platform
    aid = svc.services.agent_repo.create(agent_id="private-agent", user_id=other.id, name="private")
    body = task_body(space)
    body["workflow"]["nodes"][0].update(agent_id=aid, skill="agent.analyze")
    with pytest.raises(OctopError):
        svc.create_task(tenant, user, body)


async def run_to_end(svc, tenant, task_id):
    for _ in range(30):
        task = svc.repo.task(tenant, task_id)
        if task["status"] not in {"queued", "running"}:
            return task
        await svc.step(tenant, task_id)
    pytest.fail("bounded workflow did not terminate")


async def test_offline_task_is_persistent_and_chain_verifies(platform, monkeypatch):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    task = svc.create_task(tenant, user, task_body(space))
    svc.command(
        tenant,
        task["task_id"],
        user,
        {"command": "start", "expected_revision": 1, "idempotency_key": "start-once"},
    )
    first = await svc.step(tenant, task["task_id"])
    assert first == "running"
    # A new control-plane instance resumes the recorded checkpoint.
    resumed = LongtermService(svc.services)
    result = await run_to_end(resumed, tenant, task["task_id"])
    assert result["status"] == "completed"
    assert (
        result["checkpoint"]["node_results"]["observe"]["result"]["output"]["message"] == "离线可用"
    )
    assert result["result"]["execution_health"] == "succeeded"
    assert result["result"]["goal_outcome"] == "inconclusive"
    chain = resumed.evidence(tenant, task["task_id"], user)
    assert chain["valid"] and len(chain["events"]) >= 10
    assert all(e["tenant_id"] == tenant for e in chain["events"])


async def test_command_retry_returns_original_admission(platform, monkeypatch):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    task = svc.create_task(tenant, user, task_body(space))
    command = {"command": "start", "expected_revision": 1, "idempotency_key": "stable"}
    accepted = svc.command(tenant, task["task_id"], user, command)
    await svc.step(tenant, task["task_id"])
    assert svc.command(tenant, task["task_id"], user, command) == accepted


async def test_recovery_blocks_an_unresolved_write(platform, monkeypatch):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    task = svc.create_task(tenant, user, task_body(space))
    svc.command(tenant, task["task_id"], user, {"command": "start", "expected_revision": 1})
    svc.repo.append(
        tenant,
        task["task_id"],
        "action_intent",
        {"node_id": "write", "risk": "R2", "idempotency_key": "write-key"},
    )
    assert await svc.step(tenant, task["task_id"]) == "uncertain"
    recovered = svc.repo.task(tenant, task["task_id"])
    assert recovered["result"]["reason"] == "write_requires_reconciliation"
    with pytest.raises(OctopError):
        svc.command(
            tenant,
            task["task_id"],
            user,
            {"command": "resume", "expected_revision": recovered["revision"]},
        )


async def test_lease_prevents_two_workers_executing_same_batch(platform, monkeypatch):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    task = svc.create_task(tenant, user, task_body(space))
    svc.command(tenant, task["task_id"], user, {"command": "start", "expected_revision": 1})
    assert svc.repo.claim(tenant, task["task_id"], "other-worker") is not None
    assert await svc.step(tenant, task["task_id"]) is None


async def test_committed_completion_survives_missing_checkpoint_without_redispatch(
    platform, monkeypatch
):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    value = svc.create_task(tenant, user, task_body(space))
    task_id = value["task_id"]
    svc.command(tenant, task_id, user, {"command": "start", "expected_revision": 1})
    for _ in range(4):
        await svc.step(tenant, task_id)
    assert svc.repo.task(tenant, task_id)["checkpoint"]["phase"] == "Act"
    original = svc.repo.checkpoint
    with monkeypatch.context() as patch:
        # Completion was committed by finish_event, then the process lost its
        # checkpoint transaction. The next worker must use the durable result.
        patch.setattr(svc.repo, "checkpoint", lambda *args, **kwargs: False)
        await svc.step(tenant, task_id)
    assert svc.repo.task(tenant, task_id)["checkpoint"]["node_results"] == {}
    assert svc.repo.checkpoint == original
    result = await run_to_end(svc, tenant, task_id)
    assert result["status"] == "completed"
    intents = [
        entry
        for entry in svc.repo.events(tenant, task_id)
        if entry["event_type"] == "action_intent"
    ]
    assert [entry["payload"]["node_id"] for entry in intents] == ["observe", "review"]
    assert result["checkpoint"]["actions"] == 2


async def test_step_returns_committed_status_after_pause_resume_race(platform, monkeypatch):
    from octop.infra.longterm.runtime import LongTermRuntime

    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    value = svc.create_task(tenant, user, task_body(space))
    task_id = value["task_id"]
    svc.command(tenant, task_id, user, {"command": "start", "expected_revision": 1})
    original = LongTermRuntime.advance
    raced = False

    async def advance(runtime, current, checkpoint=None):
        nonlocal raced
        result = await original(runtime, current, checkpoint)
        if result.status == "completed" and not raced:
            raced = True
            row = svc.repo.task(tenant, task_id)
            paused = svc.command(
                tenant, task_id, user, {"command": "pause", "expected_revision": row["revision"]}
            )
            svc.command(
                tenant,
                task_id,
                user,
                {"command": "resume", "expected_revision": paused["revision"]},
            )
        return result

    monkeypatch.setattr(LongTermRuntime, "advance", advance)
    for _ in range(10):
        status = await svc.step(tenant, task_id)
        if raced:
            break
    assert status == "queued"
    assert (await run_to_end(svc, tenant, task_id))["status"] == "completed"


async def test_api_temporal_dispatch_uses_same_tls_truthy_contract_as_worker(platform, monkeypatch):
    from octop.infra.longterm import temporal_runtime

    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    monkeypatch.setenv("OCTOP_TEMPORAL_ADDRESS", "temporal.example.test:7233")
    monkeypatch.setenv("OCTOP_TEMPORAL_TLS", "true")
    called = []

    async def connect(**kwargs):
        called.append(kwargs)
        return object()

    async def start(*args, **kwargs):
        return "workflow"

    monkeypatch.setattr(temporal_runtime, "connect", connect)
    monkeypatch.setattr(temporal_runtime, "start_task", start)
    value = svc.create_task(tenant, user, {**task_body(space), "mode": "temporal"})
    row = svc.command(tenant, value["task_id"], user, {"command": "start", "expected_revision": 1})
    await svc._dispatch_temporal(row)
    assert called[0]["tls"] is True


async def test_pause_wins_over_a_stale_worker_checkpoint(platform, monkeypatch):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    task = svc.create_task(tenant, user, task_body(space))
    task = svc.command(tenant, task["task_id"], user, {"command": "start", "expected_revision": 1})
    claimed = svc.repo.claim(tenant, task["task_id"], "worker")
    paused = svc.command(
        tenant, task["task_id"], user, {"command": "pause", "expected_revision": task["revision"]}
    )
    svc.repo.checkpoint(
        claimed, "worker", {"phase": "Act"}, status="running", result=None, events=[]
    )
    assert svc.repo.task(tenant, task["task_id"])["status"] == paused["status"] == "paused"


def test_replan_requires_a_new_frozen_task_instead_of_resuming_stale_plan(platform, monkeypatch):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    task = svc.create_task(tenant, user, task_body(space))
    svc.command(tenant, task["task_id"], user, {"command": "start", "expected_revision": 1})
    claimed = svc.repo.claim(tenant, task["task_id"], "worker")
    svc.repo.checkpoint(
        claimed, "worker", {"decision": "replan"}, status="paused", result=None, events=[]
    )
    row = svc.repo.task(tenant, task["task_id"])
    with pytest.raises(OctopError) as error:
        svc.command(
            tenant,
            task["task_id"],
            user,
            {"command": "resume", "expected_revision": row["revision"]},
        )
    assert error.value.status == 409
    assert error.value.details["reason"] == "create_new_task_with_revised_plan"


def test_evidence_tampering_is_detected(platform):
    svc, user, _, tenant, space = platform
    task = svc.create_task(tenant, user, task_body(space))
    with svc.services.db.transaction() as conn:
        conn.execute(
            "UPDATE bm_events SET payload_json=? WHERE tenant_id=? AND task_id=?",
            (json.dumps({"changed": True}), tenant, task["task_id"]),
        )
    assert svc.evidence(tenant, task["task_id"], user)["valid"] is False


async def test_background_start_runs_and_shutdown_cleans_jobs(platform):
    svc, user, _, tenant, space = platform
    task = svc.create_task(tenant, user, task_body(space))
    svc.command(tenant, task["task_id"], user, {"command": "start", "expected_revision": 1})
    for _ in range(40):
        await asyncio.sleep(0)
        if svc.repo.task(tenant, task["task_id"])["status"] == "completed":
            break
    assert svc.repo.task(tenant, task["task_id"])["status"] == "completed"
    await svc.close()
    assert not svc._jobs


def test_two_database_connections_have_one_config_winner(platform, tmp_path):
    svc, user, _, tenant, space = platform
    other_pool = SqlitePool(tmp_path / "control.db")
    try:
        other = LongtermRepo(other_pool)
        other.save_config(
            tenant, space, user.id, expected_revision=0, config=public_config(), device_id="winner"
        )
        with pytest.raises(RevisionConflict):
            svc.save_config(
                tenant, space, user, expected_revision=0, config=public_config(), device_id="loser"
            )
    finally:
        other_pool.close()


async def test_disabled_creator_pauses_background_task_before_next_dispatch(platform, monkeypatch):
    svc, user, _, tenant, space = platform
    monkeypatch.setattr(svc, "schedule", lambda *args: None)
    task = svc.create_task(tenant, user, task_body(space))
    svc.command(tenant, task["task_id"], user, {"command": "start", "expected_revision": 1})
    svc.services.user_repo.set_disabled(user.id, True)
    assert await svc.step(tenant, task["task_id"]) == "paused"
    row = svc.repo.task(tenant, task["task_id"])
    assert row["status"] == "paused"
    assert row["checkpoint"] == {}
    with pytest.raises(OctopError) as error:
        svc.authorize(tenant, user, write=True)
    assert error.value.status == 403


def test_disabled_creator_cannot_admit_or_authorize_native_evaluation_task(platform):
    svc, user, _, tenant, space = platform
    original = svc.create_task(tenant, user, task_body(space))
    document = {**original, "task_id": "disabled-eval-admission"}
    svc.services.user_repo.set_disabled(user.id, True)
    with pytest.raises(AuthorityRevoked):
        svc.repo.create_task(document, "disabled-eval", digest(document))
    assert svc.repo.task(tenant, document["task_id"]) is None
    with pytest.raises(AuthorityRevoked):
        svc.repo.assert_authority(tenant, original["task_id"], user.id)
