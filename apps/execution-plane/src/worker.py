import sys
import asyncio
import os
import signal
import logging

# Add repo root to sys.path to allow importing api.gen...
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")))
# Add src to sys.path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.contrib.opentelemetry import TracingInterceptor
from dotenv import load_dotenv

# Import our definitions
from workflows import BrowserWorkflow
from activities.activities import browser_automation_activity
from telemetry import init_telemetry, shutdown_telemetry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global shutdown event for graceful shutdown
shutdown_event = asyncio.Event()


def handle_shutdown(signum, frame):
    """
    Graceful shutdown handler.
    Lets the current workflow/activity finish before stopping.
    """
    logger.warning("🛑 Shutdown signal received (SIGTERM/SIGINT). Finishing current tasks...")
    shutdown_event.set()


# Register signal handlers
signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


async def main():
    tracer_provider = None
    meter_provider = None
    client = None
    
    try:
        # 1. Load Config
        load_dotenv("../../../.env")
        temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
        logger.info("🚀 Starting Python Execution Worker...")
        logger.info(f"   Temporal Host: {temporal_host}")
        
        # 2. Initialize OpenTelemetry
        tracer_provider, meter_provider = init_telemetry("execution-plane")
        
        # 3. Connect to Temporal Server
        interceptors = []
        if tracer_provider:
            interceptors.append(TracingInterceptor())
            logger.info("   OpenTelemetry tracing enabled")
        
        client = await Client.connect(
            temporal_host,
            interceptors=interceptors
        )
        logger.info("   Connected to Temporal")
        
        # 4. Create Worker
        # Queue Name must match what the Go API sends
        worker = Worker(
            client,
            task_queue="e2e-browser-tasks",
            workflows=[BrowserWorkflow],
            activities=[browser_automation_activity],
        )
        logger.info("✅ Worker Started! Waiting for jobs...")
        logger.info("   Task Queue: e2e-browser-tasks")
        
        # 5. Run Until Shutdown Signal
        await worker.run(shutdown_event=shutdown_event)
        
        logger.info("✅ Worker stopped gracefully. All tasks completed.")
        
    except Exception as e:
        logger.error(f"❌ Worker failed: {e}", exc_info=True)
        raise
    finally:
        # Cleanup
        if client:
            await client.close()
        shutdown_telemetry(tracer_provider, meter_provider)


if __name__ == "__main__":
    asyncio.run(main())
