"""SQL persistence and atomic task evidence for private component sessions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.longterm import (
    AuthorityRevoked,
    LongtermRepo,
    RevisionConflict,
    canonical,
    digest,
)


class OperationSessionRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db
        self._tasks = LongtermRepo(db)

    @contextmanager
    def scoped(self, tenant: str, space: str, owner: int) -> Iterator[Any]:
        with self._tasks.scoped(tenant) as conn:
            if self._db.dialect == "postgresql":
                conn.execute("SELECT set_config('octop.space_id', ?, true)", (space,))
                conn.execute("SELECT set_config('octop.user_id', ?, true)", (str(owner),))
            yield conn

    def _owner(self, conn: Any, tenant: str, space: str, owner: int, *, write: bool) -> None:
        share = " FOR SHARE" if self._db.dialect == "postgresql" else ""
        actor = conn.execute("SELECT disabled FROM users WHERE id=?" + share, (owner,)).fetchone()
        member = conn.execute(
            "SELECT role FROM bm_memberships WHERE tenant_id=? AND user_id=?" + share,
            (tenant, owner),
        ).fetchone()
        # Serialize commands within one private space, including different processes.
        lock = " FOR UPDATE" if write and self._db.dialect == "postgresql" else share
        row = conn.execute(
            "SELECT owner_user_id FROM bm_spaces WHERE tenant_id=? AND space_id=?" + lock,
            (tenant, space),
        ).fetchone()
        if actor is None or actor["disabled"] or member is None:
            raise AuthorityRevoked("session authority revoked")
        if write and member["role"] == "viewer":
            raise AuthorityRevoked("session write authority revoked")
        if row is None or row["owner_user_id"] != owner:
            raise LookupError("private space not found")

    def _task(self, conn: Any, tenant: str, space: str, owner: int, task_id: str | None) -> None:
        if task_id is None:
            return
        lock = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        # Take the task lock first, but do not take a shared space lock before
        # _owner upgrades it: two sessions on distinct tasks would deadlock.
        row = conn.execute(
            "SELECT space_id,creator_user_id FROM bm_tasks WHERE tenant_id=? AND task_id=?" + lock,
            (tenant, task_id),
        ).fetchone()
        if row is None or row["space_id"] != space or row["creator_user_id"] != owner:
            raise AuthorityRevoked("session task scope changed")

    @staticmethod
    def _document(row: Any) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(row["document_json"])
        result.update(
            revision=row["revision"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        return result

    @staticmethod
    def _row(conn: Any, tenant: str, space: str, owner: int, session_id: str) -> Any:
        row = conn.execute(
            "SELECT * FROM bm_operation_sessions WHERE tenant_id=? AND space_id=? "
            "AND owner_user_id=? AND session_id=?",
            (tenant, space, owner, session_id),
        ).fetchone()
        if row is None:
            raise LookupError("operation session not found")
        return row

    @staticmethod
    def _prior(
        conn: Any, tenant: str, space: str, owner: int, key: str, request: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT request_digest,response_json FROM bm_operation_commands "
            "WHERE tenant_id=? AND space_id=? AND owner_user_id=? AND command_key=?",
            (tenant, space, owner, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request:
            raise RevisionConflict("operation idempotency key reused")
        result: dict[str, Any] = json.loads(row["response_json"])
        return result

    @staticmethod
    def _save(conn: Any, result: dict[str, Any], key: str, request: str) -> None:
        conn.execute(
            "INSERT INTO bm_operation_commands(tenant_id,space_id,owner_user_id,session_id,"
            "command_key,request_digest,response_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                result["tenant_id"],
                result["space_id"],
                result["owner_user_id"],
                result["session_id"],
                key,
                request,
                canonical(result),
                now_ts(),
            ),
        )

    def _guards(self, conn: Any, guards: dict[str, str]) -> None:
        lock = " FOR SHARE" if self._db.dialect == "postgresql" else ""
        for key, expected in sorted(guards.items()):
            row = conn.execute("SELECT value FROM settings WHERE key=?" + lock, (key,)).fetchone()
            if digest(row["value"] if row else None) != expected:
                raise RevisionConflict("operation component configuration changed")

    def get(self, tenant: str, space: str, owner: int, session_id: str) -> dict[str, Any]:
        with self.scoped(tenant, space, owner) as conn:
            self._owner(conn, tenant, space, owner, write=False)
            result = self._document(self._row(conn, tenant, space, owner, session_id))
        return result

    def list(self, tenant: str, space: str, owner: int) -> list[dict[str, Any]]:
        with self.scoped(tenant, space, owner) as conn:
            self._owner(conn, tenant, space, owner, write=False)
            rows = conn.execute(
                "SELECT * FROM bm_operation_sessions WHERE tenant_id=? AND space_id=? "
                "AND owner_user_id=? ORDER BY created_at DESC,session_id DESC LIMIT 100",
                (tenant, space, owner),
            ).fetchall()
        return [self._document(row) for row in rows]

    def prior(
        self, tenant: str, space: str, owner: int, key: str, request: str
    ) -> dict[str, Any] | None:
        with self.scoped(tenant, space, owner) as conn:
            self._owner(conn, tenant, space, owner, write=True)
            return self._prior(conn, tenant, space, owner, key, request)

    def create(self, document: dict[str, Any], key: str, request: str) -> dict[str, Any]:
        tenant, space, owner = (
            document["tenant_id"],
            document["space_id"],
            document["owner_user_id"],
        )
        with self.scoped(tenant, space, owner) as conn:
            self._task(conn, tenant, space, owner, document["task_id"])
            self._owner(conn, tenant, space, owner, write=True)
            prior = self._prior(conn, tenant, space, owner, key, request)
            if prior is not None:
                return prior
            self._guards(conn, document["snapshot"]["settings_digests"])
            ts = now_ts()
            result = {
                **document,
                "status": "created",
                "revision": 1,
                "created_at": ts,
                "updated_at": ts,
            }
            conn.execute(
                "INSERT INTO bm_operation_sessions(session_id,tenant_id,space_id,owner_user_id,task_id,"
                "status,revision,document_json,created_at,updated_at) VALUES(?,?,?,?,?,'created',1,?,?,?)",
                (
                    result["session_id"],
                    tenant,
                    space,
                    owner,
                    result["task_id"],
                    canonical(result),
                    ts,
                    ts,
                ),
            )
            self._save(conn, result, key, request)
            return result

    def change(
        self,
        document: dict[str, Any],
        *,
        expected_revision: int,
        key: str,
        request: str,
        action: str,
        candidate: dict[str, Any] | None = None,
        result_digest: str | None = None,
    ) -> dict[str, Any]:
        tenant, space, owner = (
            document["tenant_id"],
            document["space_id"],
            document["owner_user_id"],
        )
        session_id = document["session_id"]
        with self.scoped(tenant, space, owner) as conn:
            # All task-linked mutations lock the task before the space/session.
            self._task(conn, tenant, space, owner, document["task_id"])
            self._owner(conn, tenant, space, owner, write=True)
            prior = self._prior(conn, tenant, space, owner, key, request)
            if prior is not None:
                return prior
            current = self._document(self._row(conn, tenant, space, owner, session_id))
            self._guards(conn, current["snapshot"]["settings_digests"])
            if current["revision"] != expected_revision or current["status"] in {
                "committed",
                "cancelled",
            }:
                raise RevisionConflict("operation session changed or closed")
            result = {**current, "revision": expected_revision + 1, "updated_at": now_ts()}
            if action in {"readback", "import"}:
                assert candidate is not None
                result.update(
                    status="ready",
                    candidate=candidate,
                    result_digest=digest(candidate),
                    source=candidate["source"],
                )
            elif action == "commit":
                if current["status"] != "ready" or result_digest != current["result_digest"]:
                    raise RevisionConflict("operation result changed or unavailable")
                result.update(status="committed", result=current["candidate"], candidate=None)
            elif action == "cancel":
                result.update(status="cancelled", candidate=None, result_digest=None, source=None)
            else:
                raise ValueError("unknown operation action")
            changed = conn.execute(
                "UPDATE bm_operation_sessions SET document_json=?,revision=?,status=?,updated_at=? "
                "WHERE tenant_id=? AND space_id=? AND owner_user_id=? AND session_id=? AND revision=?",
                (
                    canonical(result),
                    result["revision"],
                    result["status"],
                    result["updated_at"],
                    tenant,
                    space,
                    owner,
                    session_id,
                    expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise RevisionConflict("operation session revision conflict")
            if action == "commit" and current["task_id"]:
                self._tasks._append(
                    conn,
                    tenant,
                    current["task_id"],
                    "component.result.committed",
                    {
                        "session_id": session_id,
                        "component_id": current["component_id"],
                        "operation": current["operation"],
                        "node_id": current["node_id"],
                        "result_digest": result["result_digest"],
                        "source": result["source"],
                        "verified": result["result"]["verified"],
                        "draft_only": True,
                        "promotion_allowed": False,
                    },
                )
            self._save(conn, result, key, request)
            return result
