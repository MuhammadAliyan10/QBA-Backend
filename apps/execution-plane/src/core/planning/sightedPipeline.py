import logging
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright
from core.rag.planner import get_planner
from core.recipe.recipe_schema import Recipe
from core.recipe.recipe_engine import RecipeEngine

logger = logging.getLogger("sightedPipeline")

class SightedPipelineResult:
    def __init__(self, success: bool, status: str, epochs_run: int, extracted_data: dict, error: str = None):
        self.success = success
        self.status = status
        self.epochs_run = epochs_run
        self.extracted_data = extracted_data
        self.error = error

class SightedPipeline:
    def __init__(self):
        self.planner = get_planner()
        self.max_epochs = 5

    async def run(self, url: str, objective: str, headless: bool = True) -> SightedPipelineResult:
        logger.info(f"Starting JIT Sighted Pipeline for: {objective}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            
            # 1. Initial Plan Generation
            logger.info("Generating initial plan (Epoch 1)...")
            plan_res = await self.planner.generate(objective, url)
            if not plan_res.success:
                await browser.close()
                return SightedPipelineResult(False, "PLANNING_FAILED", 0, {}, plan_res.error)

            recipe_dict = plan_res.recipe
            extracted_data = {}
            history = []
            
            # Setup engine
            engine = RecipeEngine(job_id="jit_sighted_job")
            
            for epoch in range(1, self.max_epochs + 1):
                logger.info(f"--- Epoch {epoch} Execution ---")
                
                try:
                    await engine.load_recipe(recipe_dict)
                    
                    result = await engine.run(browser=browser)
                    status = result.get("status")
                    
                    if status == "completed":
                        logger.info("Execution completed successfully.")
                        extracted_data.update(result.get("context", {}))
                        await browser.close()
                        return SightedPipelineResult(True, "COMPLETED", epoch, extracted_data)
                    else:
                        # Execution failed
                        error_msg = result.get("error", "Unknown execution error")
                        logger.warning(f"Execution failed in epoch {epoch}: {error_msg}")
                        
                        executed = result.get("executed_nodes", [])
                        history.extend([f"Successfully ran node: {n}" for n in executed])
                        
                        current_url = result.get("failure_url", url)
                        if current_url == "unknown":
                            current_url = url
                            
                        failed_attempt = engine.ctx.current_node_id if engine.ctx else "Unknown Node"
                        
                        logger.info("Invoking LLM Re-Planner with failure context...")
                        # 2. Re-Plan from failure
                        replan_res = await self.planner.replan_from_failure(
                            prompt=objective,
                            url=current_url,
                            history=history[-5:],
                            failed_attempt=failed_attempt,
                            error=error_msg
                        )
                        
                        if not replan_res.success:
                            await browser.close()
                            return SightedPipelineResult(False, "REPLANNING_FAILED", epoch, extracted_data, replan_res.error)
                        
                        logger.info("Successfully generated new plan. Hot-swapping DAG...")
                        recipe_dict = replan_res.recipe
                        
                except Exception as e:
                    logger.error(f"Pipeline error: {e}")
                    await browser.close()
                    return SightedPipelineResult(False, "PIPELINE_ERROR", epoch, extracted_data, str(e))
            
            await browser.close()
            return SightedPipelineResult(False, "MAX_EPOCHS_REACHED", self.max_epochs, extracted_data, "Exceeded max replanning limit")
