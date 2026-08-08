import sys
import asyncio
import os
import logging
from datetime import timedelta  # CRITICAL: Must be imported before Worker initialization

# --- 1. PATH SETUP ---
current_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(repo_root)
sys.path.append(current_dir)

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.opentelemetry import TracingInterceptor
from dotenv import load_dotenv

# Import Business Logic
from workflows import BrowserWorkflow, GenerateWorkflowRecipe
from activities.core_workflow import browser_automation_activity
from activities.discovery_activities import (
    harvest_and_plan_activity,
    execute_action_activity,
    cleanup_browser_activity
)
from activities.healing_activities import (
    validateRequestActivity,
    generateWorkflowMapActivity,
    executeWorkflowStrictlyActivity,
    healWorkflowActivity
)
from activities.hybrid_activities import (
    generateIntentSequenceActivity,
    executeHybridWorkflowActivity
)
from activities.recipe_activity import execute_recipe_activity
from activities.publish_activities import publish_event_activity
from activities.sighted_activity import sighted_execution_activity
from activities.executeUniversalAgent import execute_universal_agent
from telemetry import init_telemetry, shutdown_telemetry

# --- 2. LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("e2e_worker")

async def main():
    # Load Environment
    load_dotenv(os.path.join(repo_root, ".env"))
    temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    task_queue = "e2e-browser-tasks"

    logger.info(f"[System] Worker process started, connecting to {temporal_host}")

    # Telemetry
    tracer_provider, meter_provider = init_telemetry("execution-plane")
    interceptors = []
    if tracer_provider:
        interceptors.append(TracingInterceptor())

    try:
        # 3. CONNECT
        client = await Client.connect(temporal_host, interceptors=interceptors)
        logger.info("[System] Successfully connected to Temporal cluster")

        # 4. CREATE WORKER
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[
                BrowserWorkflow,
                GenerateWorkflowRecipe  # NEW: Verified workflow generation
            ],
            activities=[
                browser_automation_activity,
                # Math-First generation activities
                harvest_and_plan_activity,    # One-shot DOM harvest + intent plan (replaces ReAct loop)
                execute_action_activity,
                cleanup_browser_activity,
                # Event publishing (notify frontend of step results)
                publish_event_activity,

                # Strict Assertion & Self-Healing Layer
                validateRequestActivity,
                generateWorkflowMapActivity,
                executeWorkflowStrictlyActivity,
                healWorkflowActivity,

                # Hybrid Agentic DOM-Walker Layer
                generateIntentSequenceActivity,
                executeHybridWorkflowActivity,

                # Unified Recipe Engine
                execute_recipe_activity,

                # Sighted Pipeline (Harvest → Plan → Execute)
                sighted_execution_activity,

                # Two-Phase Cognitive Orchestration
                execute_universal_agent,
            ],
            # Concurrency limits — each browser activity consumes ~200MB RAM + 1 CPU core.
            # 10 concurrent activities matches the container's 2GB shm_size budget.
            # Scale horizontally (more replicas) rather than increasing this.
            max_concurrent_activities=int(os.getenv("WORKER_MAX_CONCURRENT_ACTIVITIES", "10")),
            max_concurrent_workflow_tasks=int(os.getenv("WORKER_MAX_CONCURRENT_WORKFLOWS", "20")),
            # CRITICAL: Allow enough time for Browser to close gracefully
            graceful_shutdown_timeout=timedelta(seconds=15)
        )


        logger.info(f"[Worker] Listening on queue: '{task_queue}'")
        logger.info("[Worker] Press Ctrl+C to stop.")

        # 5. RUN (BLOCKING)
        # The SDK automatically handles SIGINT/SIGTERM here.
        # It will wait for 'graceful_shutdown_timeout' before killing activities.
        await worker.run()

        logger.info("[System] Worker shutdown complete.")

    except asyncio.CancelledError:
        logger.info("[System] Worker cancelled.")
    except Exception as e:
        logger.error(f"[System] Critical worker error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        shutdown_telemetry(tracer_provider, meter_provider)

if __name__ == "__main__":
    # timedelta already imported at module level
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # This catch is just to suppress the ugly Traceback on Ctrl+C
        pass
