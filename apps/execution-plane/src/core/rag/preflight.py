"""
preflight.py - Preflight API Endpoint & Orchestrator

The Quality Control layer that converts "Soft Recipes" into "Hardened Recipes".

Tri-Layer Pipeline:
1. Memory Check (RAG) - Return verified template if found
2. Static Analysis - Validate JSON structure and logic
3. Dynamic Justification - Browser verification with Math+Vision

POST /api/engine/preflight
{
    "url": "https://example.com",
    "prompt": "Login and scrape the dashboard"
}

Author: Quanta Box Paradox Engineering
Version: 1.0.0
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("preflight")


# =============================================================================
# RESULT TYPES
# =============================================================================

@dataclass
class PreflightResult:
    """Result of the preflight pipeline."""
    success: bool
    recipe: Optional[Dict]
    source: str  # "memory", "generated", "patched"

    # Layer results
    memory_hit: bool = False
    memory_similarity: float = 0.0
    static_valid: bool = False
    justification_success: bool = False

    # Flags
    warnings: list = None
    calibration_needed: bool = False

    # Timing
    total_ms: int = 0
    layer_ms: Dict[str, int] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []
        if self.layer_ms is None:
            self.layer_ms = {}

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "recipe": self.recipe,
            "source": self.source,
            "meta": {
                "memory_hit": self.memory_hit,
                "memory_similarity": self.memory_similarity,
                "static_valid": self.static_valid,
                "justification_success": self.justification_success,
                "calibration_needed": self.calibration_needed,
                "warnings": self.warnings,
                "timing": {
                    "total_ms": self.total_ms,
                    **self.layer_ms
                }
            }
        }


# =============================================================================
# PREFLIGHT PIPELINE
# =============================================================================

class PreflightPipeline:
    """
    The Preflight Orchestrator.

    Flow:
    1. RAG.find_template() → If hit (>92% similarity), return immediately
    2. PlannerAI.generate() → Create soft recipe from prompt
    3. StaticValidator.validate() → Check logic without browser
    4. JustifierEngine.justify() → Browser verification
    5. Return hardened recipe
    """

    def __init__(self):
        # Lazy-load services
        self._rag = None
        self._classifier = None
        self._validator = None
        self._justifier = None

    @property
    def rag(self):
        if self._rag is None:
            from core.rag.ragService import RAGService
            self._rag = RAGService()
        return self._rag

    @property
    def classifier(self):
        if self._classifier is None:
            from core.rag.classifier import URLClassifier
            self._classifier = URLClassifier()
        return self._classifier

    @property
    def validator(self):
        if self._validator is None:
            from core.rag.staticValidator import StaticValidator
            self._validator = StaticValidator()
        return self._validator

    @property
    def justifier(self):
        if self._justifier is None:
            from core.rag.justifier import JustifierEngine
            self._justifier = JustifierEngine()
        return self._justifier

    async def run(
        self,
        url: str,
        prompt: str,
        skip_justification: bool = False
    ) -> PreflightResult:
        """
        Execute the full preflight pipeline.

        Args:
            url: Target URL
            prompt: User's task description
            skip_justification: Skip browser verification (faster but less safe)

        Returns:
            PreflightResult with hardened recipe
        """
        start_time = time.time()
        layer_ms = {}
        warnings = []

        # ---------------------------------------------------------------------
        # LAYER 1: Memory Check (RAG)
        # ---------------------------------------------------------------------
        logger.info(f"[Preflight] Layer 1: Memory Check for {url}")
        layer_start = time.time()

        try:
            template = await self.rag.find_template(prompt, url)
            layer_ms["memory_ms"] = int((time.time() - layer_start) * 1000)

            if template and template.is_high_confidence:
                logger.info(f"[Preflight] Memory HIT (similarity: {template.similarity:.2%})")
                return PreflightResult(
                    success=True,
                    recipe=template.recipe_json,
                    source="memory",
                    memory_hit=True,
                    memory_similarity=template.similarity,
                    static_valid=True,  # Already verified
                    justification_success=True,
                    total_ms=int((time.time() - start_time) * 1000),
                    layer_ms=layer_ms
                )

            logger.info("[Preflight] No high-confidence template found")

        except Exception as e:
            logger.warning(f"[Preflight] RAG failed: {e}")
            warnings.append(f"Memory check failed: {str(e)[:50]}")
            layer_ms["memory_ms"] = int((time.time() - layer_start) * 1000)

        # ---------------------------------------------------------------------
        # LAYER 1.5: Classification (For context)
        # ---------------------------------------------------------------------
        classification = None
        try:
            classification = await self.classifier.classify(url)
            logger.info(f"[Preflight] Classified as: {classification.category}")
        except Exception as e:
            logger.warning(f"[Preflight] Classification failed: {e}")

        # ---------------------------------------------------------------------
        # LAYER 2: Generate Soft Recipe (Planner AI)
        # ---------------------------------------------------------------------
        logger.info("[Preflight] Layer 2: Generating soft recipe")
        layer_start = time.time()

        try:
            soft_recipe = await self._generate_recipe(prompt, url, classification)
            layer_ms["generation_ms"] = int((time.time() - layer_start) * 1000)

            if not soft_recipe:
                return PreflightResult(
                    success=False,
                    recipe=None,
                    source="generated",
                    warnings=["Recipe generation failed"],
                    total_ms=int((time.time() - start_time) * 1000),
                    layer_ms=layer_ms
                )

        except Exception as e:
            logger.error(f"[Preflight] Generation failed: {e}")
            return PreflightResult(
                success=False,
                recipe=None,
                source="generated",
                warnings=[f"Generation failed: {str(e)[:100]}"],
                total_ms=int((time.time() - start_time) * 1000),
                layer_ms=layer_ms
            )

        # ---------------------------------------------------------------------
        # LAYER 3: Static Validation
        # ---------------------------------------------------------------------
        logger.info("[Preflight] Layer 3: Static validation")
        layer_start = time.time()

        static_valid = False
        try:
            result = self.validator.validate(soft_recipe)
            static_valid = result.is_valid
            layer_ms["static_ms"] = int((time.time() - layer_start) * 1000)

            if result.warnings:
                warnings.extend([w.message for w in result.warnings[:3]])

        except Exception as e:
            logger.warning(f"[Preflight] Static validation failed: {e}")
            warnings.append(f"Validation warning: {str(e)[:50]}")
            layer_ms["static_ms"] = int((time.time() - layer_start) * 1000)

        # ---------------------------------------------------------------------
        # LAYER 4: Dynamic Justification (Browser)
        # ---------------------------------------------------------------------
        hardened_recipe = soft_recipe
        justification_success = False
        calibration_needed = False

        if not skip_justification:
            logger.info("[Preflight] Layer 4: Dynamic justification")
            layer_start = time.time()

            try:
                justification = await self.justifier.justify_recipe(soft_recipe, url)
                layer_ms["justification_ms"] = int((time.time() - layer_start) * 1000)

                hardened_recipe = justification.patched_recipe
                justification_success = justification.success
                calibration_needed = justification.needs_calibration

                if justification.warning_flags:
                    warnings.extend(justification.warning_flags)

            except Exception as e:
                logger.error(f"[Preflight] Justification failed: {e}")
                warnings.append(f"Justification failed: {str(e)[:50]}")
                layer_ms["justification_ms"] = int((time.time() - layer_start) * 1000)
        else:
            logger.info("[Preflight] Skipping justification (fast mode)")
            layer_ms["justification_ms"] = 0

        total_ms = int((time.time() - start_time) * 1000)
        logger.info(f"[Preflight] Complete in {total_ms}ms")

        return PreflightResult(
            success=static_valid and (skip_justification or justification_success),
            recipe=hardened_recipe,
            source="patched" if justification_success else "generated",
            memory_hit=False,
            memory_similarity=0.0,
            static_valid=static_valid,
            justification_success=justification_success,
            calibration_needed=calibration_needed,
            warnings=warnings,
            total_ms=total_ms,
            layer_ms=layer_ms
        )

    async def _generate_recipe(
        self,
        prompt: str,
        url: str,
        classification = None,
        similar_template: Dict = None
    ) -> Optional[Dict]:
        """
        Generate a soft recipe from prompt using LLM Planner.

        NO MORE STUBS - This is the real deal!
        """
        from core.rag.planner import get_planner

        planner = get_planner()

        # Convert classification to dict if needed
        classification_dict = None
        if classification:
            classification_dict = classification.to_dict() if hasattr(classification, 'to_dict') else classification

        # Call the real planner
        result = await planner.generate(
            prompt=prompt,
            url=url,
            classification=classification_dict,
            similar_template=similar_template
        )

        if result.success:
            logger.info(f"[Preflight] Recipe generated ({result.tokens_used} tokens, {result.generation_ms}ms)")
            return result.recipe
        else:
            logger.error(f"[Preflight] Planner failed: {result.error}")
            return None


# =============================================================================
# API HANDLER
# =============================================================================

async def handle_preflight_request(payload: Dict) -> Dict:
    """
    Handle POST /api/engine/preflight

    Args:
        payload: {"url": "...", "prompt": "..."}

    Returns:
        PreflightResult as dict
    """
    url = payload.get("url")
    prompt = payload.get("prompt")
    skip_justification = payload.get("skip_justification", False)

    if not url or not prompt:
        return {
            "success": False,
            "error": "Missing required fields: url and prompt",
            "recipe": None
        }

    pipeline = PreflightPipeline()
    result = await pipeline.run(url, prompt, skip_justification)
    return result.to_dict()


# =============================================================================
# FLASK/FASTAPI ROUTE (Example)
# =============================================================================

# If using Flask:
#
# from flask import Blueprint, request, jsonify
#
# preflight_bp = Blueprint("preflight", __name__)
#
# @preflight_bp.route("/api/engine/preflight", methods=["POST"])
# async def preflight_endpoint():
#     payload = request.get_json()
#     result = await handle_preflight_request(payload)
#     return jsonify(result)

# If using FastAPI:
#
# from fastapi import APIRouter
#
# router = APIRouter()
#
# @router.post("/api/engine/preflight")
# async def preflight_endpoint(payload: dict):
#     return await handle_preflight_request(payload)
