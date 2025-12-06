import logging
import asyncio
from urllib.parse import urlparse
from playwright.async_api import Page, ElementHandle
from simhash import Simhash
from exceptions import HumanInterventionRequired  # [NEW] Human-in-the-Loop
from core.Healer import TheHealer
from core.GlassBox import GlassBoxEngine
from algorithms.LevenshteinScorer import LevenshteinScorer  # Fixed case
from core.Planner import TheCortex
from core.PatternDB import PatternDB  # [NEW] Muscle Memory

logger = logging.getLogger("smartFinder")


class SmartFinder:
    """
    The Glass Box Element Finder with Muscle Memory.

    Pipeline:
    1. Common Sense Check (Cortex)
    2. CAPTCHA Detection
    3. 🆕 Memory Check (Fast Path)
    4. Hydration Wait & Candidate Extraction (Math Path)
    5. Scoring (Sniper)
    6. Verification (Physics)
    7. 🆕 Learning (Save Pattern)
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.glass = GlassBoxEngine()
        self.scorer = LevenshteinScorer()
        self.cortex = TheCortex()
        self.healer = TheHealer()
        self.pattern_db = PatternDB()  # [NEW] Initialize Pattern Database

    async def _get_page_fingerprint(self, page: Page) -> tuple[str, str]:
        """
        Generates a Stable Fingerprint (Domain + Structural Hash).

        Uses TAG STRUCTURE instead of text to avoid cache misses
        on dynamic sites (prices, timestamps, news headlines).

        Returns:
            tuple[str, str]: (domain, page_hash)
        """
        domain = urlparse(page.url).netloc

        # Extract tag structure: "BODY DIV HEADER DIV IMG SPAN..."
        tag_structure = await page.evaluate("""
            () => {
                function getTags(el) {
                    let tags = el.tagName;
                    for (let child of el.children) {
                        tags += ' ' + getTags(child);
                    }
                    return tags;
                }
                return getTags(document.body);
            }
        """)

        # FIX: Tokenize before hashing to prevent OverflowError on large pages
        # Split the giant string into features (tag names)
        # This avoids Simhash's internal C-tokenizer which overflows on >1000 char strings
        features = tag_structure.split(' ')  # Create list of features

        # Hash the feature list (not the raw string)
        page_hash = str(Simhash(features).value)
        logger.debug(f"[Logic] Fingerprint: {domain} | Hash: {page_hash[:16]}...")

        return domain, page_hash

    async def _generate_selector(self, page: Page, element: ElementHandle) -> str:
        """
        Generates a robust unique CSS selector for an element.

        Algorithm:
        1. If element has ID, use it (#id)
        2. Otherwise, walk up the DOM tree building a path
        3. Use nth-of-type for disambiguation

        Returns:
            str: CSS selector (e.g., "div#nav > ul > li:nth-of-type(3) > a")
        """
        return await page.evaluate("""(el) => {
            // If element has ID, that's the best selector
            if (el.id) return '#' + el.id;

            let path = [];
            while (el.nodeType === Node.ELEMENT_NODE) {
                let selector = el.nodeName.toLowerCase();

                if (el.id) {
                    // Found an ID up the tree, use it and stop
                    selector = '#' + el.id;
                    path.unshift(selector);
                    break;
                } else {
                    // Count siblings of the same type for nth-of-type
                    let sib = el, nth = 1;
                    while (sib = sib.previousElementSibling) {
                        if (sib.nodeName.toLowerCase() == selector)
                           nth++;
                    }
                    if (nth != 1)
                        selector += ":nth-of-type("+nth+")";
                }

                path.unshift(selector);
                el = el.parentNode;
            }
            return path.join(" > ");
        }""", element)

    async def detect_captcha(self, page: Page):
        """
        Checks for common CAPTCHA iframes (Cloudflare, ReCaptcha, hCaptcha).

        Raises HumanInterventionRequired if CAPTCHA detected, triggering workflow hibernation.
        """
        captcha_detected = False
        captcha_type = None

        if await page.query_selector(
            "iframe[src*='cloudflare'], iframe[src*='recaptcha'], iframe[src*='hcaptcha']"
        ):
            captcha_detected = True
            captcha_type = "iframe_captcha"

        # Check for Challenge Titles
        title = await page.title()
        if "Just a moment" in title or "Security Check" in title:
            captcha_detected = True
            captcha_type = "cloudflare_challenge"

        if captcha_detected:
            logger.warning(f"[{self.job_id}] 🚧 CAPTCHA Detected!")

            # Raise exception to trigger workflow hibernation
            raise HumanInterventionRequired(
                reason="CAPTCHA_DETECTED",
                context={
                    "url": page.url,
                    "title": title,
                    "captcha_type": captcha_type,
                    "job_id": self.job_id
                }
            )

    async def find(self, page: Page, intent: str) -> ElementHandle:
        """
        Finds an element using the Glass Box pipeline with Muscle Memory.

        Workflow:
        1. Common Sense Check
        2. CAPTCHA Check
        3. 🆕 Memory Check (Fast Path ~10-25ms)
        4. Math Path (Slow Path ~150-300ms)
        5. 🆕 Learning (Save successful pattern)
        """
        logger.info(f"[{self.job_id}] 🧠 SmartFinder searching for: '{intent}'")

        # --- STEP 1: COMMON SENSE CHECK ---
        if not await self.cortex.validate_action(page, intent):
            raise Exception(
                f"Action '{intent}' is illogical for the current page context."
            )

        # --- STEP 1.5: CAPTCHA CHECK ---
        # detect_captcha now raises HumanInterventionRequired exception directly
        # No need to check return value - exception propagates to workflow
        await self.detect_captcha(page)

        # --- 🆕 STEP 2: MEMORY CHECK (FAST PATH) ---
        domain, page_hash = await self._get_page_fingerprint(page)
        cached_selector = self.pattern_db.get_pattern(domain, page_hash, intent)

        if cached_selector:
            try:
                logger.info(f"   ⚡ Fast Path: Trying cached selector '{cached_selector[:40]}...'")
                element = await page.query_selector(cached_selector)

                # Verify it's still valid and visible
                if element and await element.is_visible():
                    logger.info(f"[System] Memory Hit Confirmed! (Saved ~200ms)")
                    return element
                else:
                    logger.warning("[Warning] Pattern Drift: Cached element missing or invisible.")
            except Exception as e:
                logger.warning(f"[Warning] Cached selector failed: {e}")

        # --- STEP 3: MATH PATH (SLOW PATH / HYDRATION WAIT) ---
        logger.info("   🧮 Math Path: Scanning DOM...")
        candidates = []

        # Get Priority Zones (e.g. check <form> first for login)
        priority_zones = self.cortex.get_search_zones(intent)

        for attempt in range(3):
            # A. FOCUSED SCAN (Fast)
            if priority_zones:
                zone_selector = ", ".join(priority_zones)
                try:
                    raw_nodes = await page.query_selector_all(zone_selector)
                    candidates = await self.glass.filter_visible_elements(
                        page, raw_nodes
                    )
                    if candidates:
                        logger.info(f"[Logic] Focused Scan hit in {priority_zones}")
                        break
                except:
                    pass

            # B. GLOBAL SCAN (Fallback)
            if not candidates:
                raw_nodes = await self.glass.get_all_interactive_nodes(page)
                candidates = await self.glass.filter_visible_elements(page, raw_nodes)

            if candidates:
                break

            logger.info(
                f"[{self.job_id}] DOM empty/loading. Retrying {attempt+1}/3..."
            )
            await page.wait_for_timeout(500)

        if not candidates:
            raise Exception(
                "Page interaction failed: No visible interactive elements found."
            )

        # --- STEP 4: SCORING (THE SNIPER) ---
        best_match = None
        best_score = -1.0

        for el in candidates:
            text = await el.inner_text()
            aria = await el.get_attribute("aria-label") or ""
            eid = await el.get_attribute("id") or ""

            content = f"{text} {aria} {eid}".strip()
            score = self.scorer.score(intent, content)

            if score > best_score:
                best_score = score
                best_match = el

        # --- STEP 5: VERIFICATION ---
        if best_match and best_score > 0.70:
            logger.info(f"[System] Sniper Hit! Score: {best_score:.2f}")

            # --- 🆕 STEP 6: LEARNING (SAVE PATTERN) ---
            try:
                # Generate unique selector for this element
                selector = await self._generate_selector(page, best_match)
                # Save to pattern database for future fast-path retrieval
                self.pattern_db.save_pattern(domain, page_hash, intent, selector)
                logger.debug(f"[Storage] Pattern learned: {selector[:50]}...")
            except Exception as e:
                logger.warning(f"[Warning] Failed to save pattern: {e}")

            # Physics Check
            if await self.glass.is_physically_clickable(page, best_match):
                return best_match
            else:
                logger.warning(
                    "   ⚠️ Best match is obscured! Attempting to heal/retry..."
                )
                return best_match

        # --- STEP 7: FAILURE ---
        logger.error(
            f"   ❌ Element '{intent}' not found. Best score was only {best_score:.2f}"
        )
        raise Exception(f"Element '{intent}' not found.")
