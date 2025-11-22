from datetime import timedelta
from temporalio import workflow
from activities.activities import browser_automation_activity, BrowserStepInput


@workflow.defn
class BrowserWorkflow:
    @workflow.run
    async def run(self, steps: list[BrowserStepInput]) -> str:
        results = []
        # Configure Activity Options (Timeouts are critical!)
        # If the browser hangs for > 30s, kill it.
        activity_config = workflow.start_activity(
            browser_automation_activity,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=workflow.RetryPolicy(maximum_attempts=3),
        )

        for step in steps:
            res = await workflow.execute_activity(
                browser_automation_activity,
                step,
                start_to_close_timeout=timedelta(seconds=30),
            )
            results.append(res)
        return "Workflow Complete"
