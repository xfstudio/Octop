"""Temporal component availability differs from a specific worker's health."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from octop.infra.longterm import temporal_runtime, worker


@pytest.mark.parametrize(
    "identities,local_expected,global_expected",
    [
        pytest.param([], False, False, id="no-pollers"),
        pytest.param(["123@api-host"], True, True, id="same-host"),
        pytest.param(["123@worker-host"], False, True, id="different-container"),
        pytest.param(["123@not-api-host"], False, True, id="hostname-suffix-is-not-local"),
        pytest.param(["123@worker-host", "456@api-host"], True, True, id="multiple-workers"),
    ],
)
async def test_poller_scope_preserves_local_worker_health(
    monkeypatch: pytest.MonkeyPatch,
    identities: list[str],
    local_expected: bool,
    global_expected: bool,
) -> None:
    pytest.importorskip("temporalio")
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import PollerInfo
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse

    monkeypatch.setattr(temporal_runtime.socket, "gethostname", lambda: "api-host")
    describe = AsyncMock(
        return_value=DescribeTaskQueueResponse(
            pollers=[PollerInfo(identity=identity) for identity in identities]
        )
    )
    client = SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=describe))

    # The omitted option is intentional: the worker's existing health call must
    # continue to reject a healthy poller running in a different container.
    assert (
        await temporal_runtime.worker_is_polling(
            client, namespace="tenant-namespace", task_queue="longterm-queue"
        )
        is local_expected
    )
    assert (
        await temporal_runtime.worker_is_polling(
            client,
            namespace="tenant-namespace",
            task_queue="longterm-queue",
            local_only=False,
        )
        is global_expected
    )
    assert describe.await_count == 2
    for call in describe.await_args_list:
        request = call.args[0]
        assert request.namespace == "tenant-namespace"
        assert request.task_queue.name == "longterm-queue"
        assert request.task_queue_type == TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY


@pytest.mark.parametrize(
    "identities,healthy",
    [
        pytest.param(["123@worker-host"], True, id="own-worker-polling"),
        pytest.param(["123@another-worker-host"], False, id="only-remote-worker-polling"),
        pytest.param([], False, id="no-worker-polling"),
    ],
)
async def test_worker_healthcheck_requires_its_own_poller(
    monkeypatch: pytest.MonkeyPatch, identities: list[str], healthy: bool
) -> None:
    pytest.importorskip("temporalio")
    from temporalio.api.taskqueue.v1 import PollerInfo
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse

    monkeypatch.setattr(temporal_runtime.socket, "gethostname", lambda: "worker-host")
    client = SimpleNamespace(
        workflow_service=SimpleNamespace(
            describe_task_queue=AsyncMock(
                return_value=DescribeTaskQueueResponse(
                    pollers=[PollerInfo(identity=identity) for identity in identities]
                )
            )
        )
    )
    database = SimpleNamespace(close=Mock())
    service = SimpleNamespace(
        components=SimpleNamespace(
            require_enabled=Mock(),
            resolve=Mock(
                return_value={
                    "address": "temporal:7233",
                    "namespace": "default",
                    "task_queue": "longterm-queue",
                    "tls": False,
                    "api_key": "",
                }
            ),
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(worker, "_open_service", lambda: (database, service))
    monkeypatch.setattr(worker, "connect", AsyncMock(return_value=client))

    if healthy:
        await worker.healthcheck()
    else:
        with pytest.raises(RuntimeError, match="no active Temporal poller for this worker host"):
            await worker.healthcheck()

    service.close.assert_awaited_once()
    database.close.assert_called_once()
