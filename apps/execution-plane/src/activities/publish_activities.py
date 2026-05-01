import logging
from temporalio import activity
from core.nervous_system import NervousSystem

logger = logging.getLogger("publish_activities")

@activity.defn(name="publish_event_activity")
async def publish_event_activity(payload: dict) -> dict:
    """
    Publish an event to the Nervous System.

    Args:
        payload: {
            "job_id": "gen-123",
            "status": "RUNNING",
            "message": "Step verified",
            "node_id": "node-1"
        }
    """
    job_id = payload.get("job_id", "unknown")
    status = payload.get("status", "RUNNING")
    message = payload.get("message", "")
    node_id = payload.get("node_id", "unknown")

    try:
        await NervousSystem.publish_update(
            job_id,
            status,
            message,
            node_id
        )
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to publish event: {e}")
        return {"success": False, "error": str(e)}
