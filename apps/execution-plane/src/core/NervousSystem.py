import time
import os
import logging
from nats.aio.client import Client as NATS
from api.gen.python.v1.events_pb2 import JobEvent  # Updated from StepUpdateEvent

logger = logging.getLogger("nervous_system")

class NervousSystem:
    _nc = None

    @classmethod
    async def get_nc(cls):
        if not cls._nc:
            cls._nc = NATS()
            nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
            await cls._nc.connect(nats_url)
        return cls._nc

    @classmethod
    async def publish_update(cls, job_id: str, status: str, message: str, node_id: str = "unknown", screenshot: bytes = b""):
        try:
            nc = await cls.get_nc()

            # Use new JobEvent message with updated field names
            event = JobEvent(
                job_id=job_id,
                status=status,
                message=message,  # Changed from log_message
                node_id=node_id,
                timestamp=int(time.time()),  # Added timestamp
                screenshot_preview=screenshot  # Changed from screenshot_url
            )

            # Serialize to Binary Protobuf
            data = event.SerializeToString()

            subject = f"job.update.{job_id}"
            await nc.publish(subject, data)
            logger.info(f"Published event to {subject}: {status}")

        except Exception as e:
            logger.error(f"Failed to publish to NATS: {e}")
