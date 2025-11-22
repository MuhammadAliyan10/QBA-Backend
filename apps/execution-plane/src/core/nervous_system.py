import os
import json
import nats


class NervousSystem:
    _nc = None
    _js = None

    @classmethod
    async def get_instance(cls):
        if not cls._nc:
            nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
            cls._nc = await nats.connect(nats_url)
            cls._js = cls._nc.jetstream()
            print("Connected to NATS server at", nats_url)
        return cls._js

    @staticmethod
    async def publish_update(job_id: str, node_id: str, status: str, message: str):
        js = await NervousSystem.get_instance()
        payload = json.dumps(
            {
                "job_id": job_id,
                "node_id": node_id,
                "status": status,
                "log_message": message,
            }
        ).encode()
        await js.publish(f"job.update.{job_id}", payload)
