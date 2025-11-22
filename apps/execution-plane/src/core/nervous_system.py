import os
import json
import nats
from nats.js.api import RetentionPolicy


class NervousSystem:
    _nc = None
    _js = None

    @classmethod
    async def get_instance(cls):
        if not cls._nc:
            nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
            # Connect to NATS
            cls._nc = await nats.connect(nats_url)
            cls._js = cls._nc.jetstream()
            print(f"✅ Connected to NATS at {nats_url}")

            # --- AUTO-CREATE STREAM (The Fix) ---
            # We define a stream named "JOBS" that listens to "job.>" subjects.
            # This acts as the bucket for all job-related events.
            try:
                await cls._js.add_stream(
                    name="JOBS",
                    subjects=["job.>"],  # Catches job.update.*, job.request, etc.
                    retention=RetentionPolicy.WORK_QUEUE,  # or LIMITS
                )
                print("✅ Stream 'JOBS' ensured.")
            except Exception as e:
                # If stream already exists, it might throw an error, which is fine.
                # But we print it just in case it's a different error.
                if "stream name already in use" not in str(e):
                    print(f"⚠️ Warning during stream creation: {e}")

        return cls._js

    @staticmethod
    async def publish_update(job_id: str, node_id: str, status: str, message: str):
        # Import here to avoid circular imports or path issues at module level if possible, 
        # but better to import at top if sys.path is fixed. 
        # Assuming sys.path is fixed in worker.py
        from api.gen.python.v1.events_pb2 import StepUpdateEvent

        js = await NervousSystem.get_instance()
        
        event = StepUpdateEvent(
            job_id=job_id,
            node_id=node_id,
            status=status,
            log_message=message
        )
        
        payload = event.SerializeToString()

        # Publish to the specific subject
        # NATS JetStream requires an existing stream to catch this.
        await js.publish(f"job.update.{job_id}", payload)
