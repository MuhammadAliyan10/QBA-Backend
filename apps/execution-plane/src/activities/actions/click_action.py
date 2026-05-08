import asyncio
import logging
from .base_action import BaseAction
from ..context import ExecutionContext
from core.nervous_system import NervousSystem
from exceptions import HumanInterventionRequired
from ..navigation import safe_wait_for_network_idle

logger = logging.getLogger("action.click")

class ClickAction(BaseAction):
    async def execute(self, ctx: ExecutionContext, payload: dict) -> dict:
        intent = payload["intent"]
        job_id = ctx.job_id
        finder = ctx.finder
        page = ctx.page
        user_logger = ctx.user_logger

        if intent == "simulate_human_check":
            raise HumanInterventionRequired(
                reason="GOD_MODE_CHECK",
                context={"msg": "System is healthy. Proceed?"}
            )

        find_result = await finder.find(
            intent,
            metadata=payload.get("metadata"),
            container_selector=payload.get("container")
        )

        if not find_result.found:
            raise Exception(f"Element not found: {intent}")

        if find_result.needs_healing and find_result.new_signature:
            logger.info(f"[{job_id}] 🩹 Healing recipe for '{intent}'")
            await finder.vector_db.store(
                intent,
                find_result.new_signature.get("selector", "unknown"),
                find_result.new_signature.get("attributes")
            )
            await user_logger.info("GENERIC_ERROR", error_details=f"Self-healed selector for {intent}")

        element = find_result.element
        await element.scroll_into_view_if_needed()

        hydration_success = False
        for click_attempt in range(3):
            try:
                await element.click(timeout=5000)
                await asyncio.sleep(0.5)
                hydration_success = True
                break
            except Exception as e:
                logger.warning(f"[{job_id}] Click hydration failure (Attempt {click_attempt+1}/3). Retrying...")
                await asyncio.sleep(1.0)

        if not hydration_success:
            logger.error(f"[{job_id}] Element failed hydration after 3 attempts.")

        await safe_wait_for_network_idle(page)
        await asyncio.sleep(1.0)

        await user_logger.info("CLICKED_ELEMENT", element=intent)
        return {"success": True}
