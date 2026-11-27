"""First-party Temporal scheduling on a queue separate from uploaded workflows."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, time, timedelta

from temporalio import activity
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleUpdate,
)
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from . import dreams
from .clients import temporal
from .config import settings
from .dream_temporal import ACTIVITY, SCHEDULE_PREFIX, WORKFLOW, NightlyDream

log = logging.getLogger(__name__)


def task_queue() -> str:
    return f"{settings.temporal_task_queue}.system"


@activity.defn(name=ACTIVITY)
async def reflect_agent(agent_id: str, schedule_revision: int, scheduled_at: str) -> str:
    try:
        return await dreams.reflect(agent_id, schedule_revision, datetime.fromisoformat(scheduled_at))
    except Exception:
        # Temporal retains activity failures. Never serialize a provider response,
        # prompt, private source, or database error with parameters into history.
        raise ApplicationError("Could not update the agent dream") from None


async def reconcile(client: Client) -> None:
    desired = {SCHEDULE_PREFIX + row["agent_id"]: row for row in dreams.scheduled_agents()}
    # Existing schedules can have operator pauses (notably staging refresh).
    # Reconciliation owns recurrence and action, never an operator's pause.
    for schedule_id, row in desired.items():
        action = ScheduleActionStartWorkflow(
            WORKFLOW,
            args=[row["agent_id"], row["schedule_revision"]],
            id=schedule_id,
            task_queue=task_queue(),
            execution_timeout=timedelta(minutes=15),
        )
        at = time.fromisoformat(row["at"])
        spec = ScheduleSpec(
            calendars=[
                ScheduleCalendarSpec(hour=(ScheduleRange(at.hour),), minute=(ScheduleRange(at.minute),))
            ],
            time_zone_name=row["timezone"],
        )
        managed = Schedule(
            action=action,
            spec=spec,
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(hours=6)),
        )
        handle = client.get_schedule_handle(schedule_id)

        async def update(current, *, wanted=managed):
            existing = current.description.schedule
            if (
                existing.spec == wanted.spec
                and existing.policy == wanted.policy
                and isinstance(existing.action, ScheduleActionStartWorkflow)
                and existing.action.workflow == wanted.action.workflow
                and existing.action.id == wanted.action.id
                and existing.action.task_queue == wanted.action.task_queue
                and existing.action.execution_timeout == wanted.action.execution_timeout
                and await current.description.data_converter.decode(existing.action.args)
                == list(wanted.action.args)
            ):
                return None
            return ScheduleUpdate(schedule=replace(wanted, state=existing.state))

        try:
            await handle.update(update, rpc_timeout=timedelta(seconds=10))
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            try:
                await client.create_schedule(schedule_id, managed, rpc_timeout=timedelta(seconds=10))
            except ScheduleAlreadyRunningError:
                pass  # Another backend won creation; the next reconciliation checks it.
    async for listed in await client.list_schedules(rpc_timeout=timedelta(seconds=10)):
        if listed.id.startswith(SCHEDULE_PREFIX) and listed.id not in desired:
            try:
                await client.get_schedule_handle(listed.id).delete(rpc_timeout=timedelta(seconds=10))
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise


async def _reconcile_loop(client: Client) -> None:
    while True:
        try:
            await reconcile(client)
        except Exception:
            log.warning("Dream schedule reconciliation failed; will retry")
        await asyncio.sleep(30)


async def serve() -> None:
    """System worker runs in the backend, where private chat and model access belong."""
    while True:
        try:
            async with asyncio.timeout(15):
                client = await temporal.client()
            async with (
                Worker(
                    client,
                    task_queue=task_queue(),
                    workflows=[NightlyDream],
                    activities=[reflect_agent],
                    max_concurrent_activities=2,
                ),
                asyncio.TaskGroup() as tasks,
            ):
                tasks.create_task(_reconcile_loop(client))
                await asyncio.Future()
        except Exception:
            log.warning("Dream system worker is unavailable; will retry")
            await asyncio.sleep(15)
