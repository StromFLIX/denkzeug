"""Replay-safe definition of the first-party nightly dream workflow."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ApplicationError

WORKFLOW = "denkzeug.system.agent_dream"
ACTIVITY = "denkzeug.system.reflect_agent"
SCHEDULE_PREFIX = "denkzeug-system-dream-"
_SCHEDULED_AT = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")
_SCHEDULED_BY = SearchAttributeKey.for_keyword("TemporalScheduledById")


@workflow.defn(name=WORKFLOW)
class NightlyDream:
    @workflow.run
    async def run(self, agent_id: str, schedule_revision: int) -> str:
        attributes = workflow.info().typed_search_attributes
        scheduled_at = attributes.get(_SCHEDULED_AT)
        if scheduled_at is None or attributes.get(_SCHEDULED_BY) != SCHEDULE_PREFIX + agent_id:
            raise ApplicationError("Dreaming must be started by its system schedule", non_retryable=True)
        return await workflow.execute_activity(
            ACTIVITY,
            args=[agent_id, schedule_revision, scheduled_at.isoformat()],
            result_type=str,
            start_to_close_timeout=timedelta(seconds=120),
            schedule_to_close_timeout=timedelta(minutes=12),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=20), maximum_attempts=3),
        )
