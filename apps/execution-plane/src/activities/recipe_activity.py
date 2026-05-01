"""
recipeActivity.py - Temporal Activity for Recipe Execution

THE BRIDGE: Connects Preflight (Schema v2.0) → RecipeEngine → Execution

This replaces the old step-based loop in activities.py.
Now activities.py should import from here for recipe execution.

Author: Quanta Box Paradox Engineering
Version: 2.0.0
"""

import os
import asyncio
import logging
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass
from temporalio import activity
from playwright.async_api import async_playwright

# Core imports
from core.nervous_system import NervousSystem
from core.recipe.recipe_engine import RecipeEngine

logger = logging.getLogger("recipeActivity")


# =============================================================================
# RESULT TYPE
# =============================================================================

@dataclass
class RecipeExecutionResult:
    """Result of recipe execution."""
    success: bool
    job_id: str
    status: str  # "COMPLETED", "FAILED", "TIMEOUT"
    nodes_executed: int = 0
    duration_ms: int = 0
    data: Dict[str, Any] = None
    error: Optional[str] = None
    checkpoint_id: Optional[str] = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "job_id": self.job_id,
            "status": self.status,
            "nodes_executed": self.nodes_executed,
            "duration_ms": self.duration_ms,
            "data": self.data,
            "error": self.error,
            "checkpoint_id": self.checkpoint_id
        }


# =============================================================================
# RECIPE EXECUTION ACTIVITY
# =============================================================================

@activity.defn(name="execute_recipe_activity")
async def execute_recipe_activity(payload: dict) -> dict:
    """
    Execute a Recipe Schema v2.0 using RecipeEngine.

    THE UNIFIED EXECUTION PATH:
    - Consumes hardened recipes from Preflight
    - Uses RecipeEngine for DAG execution
    - Triggers RAG learning on success

    Args:
        payload: {
            "job_id": "job-123",
            "recipe": { Recipe Schema v2.0 JSON },
            "params": { User inputs },
            "config": { Execution config }
        }

    Returns:
        RecipeExecutionResult as dict
    """
    start_time = time.time()

    # Unpack payload
    job_id = payload.get("job_id", f"job-{int(time.time())}")
    recipe = payload.get("recipe")
    params = payload.get("params", {})
    config = payload.get("config", {})

    # --- AUTONOMOUS RECOVERY: NO RECIPE PROVIDED ---
    if not recipe:
        target_url = payload.get("target_url")
        objective = payload.get("objective")

        if not target_url or not objective:
            logger.error(f"[{job_id}] No recipe AND no target_url/objective provided")
            return RecipeExecutionResult(
                success=False, job_id=job_id, status="FAILED", error="Missing instructions"
            ).to_dict()

        logger.info(f"[{job_id}] No recipe found. Invoking Autonomous Preflight for objective: {objective}")
        await NervousSystem.publish_update(job_id, "RUNNING", "Generating autonomous plan...", "init")

        try:
            from core.rag.preflight import PreflightPipeline
            pipeline = PreflightPipeline()
            preflight_result = await pipeline.run(target_url, objective, skip_justification=True, job_id=job_id)

            if preflight_result.success and preflight_result.recipe:
                recipe = preflight_result.recipe
                logger.info(f"[{job_id}] Autonomously generated recipe for {target_url}")
                await NervousSystem.publish_update(job_id, "RUNNING", "Autonomous plan generated", "init")
            else:
                err = f"Autonomous planning failed: {getattr(preflight_result, 'errors', 'Unknown error')}"
                logger.error(f"[{job_id}] {err}")
                await NervousSystem.publish_update(job_id, "FAILED", err, "init")
                return RecipeExecutionResult(success=False, job_id=job_id, status="FAILED", error=err).to_dict()
        except Exception as e:
            err = f"Preflight crashed: {str(e)}"
            logger.error(f"[{job_id}] {err}", exc_info=True)
            await NervousSystem.publish_update(job_id, "FAILED", err, "init")
            return RecipeExecutionResult(success=False, job_id=job_id, status="FAILED", error=err).to_dict()

    # Notify start
    await NervousSystem.publish_update(
        job_id, "RUNNING",
        f"[Engine] Starting recipe: {recipe.get('metadata', {}).get('name', 'Unknown')}",
        "init"
    )

    browser = None
    engine = None

    try:
        # =====================================================================
        # 1. INITIALIZE BROWSER
        # =====================================================================
        async with async_playwright() as p:
            launch_args = {
                "headless": config.get("headless", True),
                "args": ["--no-sandbox", "--disable-setuid-sandbox"]
            }

            # Proxy support
            if config.get("proxy"):
                launch_args["proxy"] = config["proxy"]

            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )
            page = await context.new_page()

            # =====================================================================
            # 2. INITIALIZE RECIPE ENGINE
            # =====================================================================
            engine = RecipeEngine(job_id=job_id)

            # Load the recipe (validates Schema v2.0 internally)
            # This initializes engine.ctx
            await engine.load_recipe(recipe)

            # Inject user params into context
            if params and engine.ctx:
                for key, value in params.items():
                    engine.ctx.set(f"inputs.{key}", value)

            await NervousSystem.publish_update(
                job_id, "RUNNING",
                f"[Engine] Recipe loaded: {len(recipe.get('nodes', []))} nodes",
                "init"
            )

            # =====================================================================
            # 3. EXECUTE RECIPE (DAG Traversal)
            # =====================================================================
            # We let the engine manage the browser context for full diagnostic capture
            result = await engine.run(
                browser=browser,
                inputs=params,
                secrets=payload.get("secrets", {})
            )

            duration_ms = int((time.time() - start_time) * 1000)

            if result.status.value == "completed":
                # =====================================================================
                # 4. LEARNING HOOK: Save successful recipe to RAG
                # =====================================================================
                try:
                    from core.rag import get_rag_service
                    from urllib.parse import urlparse

                    # Extract domain from first navigate action
                    domain = "unknown"
                    for node in recipe.get("nodes", []):
                        for action in node.get("actions", []):
                            if action.get("type") == "navigate" and action.get("url"):
                                domain = urlparse(action["url"]).netloc.replace("www.", "")
                                break
                        if domain != "unknown":
                            break

                    await get_rag_service().save_template(
                        recipe_json=recipe,
                        category=config.get("category", "general"),
                        domain=domain,
                        task_type=recipe.get("metadata", {}).get("name", "automation"),
                        description=recipe.get("metadata", {}).get("description", "")
                    )

                    logger.info(f"[{job_id}] Recipe saved to RAG memory")

                except Exception as e:
                    logger.warning(f"[{job_id}] Failed to save to RAG: {e}")

                # Success notification
                await NervousSystem.publish_update(
                    job_id, "COMPLETED",
                    f"[Engine] Recipe completed in {duration_ms}ms",
                    "end"
                )

                return RecipeExecutionResult(
                    success=True,
                    job_id=job_id,
                    status="COMPLETED",
                    nodes_executed=result.nodes_executed if hasattr(result, 'nodes_executed') else 0,
                    duration_ms=duration_ms,
                    data=result.data if hasattr(result, 'data') else {}
                ).to_dict()

            else:
                # Execution failed
                await NervousSystem.publish_update(
                    job_id, "FAILED",
                    f"[Engine] Recipe failed: {result.error or 'Unknown error'}",
                    "error"
                )

                return RecipeExecutionResult(
                    success=False,
                    job_id=job_id,
                    status="FAILED",
                    duration_ms=duration_ms,
                    error=result.error
                ).to_dict()

    except Exception as e:
        logger.error(f"[{job_id}] Execution error: {e}", exc_info=True)

        duration_ms = int((time.time() - start_time) * 1000)

        # Capture screenshot on failure
        screenshot = None
        try:
            if engine and engine.context and engine.context.page:
                screenshot = await engine.context.page.screenshot(type='jpeg', quality=60)
        except:
            pass

        await NervousSystem.publish_update(
            job_id, "FAILED",
            f"[Engine] Critical error: {str(e)[:100]}",
            "error",
            screenshot=screenshot
        )

        return RecipeExecutionResult(
            success=False,
            job_id=job_id,
            status="FAILED",
            duration_ms=duration_ms,
            error=str(e)[:500]
        ).to_dict()

    finally:
        # Cleanup
        if browser:
            try:
                await browser.close()
            except:
                pass


# =============================================================================
# LEGACY COMPATIBILITY WRAPPER
# =============================================================================

async def run_recipe_execution(payload: dict) -> dict:
    """
    Non-Temporal wrapper for recipe execution.
    Useful for testing and direct calls.
    """
    return await execute_recipe_activity(payload)
