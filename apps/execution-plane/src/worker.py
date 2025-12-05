import sys
import asyncio
import os
import signal
import logging

# --- 1. PATH SETUP (Critical for Monorepo) ---
# Add repo root to allow importing 'api.gen...'
current_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.abspath(os.path.join(current_dir, "../../../"))
sys.path.append(repo_root)
sys.path.append(current_dir)

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.opentelemetry import TracingInterceptor
from dotenv import load_dotenv

# Import the Business Logic
from workflows import BrowserWorkflow
from activities.activities import browser_automation_activity
from telemetry import init_telemetry, shutdown_telemetry

# --- 2. LOGGING CONFIG ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("e2e_worker")

# --- 3. SHUTDOWN LOGIC ---
interrupt_event = asyncio.Event()
worker_task = None

def signal_handler(sig, frame):
    logger.warning(f"🛑 Signal {sig} received. Forcing shutdown...")
    interrupt_event.set()
    # Force exit after 2 seconds if graceful shutdown fails
    import threading
    def force_exit():
        import time
        time.sleep(2)
        logger.error("⚠️ Graceful shutdown failed. Force exiting...")
        os._exit(0)
    threading.Thread(target=force_exit, daemon=True).start()

async def main():
    global worker_task

    # Load Environment Variables
    load_dotenv(os.path.join(repo_root, ".env"))

    temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    task_queue = "e2e-browser-tasks"

    logger.info(f"🚀 Starting Worker connecting to {temporal_host}")

    # Initialize Tracing (Optional but recommended)
    tracer_provider, meter_provider = init_telemetry("execution-plane")
    interceptors = []
    if tracer_provider:
        interceptors.append(TracingInterceptor())

    try:
        # 4. CONNECT TO TEMPORAL
        client = await Client.connect(
            temporal_host,
            interceptors=interceptors
        )
        logger.info("✅ Connected to Temporal Cluster")

        # 5. REGISTER WORKER
        # This tells Temporal: "I can handle 'BrowserWorkflow' and 'browser_automation_activity'"
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[BrowserWorkflow],
            activities=[browser_automation_activity],
        )

        logger.info(f"👂 Listening on queue: '{task_queue}'")
        logger.info("   Press Ctrl+C to stop.")

        # 6. RUN WITH INTERRUPT HANDLING
        async def run_worker():
            try:
                await worker.run()
            except asyncio.CancelledError:
                logger.info("Worker task cancelled")
                raise

        # Create worker task
        worker_task = asyncio.create_task(run_worker())

        # Wait for either worker completion or interrupt
        interrupt_task = asyncio.create_task(interrupt_event.wait())

        done, pending = await asyncio.wait(
            [worker_task, interrupt_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel all pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info("👋 Worker shutdown complete.")

    except asyncio.CancelledError:
        logger.info("🛑 Worker cancelled.")
    except Exception as e:
        logger.error(f"❌ Critical Worker Error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        shutdown_telemetry(tracer_provider, meter_provider)
        # Cancel any remaining tasks
        if worker_task and not worker_task.done():
            worker_task.cancel()

if __name__ == "__main__":
    # Register OS Signals (Docker/Kubernetes use SIGTERM)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 KeyboardInterrupt received. Exiting...")
        sys.exit(0)
