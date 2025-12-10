import logging
import asyncio
import os
import math
import numpy as np
from typing import Optional, List, Tuple, Dict, Set
from urllib.parse import urlparse
from functools import lru_cache
from playwright.async_api import Page, ElementHandle
from simhash import Simhash
from exceptions import HumanInterventionRequired
from core.Healer import TheHealer
from core.GlassBox import GlassBoxEngine
from algorithms.LevenshteinScorer import LevenshteinScorer
from core.Planner import TheCortex
from core.PatternDB import PatternDB
from core.TensorEngine import TensorEngine
from core.IntentExpander import get_intent_expander, IntentExpander

logger = logging.getLogger("smartFinder")

# =============================================================================
# INDUSTRIAL CONSTANTS
# =============================================================================

# Scoring thresholds (configurable via environment)
LEXICAL_THRESHOLD = float(os.getenv("LEXICAL_THRESHOLD", "0.65"))  # Lowered for better fuzzy matching
SEMANTIC_THRESHOLD = float(os.getenv("SEMANTIC_THRESHOLD", "0.55"))  # Lowered for concept matching
SKIP_SEMANTIC_THRESHOLD = float(os.getenv("SKIP_SEMANTIC_THRESHOLD", "0.80"))  # Skip semantic if lexical is this good

# Maximum candidates to score (performance optimization)
MAX_CANDIDATES_TO_SCORE = int(os.getenv("MAX_CANDIDATES_TO_SCORE", "100"))

# CAPTCHA detection selectors (comprehensive list)
CAPTCHA_IFRAME_SELECTORS = [
    "iframe[src*='cloudflare']",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='funcaptcha']",
    "iframe[src*='arkoselabs']",
    "iframe[src*='geetest']",
    "iframe[title*='captcha' i]",
    "iframe[title*='challenge' i]",
]

CAPTCHA_ELEMENT_SELECTORS = [
    "#px-captcha",                    # PerimeterX
    "[class*='datadome']",            # DataDome
    ".geetest_panel",                 # GeeTest
    ".cf-turnstile",                  # Cloudflare Turnstile
    "[data-sitekey]",                 # Generic reCAPTCHA
    "#akamai-bmp",                    # Akamai Bot Manager
]

CAPTCHA_TITLE_KEYWORDS = [
    "just a moment",
    "security check",
    "checking your browser",
    "please verify",
    "one moment please",
    "access denied",
    "attention required",
    "ddos protection",
    "bot detection",
]

CAPTCHA_BODY_KEYWORDS = [
    "verify you are human",
    "prove you're not a robot",
    "complete the security check",
    "enable javascript and cookies",
    "unusual traffic",
]


# =============================================================================
# HYBRID SCORER - Custom High-Performance Mathematical Algorithm
# =============================================================================

class HybridScorer:
    """
    Advanced multi-signal scoring engine using custom mathematical algorithms.

    Combines 5 scoring methods with weighted fusion:
    1. EXACT MATCH - Direct substring containment (fastest)
    2. N-GRAM OVERLAP - Character-level similarity using Jaccard index
    3. WORD OVERLAP - Token-level intersection with IDF weighting
    4. LEVENSHTEIN - Edit distance normalized by length
    5. VECTOR COSINE - Semantic embedding similarity (slowest, most powerful)

    Mathematical Formula:
        final_score = Σ(wi × si × ci) / Σ(wi)

    Where:
        wi = weight for method i
        si = raw score from method i
        ci = confidence from intent expansion
    """

    # Weights tuned for browser automation
    WEIGHTS = {
        "exact": 1.0,      # Highest priority - exact matches are definitive
        "ngram": 0.7,      # Good for typos and variations
        "word": 0.8,       # Good for reordered phrases
        "levenshtein": 0.6, # Fallback for similar strings
        "vector": 0.9,     # High weight for semantic understanding
    }

    def __init__(self, lexical_scorer: LevenshteinScorer, tensor_engine: TensorEngine):
        self.lexical = lexical_scorer
        self.tensor = tensor_engine
        self.expander = get_intent_expander()

        # Pre-compute common word IDF values
        self._idf_cache = {}

    @lru_cache(maxsize=1024)
    def _compute_ngrams(self, text: str, n: int = 3) -> frozenset:
        """Generate character n-grams from text."""
        text = text.lower().strip()
        if len(text) < n:
            return frozenset([text])
        return frozenset(text[i:i+n] for i in range(len(text) - n + 1))

    def _ngram_similarity(self, text1: str, text2: str, n: int = 3) -> float:
        """
        Jaccard similarity of character n-grams.

        Formula: |A ∩ B| / |A ∪ B|

        This is robust to:
        - Minor typos (1-2 char differences)
        - Word order changes
        - Case variations
        """
        ngrams1 = self._compute_ngrams(text1, n)
        ngrams2 = self._compute_ngrams(text2, n)

        intersection = len(ngrams1 & ngrams2)
        union = len(ngrams1 | ngrams2)

        if union == 0:
            return 0.0

        return intersection / union

    def _word_overlap_score(self, text1: str, text2: str) -> float:
        """
        Token overlap with inverse document frequency weighting.

        Rare words (like "CEO", "Jensen") get higher weight than
        common words (like "the", "a", "company").

        Formula: Σ(IDF(w) × match(w)) / Σ(IDF(w))
        """
        # Common stop words get low IDF
        STOP_WORDS = {
            "the", "a", "an", "to", "for", "of", "in", "on", "at",
            "and", "or", "is", "are", "was", "were", "be", "been",
            "this", "that", "with", "by", "from", "as", "it"
        }

        words1 = set(w.lower() for w in text1.split() if len(w) > 1)
        words2 = set(w.lower() for w in text2.split() if len(w) > 1)

        if not words1 or not words2:
            return 0.0

        # Compute weighted overlap
        total_weight = 0.0
        matched_weight = 0.0

        for word in words1:
            # IDF: rare words get higher weight
            idf = 0.1 if word in STOP_WORDS else 1.0
            total_weight += idf

            if word in words2:
                matched_weight += idf

        if total_weight == 0:
            return 0.0

        return matched_weight / total_weight

    def _exact_match_score(self, intent: str, element_text: str) -> float:
        """
        Direct substring matching with position weighting.

        Matches at the START of element text get bonus (more likely to be primary label).
        """
        intent_lower = intent.lower().strip()
        element_lower = element_text.lower().strip()

        if not intent_lower or not element_lower:
            return 0.0

        # Exact containment
        if intent_lower in element_lower:
            # Bonus for position - matches at start are more relevant
            pos = element_lower.find(intent_lower)
            position_bonus = 1.0 - (pos / max(len(element_lower), 1)) * 0.2
            return min(1.0, 0.95 * position_bonus)

        # Word-level exact match
        intent_words = set(intent_lower.split())
        element_words = set(element_lower.split())

        if intent_words and intent_words.issubset(element_words):
            return 0.85

        return 0.0

    def score(
        self,
        intent: str,
        element_text: str,
        page_url: str = "",
        use_vector: bool = True
    ) -> Tuple[float, str, str]:
        """
        Compute hybrid score using all available methods.

        Args:
            intent: User's intent
            element_text: Combined element text
            page_url: Current URL for context-aware expansion
            use_vector: Whether to use vector similarity (slower)

        Returns:
            Tuple of (final_score, matched_term, scoring_method)
        """
        if not element_text or not element_text.strip():
            return 0.0, intent, "EMPTY"

        # Get expanded intents with confidence scores
        expansions = self.expander.get_semantic_expansions(intent, page_url)

        best_score = 0.0
        best_term = intent
        best_method = "NONE"

        for term, confidence in expansions.items():
            # 1. EXACT MATCH (fastest)
            exact = self._exact_match_score(term, element_text)
            if exact > 0:
                weighted = exact * confidence * self.WEIGHTS["exact"]
                if weighted > best_score:
                    best_score = weighted
                    best_term = term
                    best_method = "EXACT"

            # 2. N-GRAM OVERLAP
            ngram = self._ngram_similarity(term, element_text)
            weighted = ngram * confidence * self.WEIGHTS["ngram"]
            if weighted > best_score:
                best_score = weighted
                best_term = term
                best_method = "NGRAM"

            # 3. WORD OVERLAP
            word_overlap = self._word_overlap_score(term, element_text)
            weighted = word_overlap * confidence * self.WEIGHTS["word"]
            if weighted > best_score:
                best_score = weighted
                best_term = term
                best_method = "WORD_OVERLAP"

            # 4. LEVENSHTEIN
            lev = self.lexical.score(term.lower(), element_text.lower())
            weighted = lev * confidence * self.WEIGHTS["levenshtein"]
            if weighted > best_score:
                best_score = weighted
                best_term = term
                best_method = "LEVENSHTEIN"

        # 5. VECTOR SIMILARITY (only if no confident match found)
        if use_vector and best_score < SKIP_SEMANTIC_THRESHOLD:
            try:
                # Use original intent for vector comparison
                vec_score = self._compute_vector_score(intent, element_text)
                weighted = vec_score * self.WEIGHTS["vector"]
                if weighted > best_score:
                    best_score = weighted
                    best_term = intent
                    best_method = "VECTOR"
            except Exception as e:
                logger.debug(f"Vector scoring failed: {e}")

        return best_score, best_term, best_method

    def _compute_vector_score(self, intent: str, element_text: str) -> float:
        """Compute cosine similarity using embeddings."""
        if not element_text.strip():
            return 0.0

        intent_vec = self.tensor.model.encode(
            intent.lower().strip(),
            convert_to_numpy=True,
            normalize_embeddings=True
        )

        element_vec = self.tensor.model.encode(
            element_text.lower().strip()[:500],  # Limit length
            convert_to_numpy=True,
            normalize_embeddings=True
        )

        # Cosine similarity (vectors already normalized)
        return float(np.dot(intent_vec, element_vec))



class SmartFinder:
    """
    The Glass Box Element Finder with Hybrid Semantic Scoring.

    Pipeline:
    1. Common Sense Check (Cortex)
    2. CAPTCHA Detection
    3. Memory Check (Fast Path)
    4. Hydration Wait & Candidate Extraction (Math Path)
    5.  Hybrid Scoring (Lexical + Semantic Vector)
    6. Verification (Physics)
    7. Learning (Save Pattern)
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.glass = GlassBoxEngine()
        self.scorer = LevenshteinScorer()
        self.cortex = TheCortex()
        self.healer = TheHealer()
        self.pattern_db = PatternDB()
        self.tensor = TensorEngine()

        # [NEW] HybridScorer with intent expansion and multi-method scoring
        self.hybrid_scorer = HybridScorer(self.scorer, self.tensor)
        self.intent_expander = get_intent_expander()

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

    async def detect_captcha(self, page: Page) -> None:
        """
        Comprehensive CAPTCHA detection covering all major providers.

        Detects:
        - Cloudflare (challenge page, Turnstile)
        - reCAPTCHA v2/v3
        - hCaptcha
        - FunCaptcha (Arkose Labs)
        - PerimeterX
        - DataDome
        - GeeTest
        - Akamai Bot Manager

        Raises:
            HumanInterventionRequired: If CAPTCHA detected, triggers workflow hibernation.
        """
        captcha_detected = False
        captcha_type = None
        detection_method = None

        # 1. Check for CAPTCHA iframes
        for selector in CAPTCHA_IFRAME_SELECTORS:
            try:
                if await page.query_selector(selector):
                    captcha_detected = True
                    captcha_type = f"iframe:{selector}"
                    detection_method = "iframe_selector"
                    break
            except Exception:
                continue

        # 2. Check for CAPTCHA elements (non-iframe)
        if not captcha_detected:
            for selector in CAPTCHA_ELEMENT_SELECTORS:
                try:
                    if await page.query_selector(selector):
                        captcha_detected = True
                        captcha_type = f"element:{selector}"
                        detection_method = "element_selector"
                        break
                except Exception:
                    continue

        # 3. Check page title for challenge keywords
        if not captcha_detected:
            try:
                title = (await page.title() or "").lower()
                for keyword in CAPTCHA_TITLE_KEYWORDS:
                    if keyword in title:
                        captcha_detected = True
                        captcha_type = f"title:{keyword}"
                        detection_method = "title_keyword"
                        break
            except Exception:
                pass

        # 4. Check body text for challenge keywords (lightweight check)
        if not captcha_detected:
            try:
                # Only check first 2000 chars to avoid performance hit
                body_text = await page.evaluate("document.body?.innerText?.substring(0, 2000)?.toLowerCase() || ''")
                for keyword in CAPTCHA_BODY_KEYWORDS:
                    if keyword in body_text:
                        captcha_detected = True
                        captcha_type = f"body:{keyword}"
                        detection_method = "body_keyword"
                        break
            except Exception:
                pass

        if captcha_detected:
            logger.warning(f"[{self.job_id}] 🚧 CAPTCHA Detected! Type: {captcha_type}")

            raise HumanInterventionRequired(
                reason="CAPTCHA_DETECTED",
                context={
                    "url": page.url,
                    "captcha_type": captcha_type,
                    "detection_method": detection_method,
                    "job_id": self.job_id,
                    "hint": "Complete the CAPTCHA and click 'Resume' in the dashboard"
                }
            )

    def _compute_semantic_score(self, intent: str, element_text: str) -> float:
        """
        Computes semantic similarity using vector embeddings.

        This is the "Neural" part of the neuro-symbolic approach.
        Uses SentenceTransformers to understand intent conceptually,
        not just lexically.

        Args:
            intent: User's search intent (e.g., "person who runs company")
            element_text: Combined element text (innerText + aria-label + id)

        Returns:
            Cosine similarity score (0.0-1.0)

        Example:
            intent = "person who runs the company"
            element_text = "Jensen Huang President and CEO"

            # Embeddings capture semantic overlap:
            # "runs company" ≈ "CEO" ≈ "President"
            score = 0.72  # PASS (above 0.60 threshold)
        """
        # Edge case: empty element
        if not element_text or not element_text.strip():
            return 0.0

        try:
            # Clean inputs
            intent_clean = intent.lower().strip()
            element_clean = element_text.lower().strip()

            # Generate embeddings (384-dimensional vectors)
            intent_vector = self.tensor.model.encode(
                intent_clean,
                convert_to_numpy=True,
                normalize_embeddings=True
            )

            element_vector = self.tensor.model.encode(
                element_clean,
                convert_to_numpy=True,
                normalize_embeddings=True
            )

            # Cosine similarity (dot product of normalized vectors)
            similarity = float(np.dot(intent_vector, element_vector))

            # Clamp to [0.0, 1.0] for safety
            similarity = np.clip(similarity, 0.0, 1.0)

            logger.debug(
                f"[Logic] Semantic: '{intent_clean[:30]}...' vs '{element_clean[:30]}...' → {similarity:.3f}"
            )

            return similarity

        except Exception as e:
            logger.warning(f"[Warning] Semantic scoring failed: {e}")
            return 0.0

    async def find(self, page: Page, intent: str) -> ElementHandle:
        """
        Finds an element using the Glass Box pipeline with Muscle Memory.

        Workflow:
        1. Common Sense Check
        2. CAPTCHA Check
        3. Memory Check (Fast Path ~10-25ms)
        4. Math Path (Slow Path ~150-300ms)
        5. Learning (Save successful pattern)
        """
        logger.info(f"[{self.job_id}] SmartFinder searching for: '{intent}'")

        # --- STEP 1: COMMON SENSE CHECK ---
        if not await self.cortex.validate_action(page, intent):
            raise Exception(
                f"Action '{intent}' is illogical for the current page context."
            )

        # --- STEP 1.5: CAPTCHA CHECK ---
        # detect_captcha now raises HumanInterventionRequired exception directly
        # No need to check return value - exception propagates to workflow
        await self.detect_captcha(page)

        # --- STEP 2: MEMORY CHECK (FAST PATH) ---
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
            # Gather diagnostics for better error messages
            page_url = page.url
            page_title = await page.title() if page else "unknown"
            total_elements = await page.evaluate("document.querySelectorAll('*').length") if page else 0

            raise Exception(
                f"Page interaction failed: No visible interactive elements found.\n"
                f"  URL: {page_url}\n"
                f"  Title: {page_title}\n"
                f"  Total DOM elements: {total_elements}\n"
                f"  Intent: '{intent}'\n"
                f"  Possible causes: Page still loading, JS error, or all elements hidden/disabled"
            )

        # --- STEP 4: HYBRID SCORING (THE SYNAPSE) ---
        # Limit candidates to avoid performance issues on large pages
        candidates_to_score = candidates[:MAX_CANDIDATES_TO_SCORE]
        if len(candidates) > MAX_CANDIDATES_TO_SCORE:
            logger.info(f"[Perf] Scoring top {MAX_CANDIDATES_TO_SCORE} of {len(candidates)} candidates")

        best_match = None
        best_score = -1.0
        best_method = "unknown"
        best_content = ""

        for el in candidates_to_score:
            try:
                # INDUSTRIAL: Extract comprehensive element attributes for icon-only buttons
                text = (await el.inner_text() or "").strip()
                aria = await el.get_attribute("aria-label") or ""
                eid = await el.get_attribute("id") or ""
                title_attr = await el.get_attribute("title") or ""
                placeholder = await el.get_attribute("placeholder") or ""
                name_attr = await el.get_attribute("name") or ""
                data_testid = await el.get_attribute("data-testid") or ""

                # Extract icon hints from class names
                classes = (await el.get_attribute("class") or "").lower()
                icon_hint = ""
                icon_keywords = {
                    "search": "search find",
                    "cart": "cart shopping basket",
                    "user": "user profile account",
                    "menu": "menu hamburger",
                    "close": "close dismiss x",
                    "add": "add plus",
                    "remove": "remove delete minus",
                    "edit": "edit pencil",
                    "save": "save disk",
                    "submit": "submit send",
                    "login": "login signin",
                    "logout": "logout signout",
                    "settings": "settings gear cog",
                    "home": "home house",
                    "back": "back arrow left",
                    "next": "next arrow right forward",
                }
                for keyword, hints in icon_keywords.items():
                    if keyword in classes:
                        icon_hint = hints
                        break

                # Combine all sources for scoring
                content = " ".join(filter(None, [
                    text, aria, eid, title_attr, placeholder, name_attr, data_testid, icon_hint
                ])).strip()

                # Skip empty elements
                if not content:
                    continue

                # =========================================================
                # HYBRID SCORING with Intent Expansion
                # Uses: Exact, N-gram, Word Overlap, Levenshtein, Vector
                # =========================================================
                final_score, matched_term, current_method = self.hybrid_scorer.score(
                    intent=intent,
                    element_text=content,
                    page_url=page.url,
                    use_vector=(len(content.split()) > 2)  # Skip vector for short text
                )

                if final_score > best_score:
                    best_score = final_score
                    best_match = el
                    best_method = current_method
                    best_content = content[:100]
                    best_matched_term = matched_term  # Track which expanded term matched

            except Exception as e:
                logger.debug(f"[Scoring] Error scoring element: {e}")
                continue

        # --- STEP 5: VERIFICATION ---
        # Use configurable thresholds from environment
        threshold = SEMANTIC_THRESHOLD if best_method == "SEMANTIC" else LEXICAL_THRESHOLD

        if best_match and best_score > threshold:
            logger.info(
                f"[System] Element matched: Score: {best_score:.2f} "
                f"(Method: {best_method}, Threshold: {threshold}, Content: '{best_content[:50]}...')"
            )

            # --- STEP 6: LEARNING (SAVE PATTERN) ---
            try:
                selector = await self._generate_selector(page, best_match)
                self.pattern_db.save_pattern(domain, page_hash, intent, selector)
                logger.debug(f"[Storage] Pattern learned: {selector[:50]}...")
            except Exception as e:
                logger.warning(f"[Warning] Failed to save pattern: {e}")

            # --- STEP 7: PHYSICS CHECK (Handle Obscured Elements) ---
            if await self.glass.is_physically_clickable(page, best_match):
                return best_match
            else:
                logger.warning(f"[{self.job_id}] Element is obscured, attempting to fix...")

                # Try 1: Scroll element into view
                try:
                    await best_match.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)  # Wait for scroll animation

                    if await self.glass.is_physically_clickable(page, best_match):
                        logger.info("[System] Element now clickable after scroll")
                        return best_match
                except Exception as scroll_err:
                    logger.debug(f"Scroll failed: {scroll_err}")

                # Try 2: Click at element center using JavaScript (bypasses overlays)
                logger.warning("[System] Returning element anyway - click_with_retry will handle overlay dismissal")
                return best_match

        # --- STEP 8: FAILURE WITH DIAGNOSTICS ---
        # Collect diagnostic information for debugging
        top_candidates = []
        for i, el in enumerate(candidates_to_score[:5]):
            try:
                text = (await el.inner_text() or "")[:50]
                aria = await el.get_attribute("aria-label") or ""
                top_candidates.append(f"  {i+1}. '{text}' (aria: '{aria}')")
            except:
                continue

        error_msg = (
            f"Element '{intent}' not found (best score: {best_score:.2f}, threshold: {threshold})\n"
            f"  URL: {page.url}\n"
            f"  Candidates scanned: {len(candidates_to_score)}\n"
            f"  Top candidates:\n" + "\n".join(top_candidates)
        )

        logger.error(f"[{self.job_id}] {error_msg}")
        raise Exception(error_msg)

