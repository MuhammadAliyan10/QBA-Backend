# activities/sightedActivity.py
"""
Temporal Activity for the Sighted Pipeline.

This is the Temporal-compatible wrapper around SightedPipeline.
It replaces the blind planning path in execute_recipe_activity for
the new Harvest-First, Plan-Second architecture.

Registration: Registered in worker.py as "sighted_execution_activity".
"""

import logging
import time
from typing import Dict, Any

from temporalio import activity

from core.planning.sighted_pipeline import SightedPipeline
from core.nervous_system import NervousSystem

logger = logging.getLogger("sightedActivity")


@activity.defn(name="sighted_execution_activity")
async def sighted_execution_activity(payload: dict) -> dict:
    """
    Execute a task using the Sighted Pipeline.

    This activity:
    1. Launches a browser
    2. Harvests the target page
    3. Plans with real element context
    4. Executes goals iteratively
    5. Returns extracted data

    Args:
        payload: {
            "job_id": "job-123",
            "target_url": "https://airbnb.com",
            "objective": "Search for apartments in Tokyo under $150",
            "config": {
                "headless": true,
                "proxy": null
            }
        }

    Returns:
        SightedPipelineResult as dict.
    """
    job_id = payload.get("job_id", f"job-{int(time.time())}")
    target_url = payload.get("target_url")
    objective = payload.get("objective")
    config = payload.get("config", {})

    if not target_url or not objective:
        logger.error(f"[{job_id}] Missing target_url or objective.")
        return {
            "success": False,
            "job_id": job_id,
            "status": "FAILED",
            "error": "Missing required fields: target_url, objective",
        }

    logger.info(f"[{job_id}] Starting sighted execution: {objective} @ {target_url}")

    await NervousSystem.publish_update(
        job_id, "RUNNING",
        f"[Sighted] Target: {target_url} | Objective: {objective[:80]}",
        "init",
    )

    pipeline = SightedPipeline()

    result = await pipeline.run(
        url=target_url,
        objective=objective,
        job_id=job_id,
        headless=config.get("headless", True),
        proxy=config.get("proxy"),
    )

    logger.info(
        f"[{job_id}] Sighted pipeline finished | "
        f"success={result.success} | {result.goals_completed}/{result.goals_planned} goals | "
        f"{result.total_duration_ms}ms"
    )

    return result.to_dict()
