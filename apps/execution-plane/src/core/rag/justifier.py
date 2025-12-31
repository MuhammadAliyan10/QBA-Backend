"""
justifier.py - Dynamic Recipe Justification Engine (REFACTORED)

Layer 3 of the Preflight Pipeline - NOW USES SMARTFINDER!

Key Changes from v1:
- Uses SmartFinder 4-layer system (no reinventing Levenshtein)
- Integrates with existing selector infrastructure
- Cleaner separation of concerns

Author: Quanta Box Paradox Engineering
Version: 2.0.0
"""

import os
import asyncio
import logging
import time
import json
import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

from playwright.async_api import async_playwright, Page, Browser, BrowserContext

logger = logging.getLogger("justifier")


# =============================================================================
# CONSTANTS
# =============================================================================

PREFLIGHT_TIMEOUT_SECONDS = 30
MIN_CONFIDENCE_THRESHOLD = 0.80

# Dangerous action types - NEVER execute in preflight (READ-ONLY)
DANGEROUS_INTENTS = {
    "submit", "buy", "purchase", "checkout", "delete", "remove",
    "confirm order", "pay now", "transfer", "send money", "place order",
    "confirm payment", "complete purchase"
}


# =============================================================================
# DATA CLASSES
# =============================================================================

class VerificationStatus(str, Enum):
    VERIFIED = "verified"              # SmartFinder found it (high confidence)
    VISION_VERIFIED = "vision_verified"  # Vision fallback found it
    CALIBRATION_NEEDED = "calibration_needed"  # Needs human review
    SKIPPED = "skipped"                # Non-browser action


@dataclass
class ElementVerification:
    """Result of verifying a single element."""
    node_id: str
    action_index: int
    intent: str
    status: VerificationStatus
    confidence: float = 0.0
    verified_selector: Optional[str] = None
    layer_used: Optional[str] = None  # "REFLEX", "HEURISTIC", "SEMANTIC", "COGNITIVE"
    vision_coordinates: Optional[Tuple[int, int]] = None
    duration_ms: int = 0
    error: Optional[str] = None


@dataclass
class JustificationResult:
    """Result of justifying an entire recipe."""
    success: bool
    patched_recipe: Dict
    verifications: List[ElementVerification] = field(default_factory=list)
    duration_ms: int = 0
    warning_flags: List[str] = field(default_factory=list)

    @property
    def verified_count(self) -> int:
        return sum(1 for v in self.verifications
                   if v.status in (VerificationStatus.VERIFIED, VerificationStatus.VISION_VERIFIED))

    @property
    def needs_calibration(self) -> bool:
        return any(v.status == VerificationStatus.CALIBRATION_NEEDED for v in self.verifications)


# =============================================================================
# JUSTIFIER ENGINE
# =============================================================================

class JustifierEngine:
    """
    Dynamic Recipe Justification Engine - USES SMARTFINDER.

    Flow:
    1. Launch headless browser (stealth mode)
    2. Navigate to target URL
    3. For each browser action node:
       - Use SmartFinder.find() with 4-layer fallback
       - If found: Patch recipe with verified selector
       - If not found: Mark for calibration
    4. Return patched recipe

    SAFETY: READ-ONLY verification. No dangerous button clicks.
    """

    def __init__(self, vision_api_key: str = None):
        """Initialize Justifier."""
        self.vision_api_key = vision_api_key or os.getenv("OPENAI_API_KEY")
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._smart_finder = None

    async def justify_recipe(
        self,
        recipe: Dict,
        url: str,
        timeout_seconds: int = PREFLIGHT_TIMEOUT_SECONDS
    ) -> JustificationResult:
        """
        Justify and patch a recipe using SmartFinder.

        Args:
            recipe: Recipe JSON (Schema v2.0)
            url: Target URL to navigate to
            timeout_seconds: Hard timeout limit

        Returns:
            JustificationResult with patched recipe
        """
        start_time = time.time()
        verifications: List[ElementVerification] = []
        warning_flags: List[str] = []

        # Deep copy recipe for patching
        import copy
        patched_recipe = copy.deepcopy(recipe)

        try:
            async with asyncio.timeout(timeout_seconds):
                await self._start_browser()

                # Navigate to target URL
                logger.info(f"[Justifier] Navigating to: {url}")
                await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)  # Let page settle

                # Initialize SmartFinder with page
                from core.selector.smartFinder import SmartFinder
                self._smart_finder = SmartFinder(self._page)

                # Verify each node's actions
                for node_idx, node in enumerate(patched_recipe.get("nodes", [])):
                    if node.get("type") != "action":
                        continue

                    node_id = node.get("id", f"node_{node_idx}")

                    for action_idx, action in enumerate(node.get("actions", [])):
                        verification = await self._verify_action(
                            action=action,
                            action_index=action_idx,
                            node_id=node_id
                        )
                        verifications.append(verification)

                        # Patch recipe with verified selector
                        if verification.verified_selector:
                            patched_recipe["nodes"][node_idx]["actions"][action_idx]["_verified_selector"] = verification.verified_selector
                            patched_recipe["nodes"][node_idx]["actions"][action_idx]["_verification_status"] = verification.status.value
                            patched_recipe["nodes"][node_idx]["actions"][action_idx]["_verification_layer"] = verification.layer_used
                            patched_recipe["nodes"][node_idx]["actions"][action_idx]["_verification_confidence"] = verification.confidence

                        if verification.status == VerificationStatus.CALIBRATION_NEEDED:
                            warning_flags.append(f"Node '{node_id}' action {action_idx} ({verification.intent}) needs calibration")

        except asyncio.TimeoutError:
            logger.warning(f"[Justifier] Timeout after {timeout_seconds}s")
            warning_flags.append(f"Preflight timed out after {timeout_seconds}s - partial verification")

        except Exception as e:
            logger.error(f"[Justifier] Error: {e}")
            warning_flags.append(f"Preflight error: {str(e)[:100]}")

        finally:
            await self._cleanup()

        duration_ms = int((time.time() - start_time) * 1000)

        logger.info(f"[Justifier] Complete: {len(verifications)} actions verified in {duration_ms}ms")

        return JustificationResult(
            success=len(warning_flags) == 0,
            patched_recipe=patched_recipe,
            verifications=verifications,
            duration_ms=duration_ms,
            warning_flags=warning_flags
        )

    # -------------------------------------------------------------------------
    # BROWSER MANAGEMENT
    # -------------------------------------------------------------------------

    async def _start_browser(self):
        """Start headless browser in stealth mode."""
        playwright = await async_playwright().start()

        self._browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox"
            ]
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        self._page = await self._context.new_page()

        # Stealth injection
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        logger.info("[Justifier] Browser started in stealth mode")

    async def _cleanup(self):
        """Clean up browser resources."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
        except:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._smart_finder = None

    # -------------------------------------------------------------------------
    # ACTION VERIFICATION (USES SMARTFINDER!)
    # -------------------------------------------------------------------------

    async def _verify_action(
        self,
        action: Dict,
        action_index: int,
        node_id: str
    ) -> ElementVerification:
        """
        Verify a single action's element using SmartFinder.

        This is the KEY REFACTOR - we now use SmartFinder's 4-layer
        fallback instead of reimplementing Levenshtein.
        """
        action_type = action.get("type", "")
        intent = action.get("intent", "")

        # Skip non-element actions
        if action_type in ("navigate", "wait", "set_context", "log", "screenshot", "wait_for_load_state"):
            return ElementVerification(
                node_id=node_id,
                action_index=action_index,
                intent=intent or action_type,
                status=VerificationStatus.SKIPPED
            )

        # Safety check: Skip dangerous actions
        intent_lower = intent.lower() if intent else ""
        if any(danger in intent_lower for danger in DANGEROUS_INTENTS):
            logger.warning(f"[Justifier] Skipping dangerous intent: {intent}")
            return ElementVerification(
                node_id=node_id,
                action_index=action_index,
                intent=intent,
                status=VerificationStatus.SKIPPED,
                error="Dangerous action - skipped in preflight"
            )

        if not intent:
            return ElementVerification(
                node_id=node_id,
                action_index=action_index,
                intent="",
                status=VerificationStatus.SKIPPED
            )

        logger.debug(f"[Justifier] Verifying: '{intent}'")
        start_time = time.time()

        # =====================================================
        # USE SMARTFINDER - THE KEY REFACTOR!
        # =====================================================
        try:
            # Get metadata from action (if any)
            metadata = action.get("metadata", {})

            # Call SmartFinder with 4-layer fallback
            result = await self._smart_finder.find(intent, metadata)

            duration_ms = int((time.time() - start_time) * 1000)

            if result.found and result.confidence >= MIN_CONFIDENCE_THRESHOLD:
                # Get selector from element
                selector = await self._extract_selector(result.element)

                logger.info(f"[Justifier] Verified '{intent}' via Layer {result.layer.value} ({result.confidence:.0%})")

                return ElementVerification(
                    node_id=node_id,
                    action_index=action_index,
                    intent=intent,
                    status=VerificationStatus.VERIFIED,
                    confidence=result.confidence,
                    verified_selector=selector,
                    layer_used=result.layer.name,
                    duration_ms=duration_ms
                )

            elif result.found:
                # Found but low confidence - try vision fallback
                logger.info(f"[Justifier] Low confidence ({result.confidence:.0%}), trying vision...")

                vision_result = await self._verify_with_vision(intent)

                if vision_result:
                    selector, coords = vision_result
                    return ElementVerification(
                        node_id=node_id,
                        action_index=action_index,
                        intent=intent,
                        status=VerificationStatus.VISION_VERIFIED,
                        confidence=0.85,
                        verified_selector=selector,
                        vision_coordinates=coords,
                        layer_used="VISION",
                        duration_ms=int((time.time() - start_time) * 1000)
                    )

            # Not found - needs calibration
            logger.warning(f"[Justifier] Could not verify: '{intent}'")
            return ElementVerification(
                node_id=node_id,
                action_index=action_index,
                intent=intent,
                status=VerificationStatus.CALIBRATION_NEEDED,
                confidence=result.confidence if result else 0.0,
                error=result.error if result else "Element not found",
                duration_ms=duration_ms
            )

        except Exception as e:
            logger.error(f"[Justifier] SmartFinder error: {e}")
            return ElementVerification(
                node_id=node_id,
                action_index=action_index,
                intent=intent,
                status=VerificationStatus.CALIBRATION_NEEDED,
                error=str(e)[:100],
                duration_ms=int((time.time() - start_time) * 1000)
            )

    async def _extract_selector(self, element) -> str:
        """Extract a reusable CSS selector from an element."""
        try:
            selector = await element.evaluate("""
                el => {
                    if (el.id) return '#' + el.id;
                    if (el.getAttribute('data-testid'))
                        return `[data-testid="${el.getAttribute('data-testid')}"]`;
                    if (el.getAttribute('name'))
                        return `[name="${el.getAttribute('name')}"]`;
                    if (el.className) {
                        const classes = el.className.split(' ').filter(c => c && !c.includes(':')).slice(0, 2).join('.');
                        if (classes) return el.tagName.toLowerCase() + '.' + classes;
                    }
                    return el.tagName.toLowerCase();
                }
            """)
            return selector
        except:
            return "unknown"

    # -------------------------------------------------------------------------
    # VISION FALLBACK
    # -------------------------------------------------------------------------

    async def _verify_with_vision(self, intent: str) -> Optional[Tuple[str, Tuple[int, int]]]:
        """
        Vision AI fallback when SmartFinder can't find element.
        EXPENSIVE - only called when math fails.
        """
        if not self.vision_api_key:
            logger.warning("[Justifier] Vision fallback unavailable (no API key)")
            return None

        logger.warning("[Justifier] Vision API call - cost incurred")

        try:
            import openai

            screenshot = await self._page.screenshot(type="png")
            screenshot_b64 = base64.b64encode(screenshot).decode()

            client = openai.OpenAI(api_key=self.vision_api_key)

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"""Find the element: "{intent}"

Return JSON only:
{{"found": true, "x": 123, "y": 456, "selector_hint": "button.login-btn"}}
or
{{"found": false}}"""
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}
                            }
                        ]
                    }
                ],
                max_tokens=100
            )

            content = response.choices[0].message.content.strip()
            # Extract JSON from response
            if "{" in content:
                json_str = content[content.index("{"):content.rindex("}")+1]
                data = json.loads(json_str)

                if data.get("found"):
                    x, y = int(data.get("x", 0)), int(data.get("y", 0))
                    selector = data.get("selector_hint", f"[data-vision='{x},{y}']")
                    logger.info(f"[Justifier] Vision found at ({x}, {y})")
                    return (selector, (x, y))

            return None

        except Exception as e:
            logger.error(f"[Justifier] Vision error: {e}")
            return None


# =============================================================================
# CONVENIENCE FUNCTION
# =============================================================================

async def justify_recipe(recipe: Dict, url: str) -> JustificationResult:
    """Convenience function for recipe justification."""
    engine = JustifierEngine()
    return await engine.justify_recipe(recipe, url)
