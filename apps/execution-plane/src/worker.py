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
from activities.activities import browser_automation_activity
from activities.discoveryActivities import (
    plan_recipe_from_prompt,
    discover_element_activity,
    generate_react_flow_node
)
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
                # NEW: Discovery activities for verified generation
                plan_recipe_from_prompt,
                discover_element_activity,
                generate_react_flow_node
            ],
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
