import time
import os
import json
import logging
from nats.aio.client import Client as NATS
from api.gen.python.v1.events_pb2 import JobEvent

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
    async def publish(cls, subject: str, payload: str):
        """Generic JSON-over-NATS publisher for telemetry."""
        try:
            nc = await cls.get_nc()
            await nc.publish(subject, payload.encode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to publish to telemetry subject {subject}: {e}")

    @classmethod
    async def publish_update(cls, job_id: str, status: str, message: str, node_id: str = "unknown", data: str = "", screenshot: bytes = b""):
        """
        Dual-broadcast status updates:
        1. Protobuf to job.update.{job_id} (Internal status/metrics)
        2. JSON to quanta.telemetry.{job_id} (Consolidated SSE stream)
        """
        try:
            nc = await cls.get_nc()

            # 1. LEGACY/INTERNAL: Protobuf Broadcast
            event = JobEvent(
                job_id=job_id,
                status=status,
                message=message,
                node_id=node_id,
                timestamp=int(time.time()),
                screenshot_preview=screenshot,
                data=data
            )
            proto_data = event.SerializeToString()
            await nc.publish(f"job.update.{job_id}", proto_data)

            # 2. TELEMETRY: Mirror as JSON to SSE Stream
            telemetry_payload = json.dumps({
                "type": "log",
                "message": f"[{status}] {message}"
            })
            await cls.publish(f"quanta.telemetry.{job_id}", telemetry_payload)

            logger.info(f"Published event to job.update.{job_id}: {status}")

        except Exception as e:
            logger.error(f"Failed to publish status update: {e}")
