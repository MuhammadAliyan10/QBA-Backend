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
    from activities.recipeActivity import run_recipe_execution
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
    from activities.recipeActivity import run_recipe_execution

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
