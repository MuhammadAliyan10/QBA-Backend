"""
discoveryActivities.py — Temporal Activities for Math-First Workflow Generation

PIPELINE (replaces the old ReAct LLM loop):

  Activity 1: harvest_and_plan_activity
    - Launches browser, navigates to target URL
    - Runs DOMHarvester → full DOM snapshot (all scroll zones, shadow DOM, iFrames)
    - Runs IntentParser → breaks prompt into ordered Intent list (zero LLM)
    - Runs ElementMatcher → async scores every intent against every element
    - Calls LLM only when QUANTA_ENABLE_ELEMENT_LLM=1 and confidence is low
    - Returns a complete plan: list of (intent, matched_element, confidence) tuples

  Activity 2: execute_action_activity (unchanged interface, enhanced internals)
    - Executes one step from the plan against the live browser
    - Scrolls the element into view using stored scrollY (no re-scan)
    - Handles iFrame context switching, overlay dismissal, Shadow DOM

  Activity 3: cleanup_browser_activity (unchanged)
    - Tears down the browser context for this job_id

Author: Quanta Engineering
Version: 3.0.0 (Math-First Engine)
"""

import asyncio
import logging
import json
import os
import time
from typing import Dict, Any, Optional, List
from datetime import timedelta

from temporalio import activity
from playwright.async_api import async_playwright, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# ── Core Imports ───────────────────────────────────────────────────────────────
from core.nervous_system import NervousSystem
from core.browser.dom_harvester import DOMHarvester, DOMSnapshot, DOMElement
from core.planning.intent_parser import IntentParser, Intent
from core.planning.element_matcher import ElementMatcher, MatchResult, CONFIDENT_THRESHOLD
from core.planning.node_builder import NodeBuilder
from core.llm.safe_client import SafeLLMClient
from core.utils.params import substitute_variables
from core.heuristics.router import evaluate_heuristics

logger = logging.getLogger("discoveryActivities")


# ─── LLM ESCALATION THRESHOLD ─────────────────────────────────────────────────
# Confidence below this triggers a focused LLM call for that specific intent.
# Above this → pure math, zero API cost.
LLM_ESCALATION_THRESHOLD = 0.50

# When unset/false, element resolution never calls the LLM in planning; uses
# overlay re-harvest, then deterministic top-k from ElementMatcher.
_ENABLE_ELEMENT_LLM = os.getenv("QUANTA_ENABLE_ELEMENT_LLM", "0").strip().lower()
ENABLE_ELEMENT_LLM: bool = _ENABLE_ELEMENT_LLM in ("1", "true", "yes")


# =============================================================================
# BROWSER POOL (unchanged — session-per-job management)
# =============================================================================

class BrowserPool:
    """
    Manages one Playwright browser instance per worker process.
    One browser context per job_id — isolated sessions for parallel jobs.
    """
    _playwright = None
    _browser    = None
    _contexts: Dict[str, Dict] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def getPage(cls, jobId: str, url: str = None, cookies: list = None) -> Page:
        async with cls._lock:
            # Launch browser on first use
            if not cls._playwright:
                try:
                    cls._playwright = await async_playwright().start()
                    cls._browser    = await cls._playwright.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-blink-features=AutomationControlled",  # Reduce bot detection
                            "--disable-dev-shm-usage",
                            "--disable-extensions",
                        ]
                    )
                    logger.info("[BrowserPool] Chromium launched")
                except Exception as exc:
                    logger.critical(f"[BrowserPool] Failed to launch browser: {exc}")
                    raise

            # Create a new context for this job if one doesn't exist
            if jobId not in cls._contexts:
                context = await cls._browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    # Block heavy assets to speed up page load (we only need DOM)
                    # NOTE: disabling images speeds harvest but may affect visibility checks
                )

                # Inject cookies for authenticated sessions
                if cookies:
                    try:
                        await context.add_cookies(cookies)
                    except Exception as cookieErr:
                        logger.warning(f"[BrowserPool] [{jobId}] Cookie injection failed: {cookieErr}")

                page = await context.new_page()

                # Block analytics and tracking to speed things up (no automation impact)
                await page.route(
                    "**/{analytics,gtm,ads,facebook,doubleclick}**",
                    lambda route: route.abort()
                )

                cls._contexts[jobId] = {"context": context, "page": page, "currentUrl": None}
                logger.info(f"[BrowserPool] [{jobId}] New browser context created")

            state = cls._contexts[jobId]
            page  = state["page"]

            # Navigate only if the requested URL differs from the current URL
            if url and state["currentUrl"] != url:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=7_000)
                    except Exception:
                        pass  # Heavy pages may never reach networkidle — that's OK
                    await asyncio.sleep(1.5)   # SPA settle window
                    state["currentUrl"] = url
                except Exception as navErr:
                    logger.warning(f"[BrowserPool] [{jobId}] Navigation warning: {navErr}")

            return page

    @classmethod
    async def cleanup(cls, jobId: str):
        """Closes the browser context for the given job and releases memory."""
        async with cls._lock:
            if jobId in cls._contexts:
                try:
                    await cls._contexts[jobId]["context"].close()
                    logger.info(f"[BrowserPool] [{jobId}] Context closed")
                except Exception:
                    pass
                del cls._contexts[jobId]


# =============================================================================
# ACTIVITY 1: HARVEST + PLAN (The Math-First Replacement for evaluate_next_step_activity)
# =============================================================================

@activity.defn(name="harvest_and_plan_activity")
async def harvest_and_plan_activity(payload: dict) -> dict:
    """
    One-shot DOM harvest + offline intent planning.

    WHAT IT DOES:
      1. Gets a live browser page for this job.
      2. Runs DOMHarvester → full DOM snapshot (no scrolling needed).
      3. Runs IntentParser → ordered list of Intent objects (zero LLM).
      4. Runs ElementMatcher → scores DOM for each intent (zero LLM).
      5. For low-confidence intents → makes ONE focused LLM call with
         pre-filtered top-5 candidates (not the full DOM — much cheaper).
      6. Returns a complete plan ready for execution and node construction.

    Returns:
      {
        "success": bool,
        "plan": [
          {
            "intent": {...},         # Serialized Intent
            "element": {...} | null, # Best-matched DOMElement
            "confidence": float,
            "requiresLlm": bool,
          }
        ],
        "snapshotStats": {...},      # Diagnostics
      }
    """
    jobId   = payload.get("job_id", "unknown")
    prompt  = payload.get("prompt", "")
    url     = payload.get("url", "")
    cookies = payload.get("cookies", [])

    logger.info(f"[{jobId}] Starting math-first harvest+plan for: '{prompt[:60]}'")
    await NervousSystem.publish_update(jobId, "RUNNING", "📡 Harvesting full DOM snapshot...", "planning")

    try:
        # ── 1. Get the live page ──────────────────────────────────────────
        page = await BrowserPool.getPage(jobId, url, cookies)

        # ── 2. Full DOM Harvest ───────────────────────────────────────────
        harvester = DOMHarvester(page)
        snapshot  = await harvester.harvest()    # URL already loaded by BrowserPool

        await NervousSystem.publish_update(
            jobId, "RUNNING",
            f"🔍 DOM snapshot: {len(snapshot.elements)} elements found "
            f"({len(snapshot.inViewportElements)} in viewport, "
            f"{len(snapshot.belowFoldElements)} below fold)",
            "planning"
        )

        # ── 3. Parse Intents (Zero LLM) ──────────────────────────────────
        parser  = IntentParser()
        intents = parser.parse(prompt, url)

        await NervousSystem.publish_update(
            jobId, "RUNNING",
            f"🧠 Parsed {len(intents)} automation steps (zero AI cost)",
            "planning"
        )

        # ── 4. Match Elements (deterministic-first; LLM gated by env) ───────
        matcher = ElementMatcher()
        plan    = []

        llmFallbackCount = 0
        llmClient = SafeLLMClient()  # Instantiated once — lazy-used only if enabled

        for intent in intents:
            stepLog = f"Step {intent.stepNumber}: [{intent.action}] '{intent.targetDescription}'"

            # ── LAYER 2: The Detective (Heuristics) ─────────────────────────
            # Try to resolve the intent using deterministic playbooks first
            action_map = [el.__dict__ for el in snapshot.elements]
            heuristic_match = evaluate_heuristics(intent.targetDescription, action_map)

            if heuristic_match:
                # Find the actual element object for this ID
                matched_el = next((el for el in snapshot.elements if el.qId == heuristic_match["target_id"]), None)
                if matched_el:
                    plan.append({
                        "intent":      _serializeIntent(intent),
                        "element":     _serializeElement(matched_el),
                        "confidence":  1.0,
                        "requiresLlm": False,
                        "source": "heuristic",
                        "heuristic_action": heuristic_match["action"]
                    })
                    logger.info(f"[{jobId}] {stepLog} → HEURISTIC MATCH: {heuristic_match['target_id']}")
                    await NervousSystem.publish_update(
                        jobId, "RUNNING",
                        f"🕵️ Heuristic match: {intent.targetDescription}",
                        "planning"
                    )
                    continue

            # Non-element intents (navigate, wait, scroll) need no element matching
            if intent.isNavigational():
                plan.append({
                    "intent":      _serializeIntent(intent),
                    "element":     None,
                    "confidence":  1.0,
                    "requiresLlm": False,
                })
                logger.info(f"[{jobId}] {stepLog} → NAVIGATIONAL (no element needed)")
                continue

            def _high_conf_entry(mr: MatchResult) -> Dict[str, Any]:
                row: Dict[str, Any] = {
                    "intent":      _serializeIntent(intent),
                    "element":     _serializeElement(mr.element),
                    "confidence":  mr.confidence,
                    "requiresLlm": False,
                }
                if getattr(mr, "ambiguous", False):
                    row["ambiguousMatch"] = True
                return row

            # ── ElementMatcher (async) ─────────────────────────────────────
            matchResult: MatchResult = await matcher.match(intent, snapshot)
            snap_for_candidates: DOMSnapshot = snapshot

            if matchResult.found and matchResult.confidence >= LLM_ESCALATION_THRESHOLD:
                plan.append(_high_conf_entry(matchResult))
                logger.info(
                    f"[{jobId}] {stepLog} → "
                    f"MATCHED (conf={matchResult.confidence:.2f}) "
                    f"<{matchResult.element.tag}> '{(matchResult.element.text or '')[:30]}'"
                )
                await NervousSystem.publish_update(
                    jobId, "RUNNING",
                    f"✅ Matched: {intent.targetDescription} (confidence: {matchResult.confidence:.0%})",
                    "planning"
                )
                continue

            # ── Low confidence: direct scrape (keyword) before heavier work ─
            if intent.action == "scrape":
                scrapedValue = await _directScrapeByKeyword(page, intent.targetDescription)
                if scrapedValue:
                    plan.append({
                        "intent":      _serializeIntent(intent),
                        "element":     None,
                        "confidence":  0.60,
                        "requiresLlm": False,
                        "directScrapeValue": scrapedValue,
                    })
                    logger.info(f"[{jobId}] {stepLog} → DIRECT SCRAPE FALLBACK")
                    await NervousSystem.publish_update(
                        jobId, "RUNNING",
                        f"Scraped directly: {intent.targetDescription}",
                        "planning"
                    )
                    continue

            # ── One-shot: dismiss overlays + re-harvest + re-match ──────────
            try:
                dismissed = await _reflexDismissOverlay(page)
                if dismissed:
                    snap_for_candidates = await DOMHarvester(page).harvest()
                    matchResult = await matcher.match(intent, snap_for_candidates)
            except Exception as overlay_exc:
                logger.warning(f"[{jobId}] Overlay rematch skipped: {overlay_exc}")

            if matchResult.found and matchResult.confidence >= LLM_ESCALATION_THRESHOLD:
                plan.append(_high_conf_entry(matchResult))
                logger.info(
                    f"[{jobId}] {stepLog} → MATCHED after overlay rematch "
                    f"(conf={matchResult.confidence:.2f})"
                )
                await NervousSystem.publish_update(
                    jobId, "RUNNING",
                    f"✅ Matched (rematch): {intent.targetDescription} "
                    f"({matchResult.confidence:.0%})",
                    "planning"
                )
                continue

            # ── Optional LLM assist (disabled by default) ───────────────────
            if ENABLE_ELEMENT_LLM:
                llmFallbackCount += 1
                logger.info(
                    f"[{jobId}] {stepLog} → "
                    f"LLM escalation (conf={matchResult.confidence:.2f})"
                )
                await NervousSystem.publish_update(
                    jobId, "RUNNING",
                    f"AI assist: '{intent.targetDescription}' (low-confidence match)",
                    "planning"
                )
                topCandidates = await matcher.getTopCandidates(
                    intent, snap_for_candidates, n=5
                )
                llmElement = await _llmResolveElement(
                    llmClient, intent, topCandidates, snap_for_candidates.url, jobId
                )
                plan.append({
                    "intent":      _serializeIntent(intent),
                    "element":     _serializeElement(llmElement) if llmElement else None,
                    "confidence":  0.65 if llmElement else 0.0,
                    "requiresLlm": True,
                })
            else:
                # Deterministic top-k: best scored visible candidate, no LLM egress
                topCandidates = await matcher.getTopCandidates(
                    intent, snap_for_candidates, n=5
                )
                if topCandidates:
                    bestScore, bestEl = topCandidates[0]
                    plan.append({
                        "intent":      _serializeIntent(intent),
                        "element":     _serializeElement(bestEl),
                        "confidence":  float(bestScore),
                        "requiresLlm": False,
                        "deterministicTopKFallback": True,
                    })
                    logger.info(
                        f"[{jobId}] {stepLog} → TOP-K FALLBACK "
                        f"(score={bestScore:.2f}, qId={bestEl.qId})"
                    )
                    await NervousSystem.publish_update(
                        jobId, "RUNNING",
                        f"Heuristic pick: {intent.targetDescription} "
                        f"(score {bestScore:.0%}, no AI)",
                        "planning"
                    )
                else:
                    plan.append({
                        "intent":      _serializeIntent(intent),
                        "element":     None,
                        "confidence":  0.0,
                        "requiresLlm": False,
                        "unresolved": True,
                    })
                    logger.warning(f"[{jobId}] {stepLog} → UNRESOLVED (no scored candidates)")
                    await NervousSystem.publish_update(
                        jobId, "RUNNING",
                        f"No element candidates for: {intent.targetDescription}",
                        "planning"
                    )

        mathResolved = len([p for p in plan if not p["requiresLlm"]])
        logger.info(
            f"[{jobId}] Plan complete: {mathResolved}/{len(plan)} steps resolved by math, "
            f"{llmFallbackCount} used LLM fallback"
        )

        return {
            "success": True,
            "plan":    plan,
            "snapshotStats": {
                "totalElements":   len(snapshot.elements),
                "inViewport":      len(snapshot.inViewportElements),
                "belowFold":       len(snapshot.belowFoldElements),
                "harvestMs":       snapshot.harvestDurationMs,
                "mathResolved":    mathResolved,
                "llmFallbacks":    llmFallbackCount,
            },
            "triggerType":  getattr(intents[0], "triggerType", "MANUAL") if intents else "MANUAL",
            "cronSchedule": getattr(intents[0], "cronSchedule", None) if intents else None,
        }

    except Exception as exc:
        logger.error(f"[{jobId}] harvest_and_plan_activity failed: {exc}", exc_info=True)
        return {"success": False, "error": str(exc), "plan": []}


# =============================================================================
# ACTIVITY 2: EXECUTE ACTION WITH SMART SCROLL + REFLEXES
# =============================================================================

@activity.defn(name="execute_action_activity")
async def execute_action_activity(payload: dict) -> dict:
    """
    Executes a single planned step against the live browser.

    WHAT'S NEW vs. old version:
      - Uses stored `scrollY` to scroll element into view precisely (no DOM re-scan)
      - Tries the full `selectorChain` in order before failing
      - Handles iFrame context switching from stored `iframeIndex`
      - Overlay dismissal reflex runs as a pre-flight check before each CLICK
      - Returns the confirmed selector that worked (stored in node for future runs)
    """
    jobId         = payload.get("job_id", "unknown")
    action        = payload.get("action", "click").lower()
    # ── Variable Substitution ─────────────────────────────────────────
    results = payload.get("results", {})
    intent  = substitute_variables(payload.get("intent", ""), results)
    value   = substitute_variables(payload.get("value", ""), results)
    message = substitute_variables(payload.get("message", ""), results)

    # Also substitute in selector chain? (rare but possible)
    selectorChain   = [substitute_variables(s, results) for s in payload.get("selectorChain", [])]
    primarySelector = substitute_variables(payload.get("selector", ""), results)

    scrollY       = int(payload.get("scrollY", 0))
    inIframe      = payload.get("inIframe", False)
    iframeIndex   = payload.get("iframeIndex", None)
    value         = payload.get("value", "")

    # Build full chain: stored primary + chain (may overlap, dedup)
    fullChain = _dedup([primarySelector] + selectorChain) if primarySelector else selectorChain

    logger.info(f"[{jobId}] Executing [{action}] '{intent}' with {len(fullChain)} selector candidates")
    await NervousSystem.publish_update(jobId, "RUNNING", f"⚡ Executing: {intent}...", "execution")

    try:
        page = await BrowserPool.getPage(jobId)

        # ── Resolve target frame (main page or iFrame) ─────────────────────
        targetFrame = page
        if inIframe and iframeIndex is not None:
            frames = [f for f in page.frames if f != page.main_frame]
            if iframeIndex < len(frames):
                targetFrame = frames[iframeIndex]
                logger.info(f"[{jobId}] Switched to iFrame {iframeIndex}: {targetFrame.url}")

        # ── Non-element actions ────────────────────────────────────────────
        if action == "navigate":
            navUrl = value or intent
            await targetFrame.goto(navUrl, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.0)
            return {"success": True, "selector": navUrl}

        if action in ("scroll_down", "scroll"):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.5)
            return {"success": True, "selector": "window"}

        if action == "scroll_up":
            await page.evaluate("window.scrollBy(0, -window.innerHeight)")
            await asyncio.sleep(0.5)
            return {"success": True, "selector": "window"}

        if action == "wait":
            duration = int(value or 2000)
            await asyncio.sleep(duration / 1000)
            return {"success": True, "selector": "timer"}

        if action == "log":
            # LOG action simply returns the substituted message for the UI
            content = message or value or intent
            logger.info(f"[{jobId}] LOG: {content}")
            return {"success": True, "selector": "log", "scrapedValue": content}

        # ── Element-based actions ──────────────────────────────────────────

        # Check for direct scrape fallback first
        directScrapeValue = payload.get("directScrapeValue")
        if action == "scrape" and directScrapeValue:
            logger.info(f"[{jobId}] Scraped (direct): '{directScrapeValue[:80]}'")
            return {"success": True, "selector": "direct-scrape", "scrapedValue": directScrapeValue}

        # Step 1: Precise scroll to stored absolute position (no DOM re-scan)
        if scrollY > 0:
            await page.evaluate(f"window.scrollTo(0, Math.max(0, {scrollY} - 150))")
            await asyncio.sleep(0.3)

        # Step 2: Try selector chain in reliability order
        element, workingSelector = await _trySelectorChain(targetFrame, fullChain)

        # Step 3: OVERLAY PRE-FLIGHT — dismiss blocking banners before clicking
        if action == "click" and element:
            await _reflexDismissOverlay(page)

        # Step 4: Execute the action
        if not element:
            return {"success": False, "error": f"No selector matched from chain: {fullChain[:3]}"}

        if action == "click":
            try:
                await element.click(timeout=6_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=4_000)
                except PlaywrightTimeoutError:
                    pass
            except Exception as clickErr:
                if "intercepted" in str(clickErr).lower() or "target closed" in str(clickErr).lower():
                    dismissed = await _reflexDismissOverlay(page)
                    if dismissed:
                        await asyncio.sleep(0.5)
                        await element.click(timeout=15_000)
                    else:
                        return {"success": False, "error": f"Click blocked by overlay: {clickErr}"}
                else:
                    return {"success": False, "error": str(clickErr)}

        elif action == "type":
            await element.fill(value, timeout=5_000)

        elif action == "scrape":
            qualifier = payload.get("qualifier")
            if qualifier == "all":
                # Find all matching elements for the working selector
                elements = await targetFrame.query_selector_all(workingSelector)
                results = []
                for el in elements:
                    txt = await el.inner_text()
                    if txt.strip():
                        results.append(txt.strip())
                logger.info(f"[{jobId}] Scraped {len(results)} items (qualifier=all)")
                return {"success": True, "selector": workingSelector, "scrapedValue": results}
            else:
                text = await element.inner_text()
                logger.info(f"[{jobId}] Scraped: '{text[:80]}'")
                return {"success": True, "selector": workingSelector, "scrapedValue": text}

        elif action == "select":
            await element.select_option(label=value, timeout=5_000)

        elif action == "check":
            await element.check(timeout=5_000)

        elif action == "hover":
            await element.hover(timeout=5_000)

        return {"success": True, "selector": workingSelector}

    except Exception as exc:
        logger.error(f"[{jobId}] execute_action_activity failed: {exc}", exc_info=True)
        return {"success": False, "error": str(exc)}


# =============================================================================
# ACTIVITY 3: CLEANUP (unchanged interface)
# =============================================================================

@activity.defn(name="cleanup_browser_activity")
async def cleanup_browser_activity(payload: dict) -> dict:
    """Releases the browser context for the given job."""
    jobId = payload.get("job_id")
    if jobId:
        await BrowserPool.cleanup(jobId)
        logger.info(f"[{jobId}] Browser session cleaned up")
    return {"success": True}


# =============================================================================
# PRIVATE HELPERS
# =============================================================================

async def _trySelectorChain(frame, chain: List[str]):
    """
    Tries each selector in the chain in order.
    Returns (element_handle, working_selector) or (None, None) if all fail.
    This is the core execution self-healing mechanism.
    """
    for selector in chain:
        if not selector:
            continue
        try:
            loc    = frame.locator(selector).first
            isVis  = await loc.is_visible(timeout=1_500)
            if isVis:
                handle = await loc.element_handle(timeout=1_500)
                if handle:
                    logger.debug(f"[SelectorChain] Hit: {selector}")
                    return handle, selector
        except Exception:
            continue
    return None, None


async def _reflexDismissOverlay(page: Page) -> bool:
    """
    Attempts to dismiss a blocking overlay (cookie banner, GDPR consent, interstitial).
    Uses a curated list of selectors from the 50 most popular consent management platforms.
    Pure Playwright — zero LLM cost.
    """
    # Curated from: OneTrust, CookieYes, Cookiebot, Axeptio, Termly, TrustArc, and custom
    overlaySelectors = [
        # Generic close/accept patterns
        "button[aria-label='Close']",
        "button[aria-label='Accept']",
        "button[aria-label='Accept all']",
        "button[aria-label='Accept All Cookies']",
        "button[aria-label='Dismiss']",
        # Text-content buttons
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('I Accept')",
        "button:has-text('I Agree')",
        "button:has-text('Got it')",
        "button:has-text('OK')",
        "button:has-text('Close')",
        "button:has-text('Dismiss')",
        "button:has-text('Allow all')",
        "button:has-text('Allow All')",
        "button:has-text('Consent')",
        # OneTrust
        "#onetrust-accept-btn-handler",
        "#onetrust-close-btn-handler",
        ".onetrust-close-btn-handler",
        # CookieYes
        ".cky-btn-accept",
        "[data-cky-tag='accept-button']",
        # Cookiebot
        "#CybotCookiebotDialogBodyButtonAccept",
        "#CybotCookiebotDialogBodyLevelButtonAcceptAll",
        # Generic modal close
        "[class*='modal'] button[class*='close']",
        "[class*='cookie'] button[class*='accept']",
        "[class*='banner'] button[class*='accept']",
        "[class*='consent'] button[class*='accept']",
        # ARIA close buttons
        "button[aria-label*='cookie' i]",
        "button[aria-label*='consent' i]",
    ]

    for selector in overlaySelectors:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=400):
                await loc.click(timeout=1_000)
                await asyncio.sleep(0.4)
                logger.info(f"[OverlayReflex] Dismissed overlay via: {selector}")
                return True
        except Exception:
            continue

    return False


async def _llmResolveElement(
    llmClient: SafeLLMClient,
    intent: Intent,
    topCandidates: List[tuple],
    pageUrl: str,
    jobId: str,
) -> Optional[DOMElement]:
    """
    Focused LLM call for low-confidence intents.
    Sends ONLY the top 5 pre-filtered candidates (not the full 600-element DOM).
    This reduces LLM input tokens by ~95% vs. sending the full DOM.
    """
    if not topCandidates:
        return None

    # Serialize top candidates to a lean representation
    candidateSummary = []
    for score, el in topCandidates:
        candidateSummary.append({
            "qId":         el.qId,
            "tag":         el.tag,
            "text":        (el.text or "")[:80],
            "ariaLabel":   el.ariaLabel,
            "placeholder": el.placeholder,
            "role":        el.role,
            "type":        el.type,
            "score":       round(score, 2),
        })

    systemPrompt = f"""You are an element-matching assistant.
You MUST select the best element from the candidates list for the given intent.
Output ONLY valid JSON. No explanation, no preamble.

Page URL: {pageUrl}
Action:   {intent.action}
Looking for: {intent.targetDescription}

Candidates (pre-filtered top matches):
{json.dumps(candidateSummary, indent=2)}

Output exactly:
{{"qId": "q-N", "reason": "one line"}}
"""

    try:
        result = await llmClient.safe_evaluate_step(
            "Pick the best candidate element.", systemPrompt
        )
        selectedQId = result.get("qId", "")

        # Find the DOMElement that matches the selected qId
        for _, el in topCandidates:
            if el.qId == selectedQId:
                logger.info(f"[{jobId}] LLM selected: {selectedQId} — {result.get('reason', '')}")
                return el

        # LLM returned an invalid qId — fall back to the highest-scored candidate
        logger.warning(f"[{jobId}] LLM returned unknown qId '{selectedQId}'. Using top candidate.")
        return topCandidates[0][1] if topCandidates else None

    except Exception as exc:
        logger.error(f"[{jobId}] LLM element resolution failed: {exc}")
        return topCandidates[0][1] if topCandidates else None


def _serializeIntent(intent: Intent) -> dict:
    """Converts an Intent to a plain dict for Temporal boundary crossing."""
    return {
        "stepNumber":         intent.stepNumber,
        "action":             intent.action,
        "targetDescription":  intent.targetDescription,
        "value":              intent.value,
        "qualifier":          intent.qualifier,
        "rawSentence":        intent.rawSentence,
        "confidence":         intent.confidence,
    }


def _serializeElement(el: Optional[DOMElement]) -> Optional[dict]:
    """Converts a DOMElement to a plain dict for Temporal boundary crossing."""
    if el is None:
        return None
    return el.__dict__


def _dedup(items: List[str]) -> List[str]:
    """Deduplicate a list while preserving order."""
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def _directScrapeByKeyword(page: Page, targetDescription: str) -> Optional[str]:
    """
    CSS-based keyword scrape fallback for low-confidence intents.
    Searches Wikipedia infobox th/td pairs, then generic text containers.
    """
    import re
    keywords = [
        w for w in targetDescription.lower().split()
        if len(w) > 2 and w not in {
            "the", "his", "her", "for", "and", "that", "currently", "plays",
            "what", "which", "where", "how", "old", "are"
        }
    ]
    if not keywords:
        return None

    logger.info(f"[DirectScrape] Keywords: {keywords}")

    # ── Strategy 1: Wikipedia infobox th/td pairs ─────────────────────────
    try:
        infoboxData: dict = await page.evaluate("""() => {
            const result = {};
            document.querySelectorAll('.infobox tr').forEach(row => {
                const th = row.querySelector('th');
                const td = row.querySelector('td');
                if (th && td) {
                    const label = th.innerText.trim().toLowerCase();
                    const value = td.innerText.trim().replace(/\\s+/g, ' ');
                    if (value && label) result[label] = value;
                }
            });
            return result;
        }""")

        if infoboxData:
            for label, value in infoboxData.items():
                if any(kw in label for kw in keywords):
                    logger.info(f"[DirectScrape] HIT — '{label}' → '{value[:100]}'")
                    return value[:300]
    except Exception as exc:
        logger.warning(f"[DirectScrape] Infobox failed: {exc}")

    # ── Strategy 2: Age/born from bday microformat or labelled cell ───────
    if any(kw in {"age", "born", "birth", "birthday", "old"} for kw in keywords):
        try:
            result: str = await page.evaluate("""() => {
                const bday = document.querySelector('.bday');
                if (bday) {
                    const parent = bday.closest('td') || bday.parentElement;
                    if (parent) return parent.innerText.trim().replace(/\\s+/g, ' ');
                }
                for (const row of document.querySelectorAll('.infobox tr')) {
                    const th = row.querySelector('th');
                    const td = row.querySelector('td');
                    if (th && td && th.innerText.toLowerCase().includes('born')) {
                        return td.innerText.trim().replace(/\\s+/g, ' ');
                    }
                }
                return null;
            }""")
            if result:
                ageMatch = re.search(r'\(age\s+(\d+)\)', result)
                if ageMatch:
                    return f"Age {ageMatch.group(1)} | Full: {result[:200]}"
                return result[:300]
        except Exception as exc:
            logger.warning(f"[DirectScrape] Born lookup failed: {exc}")

    # ── Strategy 3: Current club / team ───────────────────────────────────
    if any(kw in {"club", "team", "plays", "current", "nassr", "squad"} for kw in keywords):
        try:
            result: str = await page.evaluate("""() => {
                for (const row of document.querySelectorAll('.infobox tr')) {
                    const th = row.querySelector('th');
                    const td = row.querySelector('td');
                    if (th && td) {
                        const label = th.innerText.toLowerCase();
                        if (label.includes('current') || label.includes('club') ||
                            label.includes('team') || label.includes('on loan')) {
                            return td.innerText.trim().replace(/\\s+/g, ' ');
                        }
                    }
                }
                return null;
            }""")
            if result:
                return result[:300]
        except Exception as exc:
            logger.warning(f"[DirectScrape] Club lookup failed: {exc}")

    # ── Strategy 4: Generic text container search ─────────────────────────
    try:
        result: str = await page.evaluate("""(keywords) => {
            const kws = keywords;
            const els = document.querySelectorAll('p, td, span, li');
            for (const el of els) {
                const text = el.innerText.toLowerCase();
                if (kws.every(kw => text.includes(kw))) {
                    const full = el.innerText.trim().replace(/\\s+/g, ' ');
                    if (full.length > 5 && full.length < 500) return full;
                }
            }
            return null;
        }""", keywords)
        if result:
            return result[:300]
    except Exception as exc:
        logger.warning(f"[DirectScrape] Generic text search failed: {exc}")

    return None
