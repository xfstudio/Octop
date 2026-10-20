"""Lazy Temporal Python SDK client and worker entry point.

Example::

    python -m octop.infra.longterm.temporal_runtime --factory package.module:factory

The factory returns an async callable ``step({tenant_id, task_id})`` backed by
the same Octop service/repository used by local execution. It must acquire the
task lease and reconcile unresolved write intents before advancing the runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from .adapters import IntegrationUnavailable
from .execution import canonical_json

ServiceStep = Callable[[dict[str, str]], Awaitable[dict[str, Any]]]


def workflow_id(tenant_id: str, task_id: str) -> str:
    digest = hashlib.sha256(canonical_json([tenant_id, task_id]).encode()).hexdigest()
    return f"octop-goal-{digest}"


def _sdk(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise IntegrationUnavailable("install octop[longterm-temporal] to use Temporal") from exc


async def connect(
    *,
    address: str = "localhost:7233",
    namespace: str = "default",
    tls: bool = False,
    api_key: str | None = None,
) -> Any:
    client_type = _sdk("temporalio.client").Client
    return await client_type.connect(address, namespace=namespace, tls=tls, api_key=api_key)


async def start_task(
    client: Any, *, tenant_id: str, task_id: str, task_queue: str = "octop-longterm"
) -> str:
    identity = {"tenant_id": tenant_id, "task_id": task_id}
    handle = await client.start_workflow(
        "octop.longterm.goal",
        identity,
        id=workflow_id(tenant_id, task_id),
        task_queue=task_queue,
    )
    return str(handle.id)


async def wake_task(client: Any, *, tenant_id: str, task_id: str) -> None:
    handle = client.get_workflow_handle(workflow_id(tenant_id, task_id))
    await handle.signal("wake")


async def worker_is_polling(
    client: Any, *, namespace: str, task_queue: str, local_only: bool = True
) -> bool:
    """Check activity pollers, restricting worker health to this host by default."""
    queue_api = _sdk("temporalio.api.taskqueue.v1")
    service_api = _sdk("temporalio.api.workflowservice.v1")
    enum_api = _sdk("temporalio.api.enums.v1")
    response = await client.workflow_service.describe_task_queue(
        service_api.DescribeTaskQueueRequest(
            namespace=namespace,
            task_queue=queue_api.TaskQueue(name=task_queue),
            task_queue_type=enum_api.TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        )
    )
    if not local_only:
        return bool(response.pollers)
    hostname = socket.gethostname()
    return any(poller.identity.endswith("@" + hostname) for poller in response.pollers)


def workflow_runner() -> Any:
    sandbox = _sdk("temporalio.worker.workflow_sandbox")
    # Octop's package initializer installs an idempotent httpx proxy patch.
    # These imports perform no workflow I/O and must not be reloaded in the sandbox.
    restrictions = sandbox.SandboxRestrictions.default.with_passthrough_modules(
        "httpx", "octop.infra.utils.httpx_proxy"
    )
    return sandbox.SandboxedWorkflowRunner(restrictions=restrictions)


async def run_worker(
    step: ServiceStep,
    *,
    address: str = "localhost:7233",
    namespace: str = "default",
    task_queue: str = "octop-longterm",
    tls: bool = False,
    api_key: str | None = None,
) -> None:
    sdk_activity = _sdk("temporalio.activity")
    worker_type = _sdk("temporalio.worker").Worker
    definition = _sdk("octop.infra.longterm.temporal_workflow").LongTermTaskWorkflow

    async def advance(identity: dict[str, str]) -> dict[str, Any]:
        async def heartbeat() -> None:
            while True:
                sdk_activity.heartbeat()
                await asyncio.sleep(15)

        pulse = asyncio.create_task(heartbeat())
        try:
            return await step(identity)
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse

    advance_activity = sdk_activity.defn(name="octop.longterm.step")(advance)
    client = await connect(address=address, namespace=namespace, tls=tls, api_key=api_key)
    worker = worker_type(
        client,
        task_queue=task_queue,
        workflows=[definition],
        activities=[advance_activity],
        workflow_runner=workflow_runner(),
    )
    await worker.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Octop long-term Temporal worker")
    parser.add_argument(
        "--factory", required=True, help="module:factory returning an async service step"
    )
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--task-queue", default="octop-longterm")
    args = parser.parse_args()
    module_name, separator, factory_name = args.factory.partition(":")
    if not separator:
        parser.error("--factory must be module:factory")
    factory = getattr(importlib.import_module(module_name), factory_name)
    step = factory()
    asyncio.run(
        run_worker(step, address=args.address, namespace=args.namespace, task_queue=args.task_queue)
    )


if __name__ == "__main__":
    main()
