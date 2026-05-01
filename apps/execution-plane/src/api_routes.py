"""
api.py - FastAPI Routes for the Execution Plane

Exposes the Preflight Pipeline and Recipe Execution to the frontend.

POST /api/engine/preflight - Run the tri-layer preflight pipeline
POST /api/engine/execute - Execute a hardened recipe

Author: Quanta Box Paradox Engineering
Version: 1.0.0
"""

import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

logger = logging.getLogger("api")

# Create router
router = APIRouter(prefix="/api/engine", tags=["engine"])


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class PreflightRequest(BaseModel):
    """Request body for preflight endpoint."""
    url: str
    prompt: str
    skip_justification: bool = False


class PreflightResponse(BaseModel):
    """Response from preflight endpoint."""
    success: bool
    recipe: Optional[Dict[str, Any]] = None
    source: str = ""  # "memory", "generated", "patched"
    meta: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ExecuteRequest(BaseModel):
    """Request body for execute endpoint."""
    job_id: Optional[str] = None
    recipe: Dict[str, Any]
    params: Dict[str, Any] = {}
    config: Dict[str, Any] = {}


class ExecuteResponse(BaseModel):
    """Response from execute endpoint."""
    success: bool
    job_id: str
    status: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# =============================================================================
# PREFLIGHT ENDPOINT
# =============================================================================

@router.post("/preflight", response_model=PreflightResponse)
async def preflight_endpoint(request: PreflightRequest) -> Dict:
    """
    Run the Tri-Layer Preflight Pipeline.

    Flow:
    1. RAG Memory Check → Return verified template if found (>92% match)
    2. LLM Generation → Create soft recipe from prompt
    3. Static Validation → Check logic without browser
    4. Dynamic Justification → Browser verification with SmartFinder

    Returns hardened recipe ready for execution.
    """
    from core.rag.preflight import handle_preflight_request

    logger.info(f"[API] Preflight request: {request.url}")

    try:
        result = await handle_preflight_request({
            "url": request.url,
            "prompt": request.prompt,
            "skip_justification": request.skip_justification
        })

        return result

    except Exception as e:
        logger.error(f"[API] Preflight error: {e}")
        return {
            "success": False,
            "recipe": None,
            "source": "error",
            "error": str(e)[:200]
        }


# =============================================================================
# EXECUTE ENDPOINT (Direct execution, bypasses Temporal)
# =============================================================================

@router.post("/execute", response_model=ExecuteResponse)
async def execute_endpoint(request: ExecuteRequest) -> Dict:
    """
    Execute a hardened recipe directly.

    This is for testing/development. In production, use Temporal.
    """
    from activities.recipe_activity import run_recipe_execution
    import time

    job_id = request.job_id or f"job-{int(time.time())}"

    logger.info(f"[API] Execute request: {job_id}")

    try:
        result = await run_recipe_execution({
            "job_id": job_id,
            "recipe": request.recipe,
            "params": request.params,
            "config": request.config
        })

        return result

    except Exception as e:
        logger.error(f"[API] Execute error: {e}")
        return {
            "success": False,
            "job_id": job_id,
            "status": "FAILED",
            "error": str(e)[:200]
        }


# =============================================================================
# COMBINED ENDPOINT (Preflight + Execute in one call)
# =============================================================================

@router.post("/run")
async def run_endpoint(
    url: str,
    prompt: str,
    params: Dict[str, Any] = {},
    skip_preflight: bool = False
) -> Dict:
    """
    One-shot execution: Preflight → Execute.

    Convenience endpoint that runs the full pipeline.
    """
    import time

    job_id = f"job-{int(time.time())}"

    logger.info(f"[API] Run request: {url}")

    # Step 1: Preflight (unless skipped)
    recipe = None
    if not skip_preflight:
        from core.rag.preflight import handle_preflight_request

        preflight_result = await handle_preflight_request({
            "url": url,
            "prompt": prompt
        })

        if not preflight_result.get("success"):
            return {
                "success": False,
                "job_id": job_id,
                "stage": "preflight",
                "error": preflight_result.get("meta", {}).get("warnings", ["Preflight failed"])
            }

        recipe = preflight_result.get("recipe")

    # Step 2: Execute
    from activities.recipe_activity import run_recipe_execution

    result = await run_recipe_execution({
        "job_id": job_id,
        "recipe": recipe,
        "params": params,
        "config": {}
    })

    return {
        "success": result.get("success", False),
        "job_id": job_id,
        "stage": "execution",
        "data": result.get("data"),
        "error": result.get("error")
    }



# =============================================================================
# SIGHTED PIPELINE (Harvest → Plan → Execute) — Production API
# =============================================================================

class SightedRequest(BaseModel):
    """Request body for sighted pipeline endpoint."""
    url: str
    objective: str
    job_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    config: Optional[Dict[str, Any]] = None

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://github.com/trending",
                "objective": "Extract the top 10 trending repository names and star counts",
                "session_id": "ses_abc123",
                "config": {"headless": True, "timeout_ms": 120000}
            }
        }

class SightedResponse(BaseModel):
    """Response from the sighted pipeline."""
    success: bool
    job_id: str = ""
    status: str = ""                  # COMPLETED | FAILED | REJECTED | TIMEOUT
    rejection_reason: str = ""
    goals_planned: int = 0
    goals_completed: int = 0
    goals_failed: int = 0
    interactive_count: int = 0
    content_count: int = 0
    feasibility_map: Optional[Dict[str, bool]] = None
    extracted_data: Optional[Dict[str, Any]] = None
    harvest_duration_ms: int = 0
    planning_duration_ms: int = 0
    execution_duration_ms: int = 0
    total_duration_ms: int = 0
    error: Optional[str] = None


@router.post("/sighted", response_model=SightedResponse)
async def sighted_pipeline(request: SightedRequest) -> Dict:
    """
    Sighted Pipeline — Harvest-First, Plan-Second automation.

    This endpoint:
    1. Navigates to the URL and harvests ALL elements (interactive + content).
    2. Checks feasibility (rejects impossible tasks with clear error).
    3. Plans using real element context (no hallucination).
    4. Executes goals iteratively with re-harvest on page transitions.
    5. Returns extracted data and execution results.
    """
    import time
    import re

    # --- Input Validation ---
    url = (request.url or "").strip()
    objective = (request.objective or "").strip()

    if not url or not re.match(r'^https?://', url):
        raise HTTPException(status_code=400, detail="Invalid URL. Must be a valid HTTP(S) URL.")
    if len(objective) < 5:
        raise HTTPException(status_code=400, detail="Objective must be at least 5 characters.")
    if len(objective) > 2000:
        raise HTTPException(status_code=400, detail="Objective must be at most 2000 characters.")

    # --- Config ---
    config = request.config or {}
    headless = config.get("headless", True)
    proxy = config.get("proxy", None)
    timeout_ms = config.get("timeout_ms", 120000)

    job_id = request.job_id or f"sighted-{int(time.time())}"

    logger.info(f"[API] Sighted pipeline: {objective} @ {url}")

    from core.planning.sighted_pipeline import SightedPipeline
    import asyncio

    pipeline = SightedPipeline()

    try:
        result = await asyncio.wait_for(
            pipeline.run(
                url=url,
                objective=objective,
                job_id=job_id,
                user_id=request.user_id or "",
                session_id=request.session_id or "",
                headless=headless,
                proxy=proxy,
            ),
            timeout=timeout_ms / 1000.0,
        )
        return result.to_dict()

    except asyncio.TimeoutError:
        logger.error(f"[API] Sighted pipeline timed out after {timeout_ms}ms")
        return {
            "success": False,
            "job_id": job_id,
            "status": "TIMEOUT",
            "error": f"Execution timed out after {timeout_ms}ms",
            "total_duration_ms": timeout_ms,
        }

    except Exception as e:
        logger.error(f"[API] Sighted pipeline crash: {e}", exc_info=True)
        return {
            "success": False,
            "job_id": job_id,
            "status": "FAILED",
            "error": f"Internal pipeline error: {str(e)[:300]}",
        }


# =============================================================================
# HEALTH CHECK
# =============================================================================

@router.get("/health")
async def health_check() -> Dict:
    """Check service health."""
    from core.rag import get_rag_service

    health = {
        "status": "healthy",
        "services": {}
    }

    # Check RAG service
    try:
        rag = get_rag_service()
        health["services"]["rag"] = "connected" if rag else "unavailable"
    except Exception as e:
        health["services"]["rag"] = f"error: {str(e)[:50]}"
        health["status"] = "degraded"

    return health

