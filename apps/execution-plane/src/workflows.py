from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from activities.activities import browser_automation_activity
from api.gen.python.v1.workflow_pb2 import BrowserStepInput


@workflow.defn
class BrowserWorkflow:
    @workflow.run
    async def run(self, steps_data: list[dict]) -> str:
        results = []
        
        # Convert dicts back to BrowserStepInput objects
        steps = []
        for s in steps_data:
            steps.append(BrowserStepInput(
                job_id=s.get("job_id"),
                node_id=s.get("node_id"),
                action=s.get("action"),
                params=s.get("params")
            ))

        # Configure Activity Options
        # We define the policy here to ensure if the browser crashes, it retries automatically
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
        )

        for step in steps:
            # Execute Step
            # We pass the retry_policy to start_activity (or execute_activity)
            res = await workflow.execute_activity(
                browser_automation_activity,
                step,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            results.append(res)

        return "Workflow Complete"
