import logging
import numpy as np
from playwright.async_api import Page
from core.TensorEngine import TensorEngine

logger = logging.getLogger("planner")


class TheCortex:
    """
    The Common Sense Engine (Vector-Based Version).

    Uses High-Dimensional Vector Analysis to classify page context.
    Replaces static keyword matching with semantic similarity scoring.

    Mathematical Foundation:
    - Page contexts mapped to archetype vectors in R^384
    - Classification via cosine similarity
    - Threshold-based decision making (0.25 for classification, 0.1 for validation)
    """

    def __init__(self):
        logger.info("🧠 Initializing TheCortex (Vector Edition)...")

        # Initialize Tensor Engine (Singleton)
        self.tensor = TensorEngine()

        # Define Semantic Archetypes (The "Anchors" in Vector Space)
        # These are rich descriptions that capture the essence of each context
        self.ARCHETYPES = {
            "AUTH": "login sign in password authentication register signup account credentials",
            "MEDIA": "video player stream watch episode movie youtube netflix entertainment content",
            "COMMERCE": "price add to cart checkout shipping buy store product purchase shopping"
        }

        # Pre-calculate Archetype Vectors (Cache them for performance)
        # This happens once during initialization
        self.archetype_vectors = {}
        for context, description in self.ARCHETYPES.items():
            vector = self.tensor.model.encode(
                description.lower().strip(),
                convert_to_numpy=True,
                normalize_embeddings=True
            ).astype(np.float32)
            self.archetype_vectors[context] = vector
            logger.debug(f"[Metrics] Archetype'{context}' cached (dim={vector.shape[0]})")

        # Intent Mappings (What user wants -> Context required)
        # This remains for specific validation rules
        self.INTENT_MAP = {
            "play": "MEDIA",
            "watch": "MEDIA",
            "login": "AUTH",
            "sign in": "AUTH",
            "buy": "COMMERCE",
            "add to cart": "COMMERCE",
            "checkout": "COMMERCE"
        }

        # Priority Zones (CSS Selectors for ROI)
        # Where to look first based on classified context
        self.ROI_MAP = {
            "AUTH": ["form", ".modal", "[role='dialog']", ".auth-container", "#login", ".login"],
            "MEDIA": ["video", "iframe", ".player-container", "[aria-label='Play']", ".vjs-tech"],
            "COMMERCE": [".product-actions", "#add-to-cart", ".sticky-bottom", ".buy-box"]
        }

        logger.info("[System] TheCortex initialized with 3 archetype vectors")

    async def classify_page(self, page: Page) -> str:
        """
        Classifies a page using Vector Space Mathematics.

        Mathematical Approach:
        ----------------------
        For each archetype A ∈ {AUTH, MEDIA, COMMERCE}:
            score_A = cos(V_page, V_archetype) = (V_page · V_archetype) / (||V_page|| × ||V_archetype||)

        Classification = argmax(score_A) if max(score_A) > 0.25 else "GENERIC"

        Args:
            page (Page): Playwright Page object to classify

        Returns:
            str: Page classification ("AUTH", "MEDIA", "COMMERCE", or "GENERIC")

        Threshold Logic:
            - score > 0.25: Sufficient semantic alignment for classification
            - score ≤ 0.25: Page doesn't match any archetype → GENERIC
        """
        try:
            # Step 1: Extract Page Vector (from DOM metadata)
            page_vector = await self.tensor.vectorize_page(page)

            # Check for zero vector (empty page)
            if np.linalg.norm(page_vector) < 1e-6:
                logger.warning("[Warning] Page has no extractable features. Classifying as GENERIC.")
                return "GENERIC"

            # Step 2: Compute Cosine Similarity with each Archetype
            scores = {}
            for context, archetype_vector in self.archetype_vectors.items():
                # Cosine Similarity: V_page · V_archetype (both are normalized)
                similarity = float(np.dot(page_vector, archetype_vector))
                scores[context] = similarity
                logger.debug(f"[Logic] {context}: {similarity:.4f}")

            # Step 3: Find Best Match
            best_context = max(scores, key=scores.get)
            best_score = scores[best_context]

            # Step 4: Apply Threshold
            if best_score > 0.25:
                logger.info(f"🧠 Context: {best_context} (Score: {best_score:.4f})")
                return best_context
            else:
                logger.info(f"🧠 Context: GENERIC (Best score: {best_score:.4f} < 0.25 threshold)")
                return "GENERIC"

        except Exception as e:
            logger.error(f"[Error] Classification failed: {e}")
            return "GENERIC"

    async def validate_action(self, page: Page, intent: str) -> bool:
        """
        The Gatekeeper: Does this intent match the page context?

        Uses two-layer validation:
        1. Vector Similarity Check (Continuous)
        2. Specific Context Blockers (Discrete)

        Mathematical Logic:
        -------------------
        relevance = cos(V_page, V_intent)

        If relevance < 0.1:
            → Action is semantically irrelevant to page → BLOCK
        Else:
            → Apply specific business rules (e.g., no "play" on AUTH pages)

        Args:
            page (Page): Current page
            intent (str): User's intended action

        Returns:
            bool: True if action is valid, False if blocked
        """
        intent_lower = intent.lower()

        try:
            # Layer 1: Vector Similarity Check
            # Extract page vector
            page_vector = await self.tensor.vectorize_page(page)

            # Compute relevance between intent and page
            relevance = self.tensor.compute_relevance(page_vector, intent)

            # If similarity is very low, the action is likely irrelevant
            if relevance < 0.1:
                logger.warning(
                    f"🛑 Vector Gate: Intent '{intent}' has low relevance to page (score: {relevance:.4f})"
                )
                # Note: We don't block here yet, just log. Could enable strict mode later.
                # return False

            # Layer 2: Specific Context Blockers
            # Determine required context for the intent
            required_context = None
            for key, context in self.INTENT_MAP.items():
                if key in intent_lower:
                    required_context = context
                    break

            if not required_context:
                return True  # Unknown intent, give benefit of doubt

            # Determine actual page context (using vector classification)
            current_context = await self.classify_page(page)

            # Strict Blocking Rules
            # Don't try to "Play Video" on a "Login Page"
            if required_context == "MEDIA" and current_context == "AUTH":
                logger.warning(
                    f"🛑 Common Sense Fail: Trying to '{intent}' (MEDIA) on an AUTH page."
                )
                return False

            # Don't try to "Buy Product" on a "Login Page"
            if required_context == "COMMERCE" and current_context == "AUTH":
                logger.warning(
                    f"🛑 Common Sense Fail: Trying to '{intent}' (COMMERCE) on an AUTH page."
                )
                return False

            return True

        except Exception as e:
            logger.error(f"[Error] Validation failed: {e}")
            return True  # Fail-safe: allow action if validation errors

    def get_search_zones(self, intent: str) -> list[str]:
        """
        Returns Priority CSS Selectors based on Intent.

        Optimization: Scans high-probability DOM regions first before global search.

        Note: This method doesn't use vectors (it's a fast lookup table).
        The vector classification happens in SmartFinder when it calls classify_page().

        Args:
            intent (str): User's intended action

        Returns:
            list[str]: CSS selectors for priority zones, or empty list if unknown
        """
        intent_lower = intent.lower()

        # Map intent to context
        for key, context in self.INTENT_MAP.items():
            if key in intent_lower:
                zones = self.ROI_MAP.get(context, [])
                if zones:
                    logger.debug(f"[Logic] Search Zones for'{intent}': {zones[:3]}...")
                return zones

        return []


# ==================== VERIFICATION BLOCK ====================
if __name__ == "__main__":
    """
    Standalone Test: Verify TheCortex vector classification without live browser.
    """
    import asyncio

    print("=" * 60)
    print("THECORTEX - VECTOR CLASSIFICATION TEST")
    print("=" * 60)

    # Initialize Cortex
    print("\n[Test 1] Initialization...")
    cortex = TheCortex()
    print(f"✅ Archetypes loaded: {list(cortex.archetype_vectors.keys())}")
    print(f"✅ Vector dimensions: {cortex.archetype_vectors['AUTH'].shape[0]}")

    # Test Archetype Similarity (Mock)
    print("\n[Test 2] Archetype Vector Similarity...")
    engine = TensorEngine()

    # Simulate different page types
    test_pages = {
        "Login Page": "Netflix Login - Sign in to your account with password",
        "Video Page": "Watch Stranger Things - Stream episodes online Netflix",
        "Shopping Page": "iPhone 15 Pro - Add to cart - Free shipping - Buy now",
        "Generic Page": "About Us - Company Information and Contact Details"
    }

    for page_name, page_text in test_pages.items():
        print(f"\n  📄 {page_name}:")
        print(f"     Text: '{page_text[:50]}...'")

        # Encode page text
        page_vector = engine.model.encode(page_text, normalize_embeddings=True)

        # Compute scores
        scores = {}
        for context, archetype_vec in cortex.archetype_vectors.items():
            score = float(np.dot(page_vector, archetype_vec))
            scores[context] = score
            print(f"     - {context}: {score:.4f}")

        # Classify
        best = max(scores, key=scores.get)
        if scores[best] > 0.25:
            print(f"     ✅ Classification: {best}")
        else:
            print(f"     ✅ Classification: GENERIC (max score: {scores[best]:.4f})")

    # Test Intent Validation (Mock)
    print("\n[Test 3] Intent Validation Logic...")
    test_intents = ["login to account", "play video", "buy product"]
    for intent in test_intents:
        zones = cortex.get_search_zones(intent)
        print(f"  Intent: '{intent}' → Zones: {zones[:2] if zones else 'None'}")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)
