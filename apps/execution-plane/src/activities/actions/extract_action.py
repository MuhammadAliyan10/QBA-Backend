import logging
from playwright.async_api import TimeoutError as PlaywrightTimeout
from .base_action import BaseAction
from ..context import ExecutionContext
from core.nervous_system import NervousSystem
from ..extraction import perform_extraction

logger = logging.getLogger("action.extract")

class ExtractAction(BaseAction):
    async def execute(self, ctx: ExecutionContext, payload: dict) -> dict:
        step_params = payload
        params = payload.get("_global_params", {})
        node_id = payload.get("_node_id", "unknown")

        # 1. Wait state for heavy background payloads to resolve (GraphQL)
        try:
            await ctx.page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightTimeout:
            logger.debug("Network idle timeout during extraction (expected for streaming sites)")

        # 2. Terminal Directive for LLM Override
        directive = "\nTERMINAL DIRECTIVE: If the GraphQL/JSON network payload is empty, and the DOM is heavily obfuscated (React/Atomic CSS), you MUST return a strict JSON null object. Do not guess. Do not extract navigation menus."
        step_params["intent"] = str(step_params.get("intent", "")) + directive

        await perform_extraction(
            ctx.page, 
            ctx.finder, 
            step_params, 
            params, 
            ctx.job_id, 
            node_id, 
            ctx.global_sniffer, 
            NervousSystem, 
            ctx.user_logger
        )
        return {"success": True}
