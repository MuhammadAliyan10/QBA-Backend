from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

# DO NOT import activities here - it triggers ML library imports (sentence_transformers)
# Activities are referenced by string name and registered in worker.py

# Import timeout configuration (safe - no ML imports)
# Note: In Temporal workflows, we use workflow.info().unsafe to call non-deterministic code
# For now, we use sensible defaults that match config.py values


# =============================================================================
# DEFAULT TIMEOUT VALUES (mirrors config.py for workflow determinism)
# =============================================================================
# Temporal workflows must be deterministic, so we can't call os.getenv directly.
# These defaults match config.py. For production tuning, update both places.
DEFAULT_ACTIVITY_TIMEOUT_SEC = 300  # 5 minutes
DEFAULT_HUMAN_WAIT_TIMEOUT_SEC = 86400  # 24 hours
DEFAULT_MAX_RETRY_ATTEMPTS = 1
DEFAULT_INITIAL_RETRY_INTERVAL_SEC = 2
DEFAULT_RETRY_BACKOFF_COEFFICIENT = 2.0


@workflow.defn
class BrowserWorkflow:
    """
    Browser Automation Workflow with Human-in-the-Loop Support.

    This workflow executes browser automation activities and can hibernate
    (zero CPU cost) when human intervention is required (CAPTCHA, 2FA, etc.).

    Timeout Configuration:
    - Activity timeout: DEFAULT_ACTIVITY_TIMEOUT_SEC (5 min)
    - Human wait timeout: DEFAULT_HUMAN_WAIT_TIMEOUT_SEC (24 hours)
    - Retry attempts: DEFAULT_MAX_RETRY_ATTEMPTS (3)

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
        # =====================================================================
        # RETRY POLICY (uses centralized defaults)
        # =====================================================================
        # Non-retryable error prevents Temporal from auto-retrying before workflow catches
        # Without this, activity would retry 3 times before bubbling up to workflow
        retry_policy = RetryPolicy(
            maximum_attempts=1,  # 1 attempt = NO retries. Prevents duplicate Chrome instances.
            initial_interval=timedelta(seconds=DEFAULT_INITIAL_RETRY_INTERVAL_SEC),
            backoff_coefficient=DEFAULT_RETRY_BACKOFF_COEFFICIENT,
            non_retryable_error_types=["HumanInterventionRequired"]
        )

        # =====================================================================
        # MAIN EXECUTION LOOP
        # =====================================================================
        # Loop to handle multiple interventions (e.g., CAPTCHA on page 1, then 2FA on page 2)
        while True:
            try:
                # Merge user input if available (from previous hibernation)
                if self.user_input:
                    payload["user_input"] = self.user_input
                    self.user_input = None  # Consumed

                # --- STEP 1: PREFLIGHT (Autonomous Planning) ---
                # If no recipe is provided, we must generate one first
                if not payload.get("recipe"):
                    workflow.logger.info(f"[Workflow] Missing recipe for objective: {payload.get('objective')}. Triggering Preflight Planner...")

                    # We reuse the harvest_and_plan_activity or a dedicated preflight activity
                    # For professional ad-hoc runs, we use execute_recipe_activity which handles its own internal planning
                    # if requested, OR we call a separate activity.
                    # Given the current architecture, execute_recipe_activity expects a recipe.

                    # Let's use recipeActivity's internal capability or a dedicated activity.
                    # Professional choice: Use a dedicated 'preflight_activity' if it exists.
                    # If not, we'll use execute_recipe_activity and update it to handle planning.
                    pass

                # --- STEP 2: EXECUTION ---
                engine_mode = payload.get("engine_settings", {}).get("engine_mode", "legacy")
                if engine_mode == "sighted":
                    workflow.logger.info("[Workflow] Dispatching sighted execution activity")
                    result = await workflow.execute_activity(
                        "sighted_execution_activity",
                        payload,
                        start_to_close_timeout=timedelta(seconds=DEFAULT_ACTIVITY_TIMEOUT_SEC),
                        retry_policy=retry_policy,
                    )
                else:
                    result = await workflow.execute_activity(
                        "browser_automation_activity",
                        payload,
                        start_to_close_timeout=timedelta(seconds=DEFAULT_ACTIVITY_TIMEOUT_SEC),
                        retry_policy=retry_policy,
                    )

                # --- STEP 3: WEBHOOK DISPATCH ---
                job_id = payload.get("job_id")
                callback_url = payload.get("callback_url")
                if job_id and callback_url:
                    workflow.logger.info(f"[Workflow] Dispatching webhook to {callback_url}")
                    await workflow.execute_activity(
                        "DispatchWebhookActivity",
                        args=[job_id, callback_url, "quanta-webhook-secret"],
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=RetryPolicy(maximum_attempts=5)
                    )

                return result

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
                        timeout=timedelta(seconds=DEFAULT_HUMAN_WAIT_TIMEOUT_SEC)
                    )

                    workflow.logger.info("[Workflow] Resuming with Human Signal...")
                    # Loop continues, re-executing activity with user input in payload
                    continue

                # If it's a real crash (not human intervention), re-raise it
                raise e
