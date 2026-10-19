"""Tenant-scoped SQL persistence for the long-running task control plane."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class RevisionConflict(ValueError):
    """The caller must reload before applying its change."""


class AuthorityRevoked(RuntimeError):
    """The actor no longer has the required authority over this private task."""


class LongtermRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @contextmanager
    def scoped(self, tenant_id: str) -> Iterator[Any]:
        with self._db.transaction() as conn:
            if self._db.dialect == "postgresql":
                conn.execute("SELECT set_config('octop.tenant_id', ?, true)", (tenant_id,))
            yield conn

    def tenants(self, user_id: int) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT t.tenant_id, t.name, t.revision, m.role FROM bm_tenants t "
                "JOIN bm_memberships m ON t.tenant_id = m.tenant_id "
                "WHERE m.user_id = ? ORDER BY t.created_at, t.tenant_id",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_tenant(self, tenant_id: str, name: str, user_id: int) -> dict[str, Any]:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO bm_tenants(tenant_id,name,created_at) VALUES(?,?,?)",
                (tenant_id, name, now_ts()),
            )
            conn.execute(
                "INSERT INTO bm_memberships(tenant_id,user_id,role) VALUES(?,?,'owner')",
                (tenant_id, user_id),
            )
        return {"tenant_id": tenant_id, "name": name, "role": "owner", "revision": 1}

    def role(self, tenant_id: str, user_id: int) -> str | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT role FROM bm_memberships WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id),
            ).fetchone()
        return str(row["role"]) if row else None

    def members(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT user_id,role FROM bm_memberships WHERE tenant_id=?", (tenant_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_member(self, tenant_id: str, user_id: int, role: str | None) -> None:
        with self._db.transaction() as conn:
            if role is None:
                conn.execute(
                    "DELETE FROM bm_memberships WHERE tenant_id=? AND user_id=? AND role!='owner'",
                    (tenant_id, user_id),
                )
            else:
                conn.execute(
                    "INSERT INTO bm_memberships(tenant_id,user_id,role) VALUES(?,?,?) "
                    "ON CONFLICT(tenant_id,user_id) DO UPDATE SET role=excluded.role "
                    "WHERE bm_memberships.role!='owner'",
                    (tenant_id, user_id, role),
                )

    def spaces(self, tenant_id: str, user_id: int) -> list[dict[str, Any]]:
        with self.scoped(tenant_id) as conn:
            rows = conn.execute(
                "SELECT space_id,tenant_id,name,revision,updated_at FROM bm_spaces "
                "WHERE tenant_id=? AND owner_user_id=? ORDER BY created_at",
                (tenant_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_space(
        self, tenant_id: str, space_id: str, name: str, user_id: int
    ) -> dict[str, Any]:
        ts = now_ts()
        with self.scoped(tenant_id) as conn:
            conn.execute(
                "INSERT INTO bm_spaces(space_id,tenant_id,owner_user_id,name,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (space_id, tenant_id, user_id, name, ts, ts),
            )
        return {"space_id": space_id, "tenant_id": tenant_id, "name": name, "revision": 0}

    def config(self, tenant_id: str, space_id: str, user_id: int) -> dict[str, Any] | None:
        with self.scoped(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM bm_spaces WHERE tenant_id=? AND space_id=? AND owner_user_id=?",
                (tenant_id, space_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "tenant_id": tenant_id,
            "space_id": space_id,
            "name": row["name"],
            "revision": row["revision"],
            "device_id": row["device_id"],
            "updated_at": row["updated_at"],
            "config": json.loads(row["config_json"]),
        }

    def save_config(
        self,
        tenant_id: str,
        space_id: str,
        user_id: int,
        *,
        expected_revision: int,
        config: dict[str, Any],
        device_id: str,
    ) -> dict[str, Any]:
        with self.scoped(tenant_id) as conn:
            changed = conn.execute(
                "UPDATE bm_spaces SET config_json=?,device_id=?,revision=revision+1,updated_at=? "
                "WHERE tenant_id=? AND space_id=? AND owner_user_id=? AND revision=?",
                (
                    canonical(config),
                    device_id,
                    now_ts(),
                    tenant_id,
                    space_id,
                    user_id,
                    expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise RevisionConflict("space revision conflict")
        result = self.config(tenant_id, space_id, user_id)
        assert result is not None
        return result

    @staticmethod
    def _task(row: Any) -> dict[str, Any]:
        task: dict[str, Any] = json.loads(row["document_json"])
        task.update(
            status=row["status"],
            revision=row["revision"],
            checkpoint=json.loads(row["checkpoint_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        return task

    def task(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self.scoped(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM bm_tasks WHERE tenant_id=? AND task_id=?", (tenant_id, task_id)
            ).fetchone()
        return self._task(row) if row else None

    def tasks(self, tenant_id: str, user_id: int) -> list[dict[str, Any]]:
        with self.scoped(tenant_id) as conn:
            rows = conn.execute(
                "SELECT * FROM bm_tasks WHERE tenant_id=? AND creator_user_id=? "
                "ORDER BY created_at DESC,task_id DESC LIMIT 200",
                (tenant_id, user_id),
            ).fetchall()
        return [self._task(row) for row in rows]

    def create_task(
        self, task: dict[str, Any], command_key: str, request_digest: str
    ) -> dict[str, Any]:
        tenant_id, task_id = task["tenant_id"], task["task_id"]
        ts = now_ts()
        with self.scoped(tenant_id) as conn:
            # Serialize admissions per tenant in PostgreSQL, including distinct processes.
            if self._db.dialect == "postgresql":
                conn.execute(
                    "SELECT tenant_id FROM bm_tenants WHERE tenant_id=? FOR UPDATE", (tenant_id,)
                )
            prior = self._command(conn, tenant_id, command_key, request_digest)
            if prior is not None:
                return prior
            conn.execute(
                "INSERT INTO bm_tasks(task_id,tenant_id,space_id,creator_user_id,status,document_json,created_at,updated_at) "
                "VALUES(?,?,?,?,'draft',?,?,?)",
                (
                    task_id,
                    tenant_id,
                    task["space_id"],
                    task["creator_user_id"],
                    canonical(task),
                    ts,
                    ts,
                ),
            )
            self._append(
                conn, tenant_id, task_id, "task.created", {"plan_digest": task["plan_digest"]}
            )
            response = {
                **task,
                "status": "draft",
                "revision": 1,
                "checkpoint": {},
                "result": None,
                "created_at": ts,
                "updated_at": ts,
            }
            self._save_command(conn, tenant_id, task_id, command_key, request_digest, response)
            return response

    @staticmethod
    def _command(conn: Any, tenant_id: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM bm_commands WHERE tenant_id=? AND command_key=?", (tenant_id, key)
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise RevisionConflict("idempotency key reused for a different request")
        result: dict[str, Any] = json.loads(row["response_json"])
        return result

    @staticmethod
    def _save_command(
        conn: Any,
        tenant_id: str,
        task_id: str,
        key: str,
        request_digest: str,
        response: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO bm_commands(tenant_id,task_id,command_key,request_digest,response_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (tenant_id, task_id, key, request_digest, canonical(response), now_ts()),
        )

    def command(
        self,
        task: dict[str, Any],
        *,
        expected_revision: int,
        command_key: str,
        request_digest: str,
        command: str,
        audit_payload: dict[str, Any] | None = None,
        actor_user_id: int | None = None,
        require_admin: bool = False,
    ) -> dict[str, Any]:
        tenant_id, task_id = task["tenant_id"], task["task_id"]
        with self.scoped(tenant_id) as conn:
            if actor_user_id is not None:
                self._assert_actor(
                    conn, tenant_id, task_id, actor_user_id, require_admin=require_admin
                )
            elif self._db.dialect == "postgresql":
                conn.execute(
                    "SELECT task_id FROM bm_tasks WHERE tenant_id=? AND task_id=? FOR UPDATE",
                    (tenant_id, task_id),
                )
            prior = self._command(conn, tenant_id, command_key, request_digest)
            if prior is not None:
                return prior
            changed = conn.execute(
                "UPDATE bm_tasks SET document_json=?,status=?,revision=revision+1,updated_at=? "
                "WHERE tenant_id=? AND task_id=? AND revision=?",
                (canonical(task), task["status"], now_ts(), tenant_id, task_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise RevisionConflict("task revision conflict")
            self._append(
                conn,
                tenant_id,
                task_id,
                "command." + command,
                {
                    "revision": expected_revision + 1,
                    "plan_digest": task["plan_digest"],
                    **({"record": audit_payload} if audit_payload is not None else {}),
                },
            )
            response = {**task, "revision": expected_revision + 1}
            self._save_command(conn, tenant_id, task_id, command_key, request_digest, response)
        return response

    def _assert_actor(
        self,
        conn: Any,
        tenant_id: str,
        task_id: str,
        actor_user_id: int,
        *,
        require_admin: bool = False,
        write: bool = True,
    ) -> None:
        task_lock = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        share_lock = " FOR SHARE" if self._db.dialect == "postgresql" else ""
        task = conn.execute(
            "SELECT creator_user_id,space_id FROM bm_tasks WHERE tenant_id=? AND task_id=?"
            + task_lock,
            (tenant_id, task_id),
        ).fetchone()
        member = conn.execute(
            "SELECT role FROM bm_memberships WHERE tenant_id=? AND user_id=?" + share_lock,
            (tenant_id, actor_user_id),
        ).fetchone()
        role = member["role"] if member else None
        if (
            task is None
            or task["creator_user_id"] != actor_user_id
            or role is None
            or (write and role == "viewer")
            or (require_admin and role not in {"owner", "admin"})
        ):
            raise AuthorityRevoked("private task authority revoked")
        space = conn.execute(
            "SELECT owner_user_id FROM bm_spaces WHERE tenant_id=? AND space_id=?" + share_lock,
            (tenant_id, task["space_id"]),
        ).fetchone()
        if space is None or space["owner_user_id"] != actor_user_id:
            raise AuthorityRevoked("private space authority revoked")

    def assert_authority(
        self,
        tenant_id: str,
        task_id: str,
        actor_user_id: int,
        *,
        require_admin: bool = False,
        write: bool = True,
    ) -> None:
        with self.scoped(tenant_id) as conn:
            self._assert_actor(
                conn,
                tenant_id,
                task_id,
                actor_user_id,
                require_admin=require_admin,
                write=write,
            )

    def replay(self, tenant_id: str, key: str, request_digest: str) -> dict[str, Any] | None:
        with self.scoped(tenant_id) as conn:
            return self._command(conn, tenant_id, key, request_digest)

    def permit(self, task: dict[str, Any], owner: str, event: dict[str, Any]) -> None:
        """Fence the writer and recheck live authority in the same transaction as the intent."""
        with self.scoped(task["tenant_id"]) as conn:
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            row = conn.execute(
                "SELECT revision,status,lease_until FROM bm_tasks WHERE tenant_id=? AND task_id=? AND lease_owner=?"
                + suffix,
                (task["tenant_id"], task["task_id"], owner),
            ).fetchone()
            member = conn.execute(
                "SELECT role FROM bm_memberships WHERE tenant_id=? AND user_id=?",
                (task["tenant_id"], task["creator_user_id"]),
            ).fetchone()
            if (
                row is None
                or row["revision"] != task["revision"]
                or row["lease_until"] < now_ts()
                or row["status"] not in {"queued", "running"}
                or member is None
                or member["role"] == "viewer"
            ):
                raise RevisionConflict("execution fenced or authority revoked")
            self._append(
                conn,
                task["tenant_id"],
                task["task_id"],
                str(event.get("kind", "runtime.event")),
                event,
            )

    def _append(
        self, conn: Any, tenant_id: str, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        # All writers, including checkpoint and memory/provenance callbacks, share
        # the task row lock before allocating the next sequence.
        if self._db.dialect == "postgresql":
            conn.execute(
                "SELECT task_id FROM bm_tasks WHERE tenant_id=? AND task_id=? FOR UPDATE",
                (tenant_id, task_id),
            )
        row = conn.execute(
            "SELECT sequence,digest FROM bm_events WHERE tenant_id=? AND task_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (tenant_id, task_id),
        ).fetchone()
        event = {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "sequence": row["sequence"] + 1 if row else 1,
            "previous_digest": row["digest"] if row else "0" * 64,
            "event_type": event_type,
            "payload": payload,
            "created_at": now_ts(),
        }
        event["digest"] = digest(event)
        conn.execute(
            "INSERT INTO bm_events(tenant_id,task_id,sequence,event_type,payload_json,previous_digest,digest,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                tenant_id,
                task_id,
                event["sequence"],
                event_type,
                canonical(payload),
                event["previous_digest"],
                event["digest"],
                event["created_at"],
            ),
        )
        return event

    def append(
        self,
        tenant_id: str,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor_user_id: int | None = None,
        require_admin: bool = False,
        write: bool = True,
    ) -> dict[str, Any]:
        with self.scoped(tenant_id) as conn:
            if actor_user_id is not None:
                self._assert_actor(
                    conn,
                    tenant_id,
                    task_id,
                    actor_user_id,
                    require_admin=require_admin,
                    write=write,
                )
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            row = conn.execute(
                "SELECT task_id FROM bm_tasks WHERE tenant_id=? AND task_id=?" + suffix,
                (tenant_id, task_id),
            ).fetchone()
            if row is None:
                raise ValueError("task not found")
            return self._append(conn, tenant_id, task_id, event_type, payload)

    def events(
        self, tenant_id: str, task_id: str, after: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        with self.scoped(tenant_id) as conn:
            rows = conn.execute(
                "SELECT * FROM bm_events WHERE tenant_id=? AND task_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (tenant_id, task_id, after, limit),
            ).fetchall()
        return [
            {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "sequence": r["sequence"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload_json"]),
                "previous_digest": r["previous_digest"],
                "digest": r["digest"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def dispatchable(self) -> list[tuple[str, str]]:
        # Only ids leave this system query; payload access always re-enters tenant scope.
        with self._db.connect() as conn:
            rows = conn.execute("SELECT tenant_id FROM bm_tenants").fetchall()
        result: list[tuple[str, str]] = []
        for row in rows:
            tenant_id = str(row["tenant_id"])
            with self.scoped(tenant_id) as conn:
                tasks = conn.execute(
                    "SELECT task_id FROM bm_tasks WHERE tenant_id=? "
                    "AND status IN ('queued','running','sleeping')",
                    (tenant_id,),
                ).fetchall()
            result.extend((tenant_id, str(t["task_id"])) for t in tasks)
        return result

    def claim(self, tenant_id: str, task_id: str, owner: str) -> dict[str, Any] | None:
        with self.scoped(tenant_id) as conn:
            changed = conn.execute(
                "UPDATE bm_tasks SET lease_owner=?,lease_until=? WHERE tenant_id=? AND task_id=? "
                "AND status IN ('queued','running') AND lease_until<?",
                (owner, now_ts() + 120, tenant_id, task_id, now_ts()),
            ).rowcount
        return self.task(tenant_id, task_id) if changed == 1 else None

    def renew(self, tenant_id: str, task_id: str, owner: str) -> bool:
        with self.scoped(tenant_id) as conn:
            return bool(
                conn.execute(
                    "UPDATE bm_tasks SET lease_until=? WHERE tenant_id=? AND task_id=? AND lease_owner=?",
                    (now_ts() + 120, tenant_id, task_id, owner),
                ).rowcount
            )

    def finish_event(self, task: dict[str, Any], owner: str, event: dict[str, Any]) -> None:
        with self.scoped(task["tenant_id"]) as conn:
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            row = conn.execute(
                "SELECT lease_owner,lease_until FROM bm_tasks WHERE tenant_id=? AND task_id=?"
                + suffix,
                (task["tenant_id"], task["task_id"]),
            ).fetchone()
            if row is None or row["lease_owner"] != owner or row["lease_until"] < now_ts():
                raise RevisionConflict("execution completion fenced")
            self._append(
                conn,
                task["tenant_id"],
                task["task_id"],
                str(event.get("kind", "runtime.event")),
                event,
            )

    def wake_due(self, tenant_id: str, task_id: str) -> None:
        from datetime import UTC, datetime

        with self.scoped(tenant_id) as conn:
            row = conn.execute(
                "SELECT status,checkpoint_json FROM bm_tasks WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id),
            ).fetchone()
            if row is None or row["status"] != "sleeping":
                return
            wake_at = json.loads(row["checkpoint_json"]).get("sleep_until")
            if isinstance(wake_at, str) and datetime.fromisoformat(
                wake_at.replace("Z", "+00:00")
            ) <= datetime.now(UTC):
                conn.execute(
                    "UPDATE bm_tasks SET status='queued',revision=revision+1 WHERE tenant_id=? AND task_id=? AND status='sleeping'",
                    (tenant_id, task_id),
                )

    def checkpoint(
        self,
        task: dict[str, Any],
        owner: str,
        checkpoint: dict[str, Any],
        *,
        status: str,
        result: dict[str, Any] | None,
        events: list[dict[str, Any]],
    ) -> bool:
        with self.scoped(task["tenant_id"]) as conn:
            # Control commands win over an in-flight worker's stale state. Its results still persist.
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            row = conn.execute(
                "SELECT status,revision,lease_until FROM bm_tasks WHERE tenant_id=? AND task_id=? AND lease_owner=?"
                + suffix,
                (task["tenant_id"], task["task_id"], owner),
            ).fetchone()
            if row is None or row["lease_until"] < now_ts():
                return False
            if row["revision"] != task["revision"]:
                status = row["status"]
            conn.execute(
                "UPDATE bm_tasks SET checkpoint_json=?,result_json=?,status=?,revision=revision+1,updated_at=?, "
                "lease_owner=NULL,lease_until=0 WHERE tenant_id=? AND task_id=? AND lease_owner=?",
                (
                    canonical(checkpoint),
                    canonical(result) if result is not None else None,
                    status,
                    now_ts(),
                    task["tenant_id"],
                    task["task_id"],
                    owner,
                ),
            )
            for event in events:
                self._append(
                    conn,
                    task["tenant_id"],
                    task["task_id"],
                    str(event.get("event_type", event.get("kind", "runtime.event"))),
                    event,
                )
        return True

    def release(self, tenant_id: str, task_id: str, owner: str) -> None:
        with self.scoped(tenant_id) as conn:
            conn.execute(
                "UPDATE bm_tasks SET lease_owner=NULL,lease_until=0 WHERE tenant_id=? AND task_id=? AND lease_owner=?",
                (tenant_id, task_id, owner),
            )
