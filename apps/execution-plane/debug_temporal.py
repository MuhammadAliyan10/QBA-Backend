import asyncio
from temporalio.client import Client

async def main():
    client = await Client.connect("localhost:7233")
    workflow_id = "32129de4-0197-4384-860c-5839ae202401"
    run_id = "019ef38c-458b-70a7-8fad-2fa136bef1e4"
    
    try:
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        desc = await handle.describe()
        print(f"Status: {desc.status}")
        
        hist = await handle.fetch_history()
        for event in hist.events:
            print(f"Event: {event.event_type}")
            if hasattr(event, 'workflow_execution_failed_event_attributes'):
                print("Failed info:", event.workflow_execution_failed_event_attributes)
            if hasattr(event, 'workflow_task_failed_event_attributes'):
                print("Task failed info:", event.workflow_task_failed_event_attributes)
            
    except Exception as e:
        print("Error fetching:", e)

if __name__ == "__main__":
    asyncio.run(main())
