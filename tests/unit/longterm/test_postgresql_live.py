"""Live PostgreSQL isolation, migration recovery and concurrent evidence writes.

Set OCTOP_TEST_BM_PG_ADMIN_URL to an administrator DSN. Tests create and remove
their own uniquely named database and login; no existing application DB is reset.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos.longterm import RevisionConflict
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout

pytestmark = [
    pytest.mark.live,
    pytest.mark.postgresql,
    pytest.mark.skipif(
        not os.getenv("OCTOP_TEST_BM_PG_ADMIN_URL"), reason="requires dedicated PG admin DSN"
    ),
]


@pytest.fixture(scope="module")
def pg_platform(tmp_path_factory):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    suffix = uuid4().hex
    database, role = "octop_bm_" + suffix, "bm_role_" + suffix
    url = os.environ["OCTOP_TEST_BM_PG_ADMIN_URL"]
    admin = psycopg.connect(url, autocommit=True)
    pool = None
    app_pool = None
    try:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD 'synthetic-test' NOSUPERUSER NOBYPASSRLS"
            ).format(sql.Identifier(role))
        )
        pool = PostgresPool(make_conninfo(url, dbname=database))
        run_migrations(pool)
        # Replay a partial v22 restore while the other RLS policies already exist.
        with pool.transaction() as conn:
            conn.execute("DROP TABLE bm_commands")
        run_migrations(pool)
        run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}")
                .format(sql.Identifier(role))
                .as_string(admin)
            )
            conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}")
                .format(sql.Identifier(role))
                .as_string(admin)
            )
            conn.execute(
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}")
                .format(sql.Identifier(role))
                .as_string(admin)
            )
        app_pool = PostgresPool(
            make_conninfo(url, dbname=database, user=role, password="synthetic-test")
        )
        paths = PathLayout(tmp_path_factory.mktemp("pg-bm"))
        shared = build_shared_services(db=app_pool, paths=paths, config=OctopConfig())
        uid = shared.user_repo.create(username="pg-user", role="user")
        user = User(uid, "pg-user", "user", None)
        service = LongtermService(shared)
        tenant = service.create_tenant(user, "tenant")["tenant_id"]
        space = service.create_space(tenant, user, "private")["space_id"]
        other = service.create_tenant(user, "other")["tenant_id"]
        yield service, user, tenant, space, other
    finally:
        if app_pool:
            app_pool.close()
        if pool:
            pool.close()
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


def test_rls_blocks_unscoped_reads_and_cross_tenant_writes(pg_platform):
    import psycopg

    service, user, tenant, space, other = pg_platform
    with service.services.db.connect() as conn:
        assert conn.execute("SELECT * FROM bm_spaces").fetchall() == []
        assert conn.execute("SELECT current_setting('is_superuser')").fetchone()[0] == "off"
    with service.repo.scoped(other) as conn:
        assert conn.execute("SELECT * FROM bm_spaces").fetchall() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege), service.repo.scoped(other) as conn:
        conn.execute(
            "INSERT INTO bm_spaces(space_id,tenant_id,owner_user_id,name,created_at,updated_at) VALUES(?,?,?,?,0,0)",
            ("forbidden", tenant, user.id, "x"),
        )
    assert service.config(tenant, space, user)["revision"] == 0


def test_concurrent_evidence_append_serializes_a_single_valid_chain(pg_platform):
    service, user, tenant, space, _ = pg_platform
    task = service.create_task(
        tenant,
        user,
        {
            "space_id": space,
            "goal": "concurrency",
            "workflow": {"nodes": [{"id": "echo", "skill": "local.echo"}]},
        },
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                service.repo.append, tenant, task["task_id"], "concurrent", {"index": index}
            )
            for index in range(12)
        ]
        for future in futures:
            future.result(timeout=15)
    chain = service.evidence(tenant, task["task_id"], user)
    assert chain["valid"] is True
    assert chain["last_sequence"] == 13
    assert len({entry["digest"] for entry in chain["events"]}) == 13


def test_postgresql_artifact_commit_rechecks_revocation_and_admin_demotion(pg_platform):
    service, owner, tenant, _, _ = pg_platform
    uid = service.services.user_repo.create(username="pg-revocable", role="user")
    actor = User(uid, "pg-revocable", "user", None)
    service.set_member(tenant, owner, uid, "admin")
    space = service.create_space(tenant, actor, "actor-private")["space_id"]
    task = service.create_task(
        tenant,
        actor,
        {
            "space_id": space,
            "goal": "revocation",
            "workflow": {"nodes": [{"id": "echo", "skill": "local.echo"}]},
        },
    )
    service.set_member(tenant, owner, uid, None)
    with pytest.raises(OctopError) as revoked:
        service._record_artifact(task, actor, "evaluation", {"source": "test"}, 1)
    assert revoked.value.status == 403
    service.set_member(tenant, owner, uid, "member")
    with pytest.raises(OctopError) as demoted:
        service._record_artifact(task, actor, "evolution", {"source": "test"}, 1)
    assert demoted.value.status == 403
    assert service.repo.task(tenant, task["task_id"])["revision"] == 1


async def test_real_postgresql_workflow_and_config_cas(pg_platform, monkeypatch):
    service, user, tenant, space, _ = pg_platform
    monkeypatch.setattr(service, "schedule", lambda *args: None)
    config = {"schema_version": 1, "workflow": {"nodes": []}}
    service.save_config(tenant, space, user, expected_revision=0, config=config, device_id="one")
    with pytest.raises(RevisionConflict):
        service.save_config(
            tenant, space, user, expected_revision=0, config=config, device_id="two"
        )
    task = service.create_task(
        tenant,
        user,
        {
            "space_id": space,
            "goal": "execute",
            "workflow": {"nodes": [{"id": "echo", "skill": "local.echo"}]},
        },
    )
    service.command(tenant, task["task_id"], user, {"command": "start", "expected_revision": 1})
    for _ in range(10):
        await service.step(tenant, task["task_id"])
        result = service.repo.task(tenant, task["task_id"])
        if result["status"] not in {"running", "queued"}:
            break
    assert result["status"] == "completed"
    assert result["result"]["goal_outcome"] == "inconclusive"
    assert service.evidence(tenant, task["task_id"], user)["valid"]
