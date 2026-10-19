"""Temporal definitions. Imported only after the optional SDK is requested.

The workflow owns durable waiting; the activity calls the same leased service
step as the local dispatcher. No provider or database I/O runs in workflow code.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn(name="octop.longterm.goal")
class LongTermTaskWorkflow:
    def __init__(self) -> None:
        self._wake_sequence = 0
        self._status = "starting"

    @workflow.signal
    def wake(self) -> None:
        """Commands are stored in Octop first; this only wakes the durable wait."""
        self._wake_sequence += 1

    @workflow.query
    def status(self) -> str:
        return self._status

    def _wake_condition(self, sequence: int) -> Callable[[], bool]:
        def woken() -> bool:
            return self._wake_sequence > sequence

        return woken

    @workflow.run
    async def run(self, identity: dict[str, str]) -> dict[str, Any]:
        if not identity.get("tenant_id") or not identity.get("task_id"):
            raise ValueError("tenant_id and task_id are required")
        for _ in range(100):
            sequence = self._wake_sequence
            try:
                result = await workflow.execute_activity(
                    "octop.longterm.step",
                    identity,
                    start_to_close_timeout=timedelta(days=7),
                    heartbeat_timeout=timedelta(seconds=90),
                    # The original activity never retries an unknown write.
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                # A lost worker can leave SQL running after Temporal times out.
                # Re-enter the leased service, not the original external call:
                # it reconstructs attempts/budgets and stops unresolved writes.
                self._status = "recovering"
                with suppress(TimeoutError):
                    await workflow.wait_condition(
                        self._wake_condition(sequence), timeout=timedelta(seconds=10)
                    )
                continue
            self._status = result["status"]
            if self._status in {"completed", "failed", "cancelled", "uncertain"}:
                return dict(result)
            if self._status in {"paused", "waiting_approval", "draft"}:
                await workflow.wait_condition(self._wake_condition(sequence))
            elif self._status == "sleeping":
                raw = result.get("checkpoint", {}).get("sleep_until")
                if not isinstance(raw, str):
                    raise ValueError("sleeping activity must return sleep_until")
                wake_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                delay = wake_at - workflow.now()
                if delay.total_seconds() > 0:
                    with suppress(TimeoutError):
                        await workflow.wait_condition(self._wake_condition(sequence), timeout=delay)
            elif self._status not in {"queued", "running"}:
                raise ValueError("unknown long-term task activity status")
        # Bound workflow history while keeping authoritative checkpoints in Octop.
        workflow.continue_as_new(identity)
