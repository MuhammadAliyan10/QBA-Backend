import asyncio
import os
from temporalio.client import Client
from temporalio.worker import Worker
from dotenv import load_dotenv


# Import our definitions
from workflows import BrowserWorkflow
from activities.activities import browser_automation_activity


async def main():
    # 1. Load Config
    load_dotenv("../../../.env")
    temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    print("Starting Python Worker...")
    print(f"   Target: {temporal_host}")

    # 2. Connect to Temporal Server
    client = await Client.connect(temporal_host)

    # 3. Create Worker
    # Queue Name must match what the Go API sends
    worker = Worker(
        client,
        task_queue="e2e-browser-tasks",
        workflows=[BrowserWorkflow],
        activities=[browser_automation_activity],
    )
    print("Worker Started! Waiting for jobs...")

    # 4. Run Forever
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
