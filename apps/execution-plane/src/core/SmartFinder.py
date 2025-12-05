import logging
import asyncio
from playwright.async_api import Page, ElementHandle

# Import our camelCase modules
from core.GlassBox import GlassBoxEngine
from algorithms.LevenshteinScorer import LevenshteinScorer

logger = logging.getLogger("smartFinder")

class SmartFinder:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.glass = GlassBoxEngine()
        self.scorer = LevenshteinScorer()

    async def find(self, page: Page, intent: str) -> ElementHandle:
        """
        Finds an element using the 'Glass Box' pipeline.
        Pipeline:
        1. Wait for Hydration (Stability)
        2. Get Candidates (Shadow Pierce)
        3. Filter Honeypots (Visibility)
        4. Score Candidates (Levenshtein)
        5. Verify Physics (Raycast)
        """
        logger.info(f"[{self.job_id}] 🧠 SmartFinder searching for: '{intent}'")

        # --- STEP 1: HYDRATION WAIT & CANDIDATE EXTRACTION ---
        # We try 3 times to allow React/Angular to finish rendering
        candidates = []
        for attempt in range(3):
            # A. Get all potential buttons
            raw_nodes = await self.glass.get_all_interactive_nodes(page)

            # B. Filter out invisible traps (Honeypots)
            candidates = await self.glass.filter_visible_elements(page, raw_nodes)

            if candidates:
                break # Found valid nodes, proceed

            logger.info(f"[{self.job_id}] DOM empty/loading. Retrying {attempt+1}/3...")
            await page.wait_for_timeout(500) # Wait 500ms

        if not candidates:
            # If after 3 tries (1.5s) we see nothing, the page is likely a Canvas or blocked
            raise Exception("Page interaction failed: No visible interactive elements found.")

        # --- STEP 2: SCORING (THE SNIPER) ---
        best_match = None
        best_score = -1.0

        for el in candidates:
            # Extract text and attributes
            text = await el.inner_text()
            aria = await el.get_attribute("aria-label") or ""
            eid = await el.get_attribute("id") or ""

            # Combine signals for the scorer
            content = f"{text} {aria} {eid}".strip()
            score = self.scorer.score(intent, content)

            if score > best_score:
                best_score = score
                best_match = el

        # --- STEP 3: VERIFICATION ---
        # Threshold: 0.70 means "Good enough"
        if best_match and best_score > 0.70:
            logger.info(f"   ✅ Sniper Hit! Score: {best_score:.2f}")

            # --- STEP 4: PHYSICS CHECK (RAYCAST) ---
            # Before we click, we ensure no popup is blocking it
            if await self.glass.is_physically_clickable(page, best_match):
                return best_match
            else:
                logger.warning("   ⚠️ Best match is obscured! Attempting to heal/retry...")
                # In the future, we add logic here to close the popup.
                # For now, we return it and let Playwright try to force click.
                return best_match

        # --- STEP 5: FAILURE ---
        logger.error(f"   ❌ Element '{intent}' not found. Best score was only {best_score:.2f}")
        raise Exception(f"Element '{intent}' not found.")
