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
    layer_ms: dict[str, int] = None

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
            from core.rag.rag_service import RAGService
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
            from core.rag.static_validator import StaticValidator
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
        skip_justification: bool = False,
        job_id: Optional[str] = None,
        is_byos_session: bool = False
    ) -> PreflightResult:
        """
        Execute the full preflight pipeline.

        Stage 0:  Static domain heuristics (zero LLM, zero latency)
        Phase 1a: HTTP reachability verification
        Phase 1b: Preflight Oracle (LLM — only when Stage 0 returns UNKNOWN)
        Layer 1:  RAG memory check (pgvector)
        Layer 2:  Recipe generation (planner)
        Layer 3:  Static validation
        Layer 4:  Dynamic justification (browser)
        """
        start_time = time.time()
        layer_ms = {}
        warnings = []

        # ----------------------------------------------------------------
        # STAGE 0: Static Domain Heuristics (Math-First, Zero LLM)
        # ----------------------------------------------------------------
        from core.rag.domain_heuristics import evaluate_feasibility, FeasibilityVerdict

        stage0_start = time.time()
        static_result = evaluate_feasibility(url, prompt)
        layer_ms["static_heuristic_ms"] = int((time.time() - stage0_start) * 1000)

        logger.info(
            f"[{job_id}] Stage 0 heuristic: verdict={static_result.verdict.value}, "
            f"category={static_result.category}, reason={static_result.reason[:80]}"
        )

        if static_result.verdict == FeasibilityVerdict.BLOCKED:
            # Hard block — no LLM override, no BYOS bypass
            return PreflightResult(
                success=False,
                recipe=None,
                source="static_heuristic",
                warnings=[f"Blocked by static policy: {static_result.reason}"],
                total_ms=int((time.time() - start_time) * 1000),
                layer_ms=layer_ms
            )

        if static_result.verdict == FeasibilityVerdict.BYOS_REQUIRED and not is_byos_session:
            # BYOS needed but no session provided
            return PreflightResult(
                success=False,
                recipe=None,
                source="static_heuristic",
                warnings=[f"Authentication required: {static_result.reason}"],
                total_ms=int((time.time() - start_time) * 1000),
                layer_ms=layer_ms
            )

        # ALLOWED or BYOS_REQUIRED+is_byos_session — propagate metadata as warnings/hints
        if static_result.is_api_friendly:
            warnings.append(f"Site is API-friendly — Network Sniffer may capture direct data.")
        if static_result.requires_auth and is_byos_session:
            warnings.append(f"Auth-gated domain {static_result.category} — BYOS session active.")

        if is_byos_session and static_result.verdict == FeasibilityVerdict.ALLOWED:
            logger.info(f"[{job_id}] BYOS session + ALLOWED domain — fast path, skip Oracle.")
            return PreflightResult(
                success=True,
                recipe=None,
                source="byos_bypass",
                warnings=warnings + ["BYOS Session: Oracle feasibility check skipped (static allowed)."],
                total_ms=int((time.time() - start_time) * 1000),
                layer_ms=layer_ms
            )

        if is_byos_session and static_result.verdict == FeasibilityVerdict.BYOS_REQUIRED:
            logger.info(f"[{job_id}] BYOS session satisfies auth requirement — fast path.")
            return PreflightResult(
                success=True,
                recipe=None,
                source="byos_bypass",
                warnings=warnings + ["BYOS Session override: Oracle feasibility check bypassed."],
                total_ms=int((time.time() - start_time) * 1000),
                layer_ms=layer_ms
            )

        # ---------------------------------------------------------------------
        # PHASE 1: HTTP Verification
        # ---------------------------------------------------------------------
        logger.info(f"[{job_id}] Phase 1: HTTP Verification for {url}")
        verify_start = time.time()
        import httpx
        try:
            # Mimic residential browser to bypass simple WAFs on GET
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Upgrade-Insecure-Requests": "1"
            }
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()

                # FIX: 200 OK Honeypot Check (Text-Signature Matching)
                body_text = resp.text.lower()
                waf_signatures = [
                    "cf-browser-verification",
                    "window.__cf_chl_opt",
                    "window.datadomeoptions",
                    "challenge-platform",
                    "just a moment..."
                ]
                for sig in waf_signatures:
                    if sig in body_text:
                        logger.warning(f"[{job_id}] WAF Signature Detected in 200 OK response: {sig}")
                        return PreflightResult(
                            success=False, recipe=None, source="http_verification",
                            warnings=[f"URL served a 200 OK honeypot (WAF signature '{sig}' detected)."],
                            total_ms=int((time.time() - start_time) * 1000), layer_ms=layer_ms
                        )

            layer_ms["http_ms"] = int((time.time() - verify_start) * 1000)
        except httpx.HTTPStatusError as e:
            # 403s are common for WAFs blocking simple HTTP GETs. Let Phase 2 Oracle attempt it.
            logger.warning(f"[{job_id}] HTTP Verification Warning: {e.response.status_code}")
            warnings.append(f"HTTP Warning: {e.response.status_code}. Site might be blocking basic GET requests.")
            layer_ms["http_ms"] = int((time.time() - verify_start) * 1000)
        except Exception as e:
            # WAFs frequently tarpit/timeout basic HTTP clients. Let Phase 2 handle the final verdict.
            logger.warning(f"[{job_id}] HTTP Verification Tarpit/Timeout: {e}")
            warnings.append(f"Verification Tarpit: {str(e)[:100]}")
            layer_ms["http_ms"] = int((time.time() - verify_start) * 1000)

        # ---------------------------------------------------------------------
        # PHASE 1: Preflight Oracle (Latency Optimized 3-in-1)
        # Cache: domain + intent_category key, 24h TTL (1h for BLOCKED).
        # BYOS sessions always bypass cache — their auth state can change.
        # ---------------------------------------------------------------------
        logger.info(f"[{job_id}] Phase 1: Preflight Oracle Validation")
        oracle_start = time.time()
        classification = None

        # Determine intent category for cache key (use first word of prompt)
        _intent_cat = (prompt.split()[0] if prompt else "general").lower()[:32]
        _cache_domain = url.split("//")[-1].split("/")[0].replace("www.", "").split("?")[0]

        # Cache lookup (skip if BYOS — session state changes)
        from core.rag.preflight_cache import get_preflight_cache
        _pcache = get_preflight_cache()
        _cached_oracle = None if is_byos_session else _pcache.get(_cache_domain, _intent_cat)

        if _cached_oracle is not None:
            oracle_data = _cached_oracle
            layer_ms["oracle_ms"] = 0  # Cache hit — zero LLM cost
            logger.info(f"[{job_id}] Oracle CACHE HIT for ({_cache_domain}, {_intent_cat})")
        else:
            oracle_data = {"is_possible": True}  # Default: allow

        try:
            from core.llm.safe_client import SafeLLMClient
            from core.rag.prompts import PREFLIGHT_ORACLE_PROMPT
            import json

            if _cached_oracle is None:
                llm = SafeLLMClient()
                oracle_raw = await llm.call(PREFLIGHT_ORACLE_PROMPT, f"URL: {url}\nIntent: {prompt}", temperature=0.0)

                # Extract and parse JSON
                start_idx = oracle_raw.find('{')
                end_idx = oracle_raw.rfind('}')
                oracle_data = json.loads(oracle_raw[start_idx:end_idx+1])

                # Store result in cache (unconditionally — BLOCKED entries have shorter TTL)
                _pcache.set(_cache_domain, _intent_cat, oracle_data)
                layer_ms["oracle_ms"] = int((time.time() - oracle_start) * 1000)

            is_possible = oracle_data.get("is_possible", True)
            if not is_possible:
                # is_byos_session is the correct parameter name — was previously
                # referenced as the undefined `byos_active` which caused a NameError.
                if is_byos_session:
                    logger.warning(
                        f"[{job_id}] Oracle rejected intent but BYOS session is active "
                        "— overriding feasibility gate"
                    )
                    warnings.append("Oracle feasibility overridden by BYOS session")
                else:
                    reason = oracle_data.get("reasoning", "Unknown logical constraint")
                    return PreflightResult(
                        success=False, recipe=None, source="oracle_gatekeeper",
                        warnings=[f"Feasibility Failure: {reason}"],
                        total_ms=int((time.time() - start_time) * 1000), layer_ms=layer_ms
                    )

            # Store oracle findings for downstream consumption
            auth_required = oracle_data.get("auth_required", False)
            site_category = oracle_data.get("site_category", "portal")
            site_complexity = oracle_data.get("complexity", "Medium")

            # Check Auth Session if required
            if auth_required:
                from core.account_manager import AccountManager
                from urllib.parse import urlparse
                domain = urlparse(url).netloc.replace("www.", "")
                try:
                    am = AccountManager()
                    with am.engine.connect() as conn:
                        from sqlalchemy import text
                        count = conn.execute(text("SELECT COUNT(*) FROM auth_accounts WHERE domain = :domain"), {"domain": domain}).scalar()
                        if count == 0:
                            warnings.append(f"Persistence Gap: {domain} requires auth, but no accounts found in DB.")
                except Exception as e:
                    logger.warning(f"[{job_id}] Auth DB check failed: {e}")

            # Assign classification for Planner
            from core.rag.classifier import ClassificationResult
            classification = ClassificationResult(
                category=site_category,
                platform="Detecting...", # Placeholder for justifier
                complexity=site_complexity,
                confidence=0.90,
                features={"auth_required": auth_required}
            )

            layer_ms["oracle_ms"] = int((time.time() - oracle_start) * 1000)
        except Exception as e:
            logger.error(f"[{job_id}] Oracle Error: {e}")
            warnings.append(f"Oracle degradation: {str(e)[:50]}")

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
        # LAYER 1.5: Classification (For context) — ONLY if Oracle didn't set it
        # ---------------------------------------------------------------------
        if classification is None:
            try:
                classification = await self.classifier.classify(url)
                logger.info(f"[Preflight] Classified as: {classification.category}")
            except Exception as e:
                logger.warning(f"[Preflight] Classification failed: {e}")
        else:
            logger.info(f"[Preflight] Using Oracle classification: {classification.category}")

        # ---------------------------------------------------------------------
        # LAYER 2: Generate Soft Recipe (Planner AI)
        # ---------------------------------------------------------------------
        logger.info("[Preflight] Layer 2: Generating soft recipe")
        layer_start = time.time()

        try:
            soft_recipe = await self._generate_recipe(prompt, url, classification, job_id=job_id)
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
            # RecipeValidationError is raised on schema/logic failures —
            # treat as warnings, not a crash, so justification can still run
            logger.warning(f"[Preflight] Static validation failed: {e}")
            warnings.append(f"Validation warning: {str(e)[:100]}")
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
        similar_template: Dict = None,
        job_id: Optional[str] = None
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
            similar_template=similar_template,
            job_id=job_id
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
    job_id = payload.get("job_id")

    if not url or not prompt:
        return {
            "success": False,
            "error": "Missing required fields: url and prompt",
            "recipe": None
        }

    pipeline = PreflightPipeline()
    result = await pipeline.run(url, prompt, skip_justification, job_id=job_id)
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
