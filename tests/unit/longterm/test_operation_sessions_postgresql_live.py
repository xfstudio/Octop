"""Live PostgreSQL private RLS and concurrent session/evidence transactions."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from octop.infra.longterm.operation_sessions import OperationSessionService
from tests.unit.longterm.test_postgresql_live import pg_platform as pg_platform
from tests.unit.longterm.test_service import task_body

pytestmark = [
    pytest.mark.live,
    pytest.mark.postgresql,
    pytest.mark.skipif(
        not os.getenv("OCTOP_TEST_BM_PG_ADMIN_URL"), reason="requires dedicated PG admin DSN"
    ),
]


def _session(platform: Any) -> tuple[Any, Any, Any]:
    service, user, tenant, space, _ = platform
    suffix = uuid4().hex
    task = service.create_task(
        tenant, user, {**task_body(space), "idempotency_key": "task-" + suffix}
    )
    sessions = OperationSessionService(service)
    session = sessions.create(
        tenant,
        space,
        user,
        {
            "component_id": "octop",
            "operation": "task",
            "input": {},
            "task_id": task["task_id"],
            "idempotency_key": "session-" + suffix,
        },
    )
    return sessions, session, task


def test_session_rls_requires_tenant_space_and_owner_context(pg_platform: Any) -> None:
    service, user, tenant, space, _ = pg_platform
    sessions, session, _ = _session(pg_platform)
    db = service.services.db
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM bm_operation_sessions").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM bm_operation_commands").fetchone()["n"] == 0
        conn.execute("SELECT set_config('octop.tenant_id', ?, true)", (tenant,))
        assert conn.execute("SELECT COUNT(*) AS n FROM bm_operation_sessions").fetchone()["n"] == 0
        conn.execute("SELECT set_config('octop.space_id', ?, true)", (space,))
        conn.execute("SELECT set_config('octop.user_id', ?, true)", (str(user.id + 1000),))
        assert conn.execute("SELECT COUNT(*) AS n FROM bm_operation_sessions").fetchone()["n"] == 0
        conn.execute("SELECT set_config('octop.user_id', ?, true)", (str(user.id),))
        assert (
            conn.execute(
                "SELECT session_id FROM bm_operation_sessions WHERE session_id=?",
                (session["session_id"],),
            ).fetchone()
            is not None
        )
        conn.execute("SELECT set_config('octop.space_id', ?, true)", ("foreign-space",))
        assert conn.execute("SELECT COUNT(*) AS n FROM bm_operation_sessions").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM bm_operation_commands").fetchone()["n"] == 0
    assert sessions.get(tenant, space, user, session["session_id"])["revision"] == 1


def test_distinct_task_sessions_commit_concurrently_without_space_lock_upgrade(
    pg_platform: Any,
) -> None:
    service, user, tenant, space, _ = pg_platform
    prepared = []
    for _ in range(2):
        sessions, session, task = _session(pg_platform)
        ready = sessions.import_result(
            tenant,
            space,
            user,
            session["session_id"],
            {
                "expected_revision": 1,
                "idempotency_key": "import-" + session["session_id"],
                "result": {"value": 1},
            },
        )
        prepared.append((sessions, ready, task))

    def commit(item: Any) -> Any:
        sessions, ready, _ = item
        return sessions.commit(
            tenant,
            space,
            user,
            ready["session_id"],
            {
                "expected_revision": ready["revision"],
                "idempotency_key": "commit-" + ready["session_id"],
                "result_digest": ready["result_digest"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(commit, item) for item in prepared]
        results = [future.result(timeout=20) for future in futures]
    assert all(result["status"] == "committed" for result in results)
    for _, ready, task in prepared:
        assert service.task(tenant, task["task_id"], user)["revision"] == task["revision"]
        evidence = service.evidence(tenant, task["task_id"], user)
        assert evidence["valid"]
        assert (
            sum(
                event["event_type"] == "component.result.committed"
                and event["payload"]["session_id"] == ready["session_id"]
                for event in evidence["events"]
            )
            == 1
        )
