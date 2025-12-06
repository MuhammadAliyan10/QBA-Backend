from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
# DO NOT import activities here - it triggers ML library imports (sentence_transformers)
# Activities are referenced by string name and registered in worker.py


@workflow.defn
class BrowserWorkflow:
    """
    Browser Automation Workflow with Human-in-the-Loop Support.

    This workflow executes browser automation activities and can hibernate
    (zero CPU cost) when human intervention is required (CAPTCHA, 2FA, etc.).

    Signal Handling:
    - Listens for "USER_INTERACTION" signals via submit_user_input()
    - Uses wait_condition for zero-cost hibernation
    - Supports multiple interventions in a single workflow run
    """

    def __init__(self):
        self.user_input: dict | None = None

    @workflow.signal(name="USER_INTERACTION")
    async def submit_user_input(self, data: dict):
        """
        Signal handler for human input.

        Called when the /resume endpoint receives data from an external source.
        This unblocks the wait_condition and resumes workflow execution.

        Args:
            data (dict): User-provided data (e.g., {"decision": "yes", "otp": "123456"})
        """
        workflow.logger.info(f"📨 Signal Received: {data}")
        self.user_input = data

    @workflow.run
    async def run(self, payload: dict) -> dict:
        """
        Execute browser automation workflow with human intervention support.

        Flow:
        1. Execute activity
        2. If HumanInterventionRequired → Hibernate
        3. Wait for Signal (zero CPU)
        4. Resume with user input
        5. Retry activity with new data

        Args:
            payload (dict): Job configuration from control plane

        Returns:
            dict: Activity result
        """
        # Non-retryable error prevents Temporal from auto-retrying before workflow catches
        # Without this, activity would retry 3 times before bubbling up to workflow
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            non_retryable_error_types=["HumanInterventionRequired"]
        )

        # Loop to handle multiple interventions (e.g., CAPTCHA on page 1, then 2FA on page 2)
        while True:
            try:
                # Merge user input if available (from previous hibernation)
                if self.user_input:
                    payload["user_input"] = self.user_input
                    self.user_input = None  # Consumed

                # Execute browser automation activity
                # Use string name to avoid importing the activity module
                return await workflow.execute_activity(
                    "browser_automation_activity",  # String reference, not function
                    payload,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=retry_policy,
                )

            except ActivityError as e:
                # Unwrap the error - Temporal wraps activity exceptions in ActivityError
                # The original exception is in e.cause
                if isinstance(e.cause, ApplicationError) and e.cause.type == "HumanInterventionRequired":

                    workflow.logger.info(f"[Workflow] Human Intervention Triggered: {e.cause.message}")

                    # Optional: Fire a notification activity here
                    # await workflow.execute_activity(notify_user, {...})

                    # HIBERNATE (Zero CPU Cost)
                    # wait_condition uses event-driven blocking - no polling, no timers
                    # Worker process frees this workflow from memory until signal arrives
                    await workflow.wait_condition(
                        lambda: self.user_input is not None,
                        timeout=timedelta(hours=24)  # Max wait time
                    )

                    workflow.logger.info("[Workflow] Resuming with Human Signal...")
                    # Loop continues, re-executing activity with user input in payload
                    continue

                # If it's a real crash (not human intervention), re-raise it
                raise e
