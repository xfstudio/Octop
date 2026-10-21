from __future__ import annotations

from octop.infra.db.migrate import _discover, run_migrations
from octop.infra.db.pool import SqlitePool


def test_discover_sqlite_excludes_pg_files():
    files = _discover("sqlite")
    names = [p.name for _, p in files]
    assert any(n == "001_initial.sql" for n in names)
    assert not any(n.endswith(".pg.sql") for n in names)


def test_discover_postgresql_only_pg_files():
    files = _discover("postgresql")
    names = [p.name for _, p in files]
    assert "001_initial.pg.sql" in names
    assert not any(n.endswith(".sql") and not n.endswith(".pg.sql") for n in names)


def test_repair_longterm_tables_preserves_later_applied_watermark(tmp_path):
    db = SqlitePool(tmp_path / "repair.db")
    try:
        run_migrations(db)
        latest = max(v for v, _ in _discover())
        with db.connect() as conn:
            conn.execute("DROP TABLE bm_events")
        run_migrations(db)
        with db.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == latest
            assert conn.execute("SELECT COUNT(*) FROM bm_events").fetchone()[0] == 0
        run_migrations(db)
        with db.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == latest
    finally:
        db.close()
