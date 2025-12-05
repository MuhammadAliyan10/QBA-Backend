from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from activities.activities import browser_automation_activity

@workflow.defn
class BrowserWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        # payload is the dictionary sent from Go:
        # { "job_id": "...", "workflow_id": "...", "config": {...}, "params": {...} }

        # Retry Policy: If the browser crashes, try 3 times
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
        )

        # We pass the ENTIRE payload to the activity.
        # The Activity will manage opening/closing the browser.
        result = await workflow.execute_activity(
            browser_automation_activity,
            payload,
            start_to_close_timeout=timedelta(minutes=5), # Max job time
            retry_policy=retry_policy,
        )

        return result
