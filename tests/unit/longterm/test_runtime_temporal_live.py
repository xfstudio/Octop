"""Opt-in real SDK + ephemeral Temporal test-server smoke test."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from octop.infra.longterm.temporal_runtime import (
    run_worker,
    start_task,
    wake_task,
    worker_is_polling,
    workflow_id,
)


@pytest.mark.live
async def test_real_temporal_worker_resumes_approval_and_uses_durable_timer():
    pytest.importorskip("temporalio")
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment

    seen = asyncio.Event()
    calls = 0

    async def step(identity):
        nonlocal calls
        info = activity.info()
        assert info.start_to_close_timeout == timedelta(days=7)
        assert info.heartbeat_timeout == timedelta(seconds=90)
        assert info.retry_policy is not None and info.retry_policy.maximum_attempts == 1
        calls += 1
        if calls == 1:
            seen.set()
            return {"status": "waiting_approval", "checkpoint": {}}
        if calls == 2:
            # A concurrent resume/approval command can preserve SQL's queued
            # state while the previous activity commits its checkpoint.
            return {"status": "queued", "checkpoint": {}}
        if calls == 3:
            return {
                "status": "sleeping",
                "checkpoint": {
                    "sleep_until": (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
                },
            }
        return {"status": "completed", "checkpoint": {}, "result": {"goal_outcome": "inconclusive"}}

    async with await WorkflowEnvironment.start_local() as environment:
        queue = "octop-test-" + uuid4().hex
        worker = asyncio.create_task(
            run_worker(
                step, address=environment.client.service_client.config.target_host, task_queue=queue
            )
        )
        task_id = uuid4().hex
        try:
            await start_task(
                environment.client, tenant_id="tenant-smoke", task_id=task_id, task_queue=queue
            )
            async with asyncio.timeout(40):
                await seen.wait()
                assert await worker_is_polling(
                    environment.client, namespace="default", task_queue=queue
                )
                handle = environment.client.get_workflow_handle(
                    workflow_id("tenant-smoke", task_id)
                )
                while await handle.query("status") != "waiting_approval":
                    await asyncio.sleep(0.02)
                # The durable approval wait must survive a worker process lifetime.
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
                worker = asyncio.create_task(
                    run_worker(
                        step,
                        address=environment.client.service_client.config.target_host,
                        task_queue=queue,
                    )
                )
                await wake_task(environment.client, tenant_id="tenant-smoke", task_id=task_id)
                description = await handle.describe()
                handle = environment.client.get_workflow_handle(
                    workflow_id("tenant-smoke", task_id), run_id=description.run_id
                )
                result = await handle.result()
            assert result["status"] == "completed"
            assert calls == 4
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


@pytest.mark.live
async def test_real_temporal_reenters_sql_recovery_after_running_worker_stops(
    tmp_path, monkeypatch
):
    pytest.importorskip("temporalio")
    from temporalio.testing import WorkflowEnvironment

    from octop.config import OctopConfig
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.services import build_shared_services
    from octop.infra.longterm.service import LongtermService
    from octop.infra.users.identity import User
    from octop.infra.utils.paths import PathLayout

    monkeypatch.delenv("OCTOP_FACT_REPORTS_ROOT", raising=False)
    database = SqlitePool(tmp_path / "control.db")
    run_migrations(database)
    shared = build_shared_services(db=database, paths=PathLayout(tmp_path), config=OctopConfig())
    uid = shared.user_repo.create(username="temporal-crash-test", role="user")
    user = User(uid, "temporal-crash-test", "user", None)
    service = LongtermService(shared)
    monkeypatch.setattr(service, "schedule", lambda *args: None)
    tenant_id = service.create_tenant(user, "Crash recovery tenant")["tenant_id"]
    space_id = service.create_space(tenant_id, user, "Private space")["space_id"]
    agent_id = shared.agent_repo.create(
        agent_id=uuid4().hex, user_id=uid, name="Crash test adapter"
    )
    task = service.create_task(
        tenant_id,
        user,
        {
            "space_id": space_id,
            "goal": "Verify unresolved writes are never repeated after worker loss",
            "mode": "temporal",
            "workflow": {
                "nodes": [
                    {"id": "write", "skill": "agent.analyze", "agent_id": agent_id, "risk": "R2"}
                ]
            },
        },
    )
    task_id = task["task_id"]
    service.command(tenant_id, task_id, user, {"command": "start", "expected_revision": 1})
    for _ in range(4):
        assert await service.step(tenant_id, task_id) == "running"
    assert await service.step(tenant_id, task_id) == "waiting_approval"
    task = service.repo.task(tenant_id, task_id)
    service.command(
        tenant_id,
        task_id,
        user,
        {
            "command": "approve",
            "expected_revision": task["revision"],
            "node_id": "write",
            "plan_digest": task["plan_digest"],
        },
    )
    running = asyncio.Event()
    adapter_calls = 0

    async def write_adapter(node, current):
        nonlocal adapter_calls
        adapter_calls += 1
        running.set()
        # The real service has persisted an authorized ActionIntent. This
        # synthetic adapter deliberately never confirms an external outcome.
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_execute_node", write_adapter)

    async def step(identity):
        await service.step(identity["tenant_id"], identity["task_id"])
        current = service.repo.task(identity["tenant_id"], identity["task_id"])
        return {
            "status": current["status"],
            "checkpoint": current["checkpoint"],
            "result": current["result"],
        }

    async with await WorkflowEnvironment.start_local() as environment:
        queue = "octop-crash-test-" + uuid4().hex
        address = environment.client.service_client.config.target_host
        worker = asyncio.create_task(run_worker(step, address=address, task_queue=queue))
        try:
            await start_task(
                environment.client, tenant_id=tenant_id, task_id=task_id, task_queue=queue
            )
            async with asyncio.timeout(180):
                await running.wait()
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
                worker = asyncio.create_task(run_worker(step, address=address, task_queue=queue))
                handle = environment.client.get_workflow_handle(workflow_id(tenant_id, task_id))
                result = await handle.result()
            assert result["status"] == "uncertain"
            assert result["result"]["reason"] == "write_requires_reconciliation"
            assert service.repo.task(tenant_id, task_id)["status"] == "uncertain"
            assert adapter_calls == 1
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            await service.close()
            database.close()
