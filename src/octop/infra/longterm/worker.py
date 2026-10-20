"""Run the configured Octop control plane behind a real Temporal worker.

    python -m octop.infra.longterm.worker

Uses OCTOP_HOME and Octop's configured SQLite/PostgreSQL database. Temporal
connection settings are device-local OCTOP_TEMPORAL_* environment variables.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import TYPE_CHECKING, Any

from .temporal_runtime import connect, run_worker, worker_is_polling

if TYPE_CHECKING:
    from octop.infra.db.pool import DatabasePool

    from .service import LongtermService


def _open_service() -> tuple[DatabasePool, LongtermService]:
    """Bind only the existing control database; never start gateways or schedulers."""
    from octop.config import load_config
    from octop.infra.db.factory import open_database, should_defer_control_plane_db
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.env_file import apply_env_file, env_file_path
    from octop.infra.utils.paths import PathLayout

    from .service import LongtermService

    paths = PathLayout.from_env()
    paths.ensure_root()
    apply_env_file(env_file_path(paths.root))
    config = load_config(paths.config)
    if should_defer_control_plane_db(config, paths):
        raise RuntimeError("Octop database setup must be completed before starting the worker")
    db = open_database(config, paths)
    try:
        run_migrations(db)
        shared = build_shared_services(db=db, paths=paths, config=config)
        return db, LongtermService(shared)
    except BaseException:
        db.close()
        raise


async def run() -> None:
    # All execution goes through explicit Temporal activities. No web-server,
    # gateway, cron, proactive-care or local recovery scheduler is booted here.
    os.environ["OCTOP_LONGTERM_WORKER"] = "1"
    loop = asyncio.get_running_loop()
    database, service = await loop.run_in_executor(None, _open_service)
    try:

        async def step(identity: dict[str, str]) -> dict[str, Any]:
            tenant_id, task_id = identity["tenant_id"], identity["task_id"]
            status = await service.step(tenant_id, task_id)
            task = service.repo.task(tenant_id, task_id)
            if task is None:
                raise ValueError("long-term task no longer exists")
            # A busy lease is not a successful activity. Keep the existing
            # checkpoint and let the workflow retry after a bounded durable timer.
            if status is None and task["status"] in {"running", "queued"}:
                from datetime import UTC, datetime, timedelta

                checkpoint = dict(task.get("checkpoint") or {})
                checkpoint["sleep_until"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
                return {"status": "sleeping", "checkpoint": checkpoint}
            return {
                "status": task["status"],
                "checkpoint": task.get("checkpoint") or {},
                "result": task.get("result"),
            }

        service.components.require_enabled("temporal")
        settings = service.components.resolve("temporal")
        await run_worker(
            step,
            address=settings["address"] or "localhost:7233",
            namespace=settings["namespace"],
            task_queue=settings["task_queue"],
            tls=settings["tls"],
            api_key=settings["api_key"] or None,
        )
    finally:
        await service.close()
        await loop.run_in_executor(None, database.close)


async def healthcheck() -> None:
    loop = asyncio.get_running_loop()
    database, service = await loop.run_in_executor(None, _open_service)
    try:
        service.components.require_enabled("temporal")
        settings = service.components.resolve("temporal")
        client = await connect(
            address=settings["address"] or "localhost:7233",
            namespace=settings["namespace"],
            tls=settings["tls"],
            api_key=settings["api_key"] or None,
        )
        if not await worker_is_polling(
            client, namespace=settings["namespace"], task_queue=settings["task_queue"]
        ):
            raise RuntimeError("no active Temporal poller for this worker host")
    finally:
        await service.close()
        await loop.run_in_executor(None, database.close)


def main() -> None:
    parser = argparse.ArgumentParser(description="Octop Temporal worker")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    asyncio.run(healthcheck() if args.healthcheck else run())


if __name__ == "__main__":
    main()
