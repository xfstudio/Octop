"""Tenant and creator scoped persistence for immutable evaluation evidence."""

from __future__ import annotations

import builtins
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.longterm import AuthorityRevoked, RevisionConflict, canonical


class EvaluationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    @contextmanager
    def scoped(self, tenant_id: str) -> Iterator[Any]:
        with self.db.transaction() as conn:
            if self.db.dialect == "postgresql":
                conn.execute("SELECT set_config('octop.tenant_id', ?, true)", (tenant_id,))
            yield conn

    def _authority(self, conn: Any, doc: dict[str, Any], actor: int | None) -> None:
        if actor is None:
            return
        share = " FOR SHARE" if self.db.dialect == "postgresql" else ""
        lock = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
        account = conn.execute("SELECT disabled FROM users WHERE id=?" + share, (actor,)).fetchone()
        member = conn.execute(
            "SELECT role FROM bm_memberships WHERE tenant_id=? AND user_id=?" + share,
            (doc["tenant_id"], actor),
        ).fetchone()
        space = conn.execute(
            "SELECT owner_user_id FROM bm_spaces WHERE tenant_id=? AND space_id=?" + lock,
            (doc["tenant_id"], doc["space_id"]),
        ).fetchone()
        if (
            account is None
            or account["disabled"]
            or member is None
            or member["role"] == "viewer"
            or space is None
            or space["owner_user_id"] != actor
            or doc["creator_user_id"] != actor
        ):
            raise AuthorityRevoked("evaluation authority revoked")

    @staticmethod
    def _decode(row: Any) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(row["document_json"])
        doc.update(
            revision=row["revision"], created_at=row["created_at"], updated_at=row["updated_at"]
        )
        return doc

    def get(
        self, tenant: str, kind: str, identifier: str, actor: int | None = None
    ) -> dict[str, Any] | None:
        with self.scoped(tenant) as conn:
            row = conn.execute(
                "SELECT * FROM bm_eval_documents WHERE tenant_id=? AND kind=? AND document_id=?",
                (tenant, kind, identifier),
            ).fetchone()
        return (
            self._decode(row)
            if row and (actor is None or row["creator_user_id"] == actor)
            else None
        )

    def list(
        self, tenant: str, kind: str, actor: int, space: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        with self.scoped(tenant) as conn:
            rows = conn.execute(
                "SELECT * FROM bm_eval_documents WHERE tenant_id=? AND kind=? AND creator_user_id=? "
                + ("AND space_id=? " if space else "")
                + "ORDER BY updated_at DESC,document_id DESC LIMIT 200",
                (tenant, kind, actor, space) if space else (tenant, kind, actor),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def save(
        self,
        doc: dict[str, Any],
        *,
        expected: int = 0,
        actor: int | None = None,
        command_key: str | None = None,
        request_digest: str | None = None,
        attempts: builtins.list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tenant, kind, identifier = doc["tenant_id"], doc["kind"], doc["document_id"]
        if len(canonical(doc).encode()) > 1024 * 1024:
            raise ValueError("evaluation document exceeds 1 MiB")
        with self.scoped(tenant) as conn:
            if self.db.dialect == "postgresql":
                conn.execute(
                    "SELECT tenant_id FROM bm_tenants WHERE tenant_id=? FOR UPDATE", (tenant,)
                )
            self._authority(conn, doc, actor)
            if command_key:
                prior = conn.execute(
                    "SELECT * FROM bm_eval_commands WHERE tenant_id=? AND command_key=?",
                    (tenant, command_key),
                ).fetchone()
                if prior:
                    if prior["request_digest"] != request_digest:
                        raise RevisionConflict("evaluation idempotency key reused")
                    row = conn.execute(
                        "SELECT * FROM bm_eval_documents WHERE tenant_id=? AND kind=? AND document_id=?",
                        (tenant, prior["kind"], prior["document_id"]),
                    ).fetchone()
                    return self._decode(row)
            ts = now_ts()
            value = {**doc, "revision": expected + 1, "updated_at": ts}
            if expected == 0:
                value["created_at"] = ts
                conn.execute(
                    "INSERT INTO bm_eval_documents(tenant_id,kind,document_id,space_id,creator_user_id,revision,status,document_json,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?,?)",
                    (
                        tenant,
                        kind,
                        identifier,
                        doc["space_id"],
                        doc["creator_user_id"],
                        doc["status"],
                        canonical(value),
                        ts,
                        ts,
                    ),
                )
            else:
                cursor = conn.execute(
                    "UPDATE bm_eval_documents SET revision=revision+1,status=?,document_json=?,updated_at=? WHERE tenant_id=? AND kind=? AND document_id=? AND revision=?",
                    (doc["status"], canonical(value), ts, tenant, kind, identifier, expected),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflict("evaluation revision conflict")
            if kind == "suite":
                conn.execute(
                    "INSERT INTO bm_eval_suite_versions(tenant_id,suite_id,version,document_json) VALUES(?,?,?,?)",
                    (tenant, identifier, value["version"], canonical(value)),
                )
            for attempt in attempts or []:
                conn.execute(
                    "INSERT INTO bm_eval_attempts(tenant_id,run_id,attempt_id,case_id,repeat_index,document_json) VALUES(?,?,?,?,?,?)",
                    (
                        tenant,
                        identifier,
                        attempt["attempt_id"],
                        attempt["case_id"],
                        attempt["repeat_index"],
                        canonical(attempt),
                    ),
                )
            if command_key:
                conn.execute(
                    "INSERT INTO bm_eval_commands(tenant_id,command_key,request_digest,kind,document_id) VALUES(?,?,?,?,?)",
                    (tenant, command_key, request_digest, kind, identifier),
                )
        return value

    def prior(self, tenant: str, key: str, request: str, actor: int) -> dict[str, Any] | None:
        with self.scoped(tenant) as conn:
            row = conn.execute(
                "SELECT * FROM bm_eval_commands WHERE tenant_id=? AND command_key=?", (tenant, key)
            ).fetchone()
            if row is None:
                return None
            if row["request_digest"] != request:
                raise RevisionConflict("evaluation idempotency key reused")
            saved = conn.execute(
                "SELECT * FROM bm_eval_documents WHERE tenant_id=? AND kind=? AND document_id=?",
                (tenant, row["kind"], row["document_id"]),
            ).fetchone()
            result = self._decode(saved)
            self._authority(conn, result, actor)
        return result

    def suite_version(self, tenant: str, identifier: str, version: int) -> dict[str, Any] | None:
        with self.scoped(tenant) as conn:
            row = conn.execute(
                "SELECT document_json FROM bm_eval_suite_versions WHERE tenant_id=? AND suite_id=? AND version=?",
                (tenant, identifier, version),
            ).fetchone()
        return json.loads(row["document_json"]) if row else None

    def attempts(self, tenant: str, run: str) -> builtins.list[dict[str, Any]]:
        with self.scoped(tenant) as conn:
            rows = conn.execute(
                "SELECT revision,document_json FROM bm_eval_attempts WHERE tenant_id=? AND run_id=? ORDER BY case_id,repeat_index",
                (tenant, run),
            ).fetchall()
        return [{**json.loads(row["document_json"]), "revision": row["revision"]} for row in rows]

    def save_attempt(self, tenant: str, run: str, attempt: dict[str, Any]) -> dict[str, Any]:
        revision = attempt.get("revision", 1)
        value = {**attempt, "revision": revision + 1}
        with self.scoped(tenant) as conn:
            cursor = conn.execute(
                "UPDATE bm_eval_attempts SET revision=revision+1,document_json=? WHERE tenant_id=? AND run_id=? AND attempt_id=? AND revision=?",
                (canonical(value), tenant, run, attempt["attempt_id"], revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("evaluation attempt revision conflict")
        return value

    def recoverable(self) -> builtins.list[tuple[str, str]]:
        # Enumerate tenant IDs first; PostgreSQL RLS remains enabled for the actual reads.
        with self.db.connect() as conn:
            tenants = conn.execute("SELECT tenant_id FROM bm_tenants").fetchall()
        result: builtins.list[tuple[str, str]] = []
        for tenant in tenants:
            with self.scoped(tenant["tenant_id"]) as conn:
                rows = conn.execute(
                    "SELECT document_id FROM bm_eval_documents WHERE tenant_id=? AND kind='run' AND status IN ('running','paused','cancelled')",
                    (tenant["tenant_id"],),
                ).fetchall()
            result.extend((tenant["tenant_id"], row["document_id"]) for row in rows)
        return result

    def claim(self, tenant: str, run: str, owner: str) -> bool:
        ts = now_ts()
        with self.scoped(tenant) as conn:
            cursor = conn.execute(
                "UPDATE bm_eval_documents SET lease_owner=?,lease_until=? WHERE tenant_id=? AND kind='run' AND document_id=? AND (lease_owner=? OR lease_until<?)",
                (owner, ts + 30, tenant, run, owner, ts),
            )
        return bool(cursor.rowcount == 1)

    def release(self, tenant: str, run: str, owner: str) -> None:
        with self.scoped(tenant) as conn:
            conn.execute(
                "UPDATE bm_eval_documents SET lease_owner=NULL,lease_until=0 WHERE tenant_id=? AND kind='run' AND document_id=? AND lease_owner=?",
                (tenant, run, owner),
            )
