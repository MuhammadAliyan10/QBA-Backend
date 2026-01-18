"""
smartFinderV2.py - Hybrid Selector Engine with 4-Layer Fallback

The SmartFinder is the core element-finding engine that makes our automation
resilient to UI changes. Instead of relying on brittle CSS selectors or XPath,
it uses a multi-layer approach:

    Layer 1 (REFLEX - <10ms):
        SimHash fingerprint matching - instant recognition of known elements

    Layer 2 (HEURISTIC - ~50ms):
        Fuzzy text matching using Levenshtein distance on interactive elements

    Layer 3 (SEMANTIC - ~200ms):
        Vector database search for semantically similar elements

    Layer 4 (COGNITIVE - Slow):
        AI-powered recovery using LLM to analyze the page

SELF-HEALING:
    When Layer 1 fails but a deeper layer succeeds, we UPDATE the element's
    fingerprint in the recipe. This allows the system to learn and adapt.

Usage:
    finder = SmartFinder(page)

    # Find element by intent
    element = await finder.find("Login Button")

    # Find with metadata (for self-healing)
    element, healed = await finder.find_with_healing(
        intent="Login Button",
        metadata={"simhash": "abc123..."}
    )
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page, ElementHandle, Locator

# Internal imports
from core.selector.utils.mathUtils import (
    compute_simhash,
    simhash_similarity,
    levenshtein_ratio,
    hybrid_similarity,
    normalize_text,
    compute_element_signature
)

logger = logging.getLogger("smartFinderV2")


# =============================================================================
# ENUMS & DATA CLASSES
# =============================================================================

class FinderLayer(Enum):
    """Which layer found the element."""
    REFLEX = 1      # SimHash match (<10ms)
    HEURISTIC = 2   # Levenshtein match (~50ms)
    SEMANTIC = 3    # Vector DB match (~200ms)
    COGNITIVE = 4   # AI recovery (slow)
    NONE = 0        # Not found


@dataclass
class FindResult:
    """Result of element finding operation."""
    element: Optional[ElementHandle] = None
    layer: FinderLayer = FinderLayer.NONE
    confidence: float = 0.0
    duration_ms: int = 0
    new_signature: Optional[Dict] = None  # For self-healing
    candidates_checked: int = 0
    error: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.element is not None

    @property
    def needs_healing(self) -> bool:
        """True if a deeper layer found the element (Layer 1 failed)."""
        return self.found and self.layer.value > 1


@dataclass
class ElementCandidate:
    """A candidate element during search."""
    handle: ElementHandle
    tag: str = ""
    text: str = ""
    classes: List[str] = field(default_factory=list)
    attributes: Dict[str, str] = field(default_factory=dict)
    score: float = 0.0
    simhash: str = ""


# =============================================================================
# VECTOR DB - Qdrant Integration for Layer 3 (Semantic Search)
# =============================================================================

class QdrantVectorDB:
    """
    Vector Database for Layer 3 (Semantic search) using Qdrant.

    Stores element signatures with semantic embeddings for fast similarity search.
    Falls back gracefully when Qdrant is unavailable.
    """

    def __init__(
        self,
        url: str = None,
        collection_name: str = "element_signatures",
        api_key: str = None
    ):
        """
        Initialize Qdrant client.

        Args:
            url: Qdrant server URL (default: from QDRANT_URL env var)
            collection_name: Name of the collection to search
            api_key: API key for Qdrant Cloud (optional)
        """
        import os
        self.url = url or os.getenv("QDRANT_URL", "http://localhost:6333")
        self.api_key = api_key or os.getenv("QDRANT_API_KEY")
        self.collection_name = collection_name
        self._client = None
        self._initialized = False

    async def _ensure_client(self):
        """Lazy-load Qdrant client."""
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
                from qdrant_client.http import models

                self._client = QdrantClient(
                    url=self.url,
                    api_key=self.api_key,
                    timeout=5.0  # Fast timeout for element finding
                )

                # Check if collection exists, create if not
                try:
                    collections = self._client.get_collections().collections
                    exists = any(c.name == self.collection_name for c in collections)

                    if not exists:
                        logger.info(f"[VectorDB] Creating collection '{self.collection_name}'")
                        self._client.create_collection(
                            collection_name=self.collection_name,
                            vectors_config=models.VectorParams(
                                size=384,  # all-MiniLM-L6-v2 dimension
                                distance=models.Distance.COSINE
                            )
                        )
                except Exception as e:
                    logger.warning(f"[VectorDB] Failed to ensure collection exists: {e}")

                self._initialized = True
                logger.info(f"[VectorDB] Connected to Qdrant at {self.url}")
            except ImportError:
                logger.warning("[VectorDB] qdrant-client not installed, using mock mode")
                self._initialized = False
            except Exception as e:
                logger.warning(f"[VectorDB] Failed to connect to Qdrant: {e}")
                self._initialized = False

    async def search(self, intent: str, page_context: str = "") -> Optional[Dict]:
        """
        Search for an element by semantic intent.

        Args:
            intent: Natural language description of the element
            page_context: Optional page URL or context for filtering

        Returns:
            Dict with selector/attributes if found, None otherwise
        """
        await self._ensure_client()

        if not self._initialized:
            logger.debug("[VectorDB] Not initialized, returning None")
            return None

        try:
            # Generate embedding for intent
            embedding = await self._get_embedding(intent)
            if not embedding:
                return None

            # Search Qdrant
            results = self._client.search(
                collection_name=self.collection_name,
                query_vector=embedding,
                limit=1,
                score_threshold=0.7
            )

            if results:
                match = results[0]
                return {
                    "selector": match.payload.get("selector"),
                    "score": match.score,
                    "intent": match.payload.get("intent"),
                    "attributes": match.payload.get("attributes", {})
                }

        except Exception as e:
            logger.warning(f"[VectorDB] Search failed: {e}")

        return None

    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding vector for text using sentence-transformers."""
        try:
            from sentence_transformers import SentenceTransformer

            # Use cached model (lazy load)
            if not hasattr(self, '_model'):
                self._model = SentenceTransformer('all-MiniLM-L6-v2')

            embedding = self._model.encode(text).tolist()
            return embedding

        except ImportError:
            logger.debug("[VectorDB] sentence-transformers not installed")
            return None
        except Exception as e:
            logger.debug(f"[VectorDB] Embedding failed: {e}")
            return None

    async def store(self, intent: str, selector: str, attributes: Dict = None):
        """Store an element signature for future semantic search."""
        await self._ensure_client()

        if not self._initialized:
            return False

        try:
            from qdrant_client.http import models
            import uuid

            embedding = await self._get_embedding(intent)
            if not embedding:
                return False

            self._client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=embedding,
                        payload={
                            "intent": intent,
                            "selector": selector,
                            "attributes": attributes or {}
                        }
                    )
                ]
            )
            return True

        except Exception as e:
            logger.warning(f"[VectorDB] Store failed: {e}")
            return False


# =============================================================================
# AI AGENT - LLM Integration for Layer 4 (Cognitive Recovery)
# =============================================================================

class LLMAgent:
    """
    AI Agent for Layer 4 (Cognitive recovery) using LLM.

    Analyzes page HTML and uses reasoning to find elements.
    Falls back gracefully when LLM is unavailable.
    """

    def __init__(
        self,
        model: str = None,
        api_key: str = None,
        base_url: str = None
    ):
        """
        Initialize LLM client.

        Args:
            model: Model name (default: from LLM_MODEL env var)
            api_key: API key (default: from LLM_API_KEY env var)
            base_url: API base URL (default: from LLM_BASE_URL env var)
        """
        import os
        self.model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("LLM_BASE_URL")
        self._client = None
        self._initialized = False

    async def _ensure_client(self):
        """Lazy-load LLM client."""
        if self._client is None and self.api_key:
            try:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=10.0
                )
                self._initialized = True
                logger.info(f"[AIAgent] Initialized with model: {self.model}")
            except ImportError:
                logger.warning("[AIAgent] openai not installed, using mock mode")
                self._initialized = False
            except Exception as e:
                logger.warning(f"[AIAgent] Failed to initialize: {e}")
                self._initialized = False

    async def recover(
        self,
        intent: str,
        page_html: str = "",
        screenshot: bytes = None
    ) -> Optional[str]:
        """
        Use AI to find an element selector.

        Args:
            intent: Natural language description of the element
            page_html: Truncated page HTML for context
            screenshot: Optional screenshot bytes

        Returns:
            CSS selector string if found, None otherwise
        """
        await self._ensure_client()

        if not self._initialized:
            logger.debug("[AIAgent] Not initialized, returning None")
            return None

        try:
            # Build prompt
            prompt = self._build_prompt(intent, page_html)

            # Call LLM
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert at finding HTML elements. "
                            "Given a description and HTML, return ONLY a valid CSS selector. "
                            "Return just the selector, nothing else. "
                            "If you cannot find a matching element, return 'NOT_FOUND'."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                max_tokens=100,
                temperature=0
            )

            selector = response.choices[0].message.content.strip()

            # Validate response
            if selector and selector != "NOT_FOUND" and len(selector) < 200:
                logger.info(f"[AIAgent] Found selector: {selector[:50]}...")
                return selector

            return None

        except Exception as e:
            logger.warning(f"[AIAgent] Recovery failed: {e}")
            return None

    def _build_prompt(self, intent: str, page_html: str) -> str:
        """Build the prompt for the LLM."""
        # Truncate HTML to avoid token limits
        html_preview = page_html[:15000] if page_html else "No HTML available"

        return f"""Find the element: "{intent}"

HTML (truncated):
```html
{html_preview}
```

Return a CSS selector that uniquely identifies this element."""


# =============================================================================
# BACKWARDS COMPATIBILITY - Mock aliases
# =============================================================================

# Keep old names for backwards compatibility
MockVectorDB = QdrantVectorDB
MockAIAgent = LLMAgent


# =============================================================================
# SMART FINDER - Main Class
# =============================================================================

class SmartFinder:
    """
    Hybrid Selector Engine with 4-Layer Fallback.

    AUDIT FIXES APPLIED:
    ====================
    - MAX_CANDIDATES limit to prevent performance bomb
    - Shadow DOM piercing via >>> combinator
    - iFrame recursion via page.frames
    - Contextual container scoping
    - Visible-only filtering
    - Early exit optimization

    ARCHITECTURE:
    =============

    Layer 1 (REFLEX) - <10ms:
        - Check metadata for SimHash fingerprint
        - Scan DOM for matching element
        - Instant hit if fingerprint matches

    Layer 2 (HEURISTIC) - ~50ms:
        - Get VISIBLE interactive elements ONLY (capped at MAX_CANDIDATES)
        - Use Levenshtein distance on text/aria-label
        - Return if score > 0.8

    Layer 3 (SEMANTIC) - ~200ms:
        - Query vector database with intent
        - Find semantically similar elements
        - Return if confidence > 0.7

    Layer 4 (COGNITIVE) - Slow:
        - Send page HTML to AI
        - Get selector recommendation
        - Last resort fallback

    SELF-HEALING:
    ==============

    If Layer 1 fails but Layer 2+ succeeds:
    1. Compute new SimHash for found element
    2. Return it in FindResult.new_signature
    3. Caller updates recipe metadata
    4. Next execution hits Layer 1 immediately
    """

    # AUDIT FIX: Performance limits
    MAX_CANDIDATES = 100  # Cap element scanning to prevent freeze
    MAX_IFRAME_DEPTH = 3  # Limit iframe recursion

    # Interactive element selectors - AUDIT FIX: Simplified for performance
    INTERACTIVE_SELECTORS = [
        "button:visible",
        "a:visible",
        "input:visible",
        "select:visible",
        "textarea:visible",
        "[role='button']:visible",
        "[role='link']:visible",
    ]

    # Shadow DOM piercing selectors (Playwright >>> combinator)
    SHADOW_DOM_SELECTORS = [
        ">>> button",
        ">>> a",
        ">>> input",
        ">>> [role='button']",
    ]

    # Minimum scores for each layer
    LAYER2_THRESHOLD = 0.8   # Heuristic match threshold
    LAYER3_THRESHOLD = 0.7   # Semantic match threshold
    SIMHASH_THRESHOLD = 0.85 # SimHash similarity threshold

    def __init__(
        self,
        page: Page,
        vector_db: MockVectorDB = None,
        ai_agent: MockAIAgent = None
    ):
        """
        Initialize SmartFinder.

        Args:
            page: Playwright Page instance
            vector_db: Vector database client (mock by default)
            ai_agent: AI agent client (mock by default)
        """
        self.page = page
        self.vector_db = vector_db or MockVectorDB()
        self.ai_agent = ai_agent or MockAIAgent()

        # Cache for element signatures (avoid recomputing)
        self._signature_cache: Dict[str, Dict] = {}

    async def find(
        self,
        intent: str,
        metadata: Optional[Dict] = None,
        container_selector: Optional[str] = None
    ) -> FindResult:
        """
        Find an element using the 4-layer fallback system.

        AUDIT FIX: Added container_selector for contextual scoping.

        Args:
            intent: Natural language description (e.g., "Login Button")
            metadata: Optional metadata with 'simhash' for Layer 1
            container_selector: Optional CSS selector to scope search (e.g., ".login-modal")

        Returns:
            FindResult with element, layer used, and healing info
        """
        metadata = metadata or {}
        start_time = time.time()

        # Parse container hint from intent (e.g., "Login Button in the header")
        container_hint = self._parse_container_hint(intent)
        if container_hint and not container_selector:
            container_selector = container_hint

        logger.info(f"[SmartFinder] Searching for: '{intent}'" +
                    (f" in container: {container_selector}" if container_selector else ""))

        # =====================================================================
        # LAYER 1: REFLEX (SimHash Matching) - <10ms
        # =====================================================================
        if metadata.get("simhash"):
            logger.debug("[Layer 1] REFLEX: Checking SimHash fingerprint...")
            layer1_start = time.time()

            try:
                result = await self._layer1_reflex(intent, metadata["simhash"], container_selector)
                if result.found:
                    result.duration_ms = int((time.time() - start_time) * 1000)
                    logger.info(
                        f"[Layer 1] ✅ REFLEX HIT in {time.time() - layer1_start:.3f}s "
                        f"(confidence: {result.confidence:.2f})"
                    )
                    return result
                else:
                    logger.info("[Layer 1] ❌ REFLEX MISS: SimHash not found, falling back...")
            except Exception as e:
                logger.warning(f"[Layer 1] ⚠️ REFLEX ERROR: {e}")
        else:
            logger.debug("[Layer 1] SKIPPED: No SimHash in metadata")

        # =====================================================================
        # LAYER 2: HEURISTIC (Levenshtein Matching) - ~50ms
        # =====================================================================
        logger.debug("[Layer 2] HEURISTIC: Scanning interactive elements...")
        layer2_start = time.time()

        try:
            result = await self._layer2_heuristic(intent, container_selector)
            if result.found:
                # Self-healing: Compute new signature for the found element
                result.new_signature = await self._compute_element_signature(result.element)
                result.duration_ms = int((time.time() - start_time) * 1000)
                logger.info(
                    f"[Layer 2] ✅ HEURISTIC HIT in {time.time() - layer2_start:.3f}s "
                    f"(score: {result.confidence:.2f}, checked: {result.candidates_checked})"
                )
                return result
            else:
                logger.info(
                    f"[Layer 2] ❌ HEURISTIC MISS: No match > {self.LAYER2_THRESHOLD} "
                    f"(checked: {result.candidates_checked})"
                )
        except Exception as e:
            logger.warning(f"[Layer 2] ⚠️ HEURISTIC ERROR: {e}")

        # =====================================================================
        # LAYER 3: SEMANTIC (Vector DB) - ~200ms
        # =====================================================================
        logger.debug("[Layer 3] SEMANTIC: Querying vector database...")
        layer3_start = time.time()

        try:
            result = await self._layer3_semantic(intent)
            if result.found:
                result.new_signature = await self._compute_element_signature(result.element)
                result.duration_ms = int((time.time() - start_time) * 1000)
                logger.info(
                    f"[Layer 3] ✅ SEMANTIC HIT in {time.time() - layer3_start:.3f}s "
                    f"(confidence: {result.confidence:.2f})"
                )
                return result
            else:
                logger.info("[Layer 3] ❌ SEMANTIC MISS: No vector match found")
        except Exception as e:
            logger.warning(f"[Layer 3] ⚠️ SEMANTIC ERROR: {e}")

        # =====================================================================
        # LAYER 4: COGNITIVE (AI Recovery) - Slow
        # =====================================================================
        logger.debug("[Layer 4] COGNITIVE: Invoking AI agent...")
        layer4_start = time.time()

        try:
            result = await self._layer4_cognitive(intent)
            if result.found:
                result.new_signature = await self._compute_element_signature(result.element)
                result.duration_ms = int((time.time() - start_time) * 1000)
                logger.info(
                    f"[Layer 4] ✅ COGNITIVE HIT in {time.time() - layer4_start:.3f}s"
                )
                return result
            else:
                logger.info("[Layer 4] ❌ COGNITIVE MISS: AI could not locate element")
        except Exception as e:
            logger.warning(f"[Layer 4] ⚠️ COGNITIVE ERROR: {e}")

        # =====================================================================
        # ALL LAYERS FAILED
        # =====================================================================
        total_duration = int((time.time() - start_time) * 1000)
        logger.error(f"[SmartFinder] ❌ ELEMENT NOT FOUND: '{intent}' (total: {total_duration}ms)")

        return FindResult(
            element=None,
            layer=FinderLayer.NONE,
            confidence=0.0,
            duration_ms=total_duration,
            error=f"Element not found: {intent}"
        )

    def _parse_container_hint(self, intent: str) -> Optional[str]:
        """
        Parse container hints from intent string.

        Examples:
            "Login button in the header" -> "header"
            "Submit in the modal" -> "[role='dialog'], .modal"
            "Edit button in row containing John" -> None (complex, handle separately)
        """
        intent_lower = intent.lower()

        # Common container patterns
        container_map = {
            " in the header": "header, [role='banner'], .header, #header",
            " in the footer": "footer, [role='contentinfo'], .footer, #footer",
            " in the sidebar": "aside, [role='complementary'], .sidebar, #sidebar",
            " in the modal": "[role='dialog'], .modal, [data-modal], dialog",
            " in the dialog": "[role='dialog'], dialog",
            " in the form": "form",
            " in the nav": "nav, [role='navigation']",
            " in the menu": "[role='menu'], .menu, nav",
        }

        for pattern, selector in container_map.items():
            if pattern in intent_lower:
                return selector

        return None

    # -------------------------------------------------------------------------
    # LAYER 1: REFLEX (SimHash)
    # -------------------------------------------------------------------------
    async def _layer1_reflex(
        self,
        intent: str,
        target_simhash: str,
        container_selector: Optional[str] = None
    ) -> FindResult:
        """
        Layer 1: Find element by SimHash fingerprint.

        This is the fastest path - if we've seen this element before
        and computed its fingerprint, we can find it instantly.
        """
        candidates = await self._get_interactive_elements(container_selector=container_selector)

        best_match: Optional[ElementCandidate] = None
        best_similarity = 0.0

        for candidate in candidates:
            # Compute SimHash for this element
            simhash = compute_simhash(
                tag=candidate.tag,
                text=candidate.text,
                classes=candidate.classes,
                attributes=candidate.attributes
            )
            candidate.simhash = simhash

            # Compare with target
            similarity = simhash_similarity(target_simhash, simhash)

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = candidate

        # Check if best match exceeds threshold
        if best_match and best_similarity >= self.SIMHASH_THRESHOLD:
            return FindResult(
                element=best_match.handle,
                layer=FinderLayer.REFLEX,
                confidence=best_similarity,
                candidates_checked=len(candidates)
            )

        return FindResult(
            layer=FinderLayer.REFLEX,
            candidates_checked=len(candidates)
        )

    # -------------------------------------------------------------------------
    # LAYER 2: HEURISTIC (Levenshtein)
    # -------------------------------------------------------------------------
    async def _layer2_heuristic(
        self,
        intent: str,
        container_selector: Optional[str] = None
    ) -> FindResult:
        """
        Layer 2: Find element by fuzzy text matching.

        Scans all interactive elements and compares their text/aria-label
        against the intent using Levenshtein distance.

        AUDIT FIX: Now accepts container_selector for contextual scoping.
        """
        candidates = await self._get_interactive_elements(container_selector=container_selector)

        best_match: Optional[ElementCandidate] = None
        best_score = 0.0

        # Normalize intent for comparison
        intent_normalized = normalize_text(intent).lower()

        for candidate in candidates:
            # Compare against inner text
            text_score = hybrid_similarity(intent_normalized, candidate.text.lower())

            # Compare against aria-label
            aria_label = candidate.attributes.get("aria-label", "")
            aria_score = hybrid_similarity(intent_normalized, aria_label.lower())

            # Compare against placeholder (for inputs)
            placeholder = candidate.attributes.get("placeholder", "")
            placeholder_score = hybrid_similarity(intent_normalized, placeholder.lower())

            # Compare against value (for buttons with value)
            value = candidate.attributes.get("value", "")
            value_score = hybrid_similarity(intent_normalized, value.lower())

            # Take best score across all attributes
            candidate.score = max(text_score, aria_score, placeholder_score, value_score)

            if candidate.score > best_score:
                best_score = candidate.score
                best_match = candidate

        # Check if best match exceeds threshold
        if best_match and best_score >= self.LAYER2_THRESHOLD:
            return FindResult(
                element=best_match.handle,
                layer=FinderLayer.HEURISTIC,
                confidence=best_score,
                candidates_checked=len(candidates)
            )

        return FindResult(
            layer=FinderLayer.HEURISTIC,
            candidates_checked=len(candidates),
            confidence=best_score
        )

    # -------------------------------------------------------------------------
    # LAYER 3: SEMANTIC (Vector DB)
    # -------------------------------------------------------------------------
    async def _layer3_semantic(self, intent: str) -> FindResult:
        """
        Layer 3: Find element using semantic vector search.

        Queries the vector database for semantically similar elements.
        """
        # Query vector DB
        match = await self.vector_db.search(intent)

        if not match:
            return FindResult(layer=FinderLayer.SEMANTIC)

        # Get selector from match
        selector = match.get("selector")
        if not selector:
            return FindResult(layer=FinderLayer.SEMANTIC)

        # Find element using selector
        try:
            element = await self.page.query_selector(selector)
            if element:
                return FindResult(
                    element=element,
                    layer=FinderLayer.SEMANTIC,
                    confidence=match.get("score", 0.7)
                )
        except Exception as e:
            logger.debug(f"[Layer 3] Selector failed: {e}")

        return FindResult(layer=FinderLayer.SEMANTIC)

    # -------------------------------------------------------------------------
    # LAYER 4: COGNITIVE (AI)
    # -------------------------------------------------------------------------
    async def _layer4_cognitive(self, intent: str) -> FindResult:
        """
        Layer 4: Use AI to recover the element.

        Sends page context to an AI agent that analyzes the DOM
        and returns a selector recommendation.
        """
        # Get page HTML (truncated for AI)
        try:
            page_html = await self.page.content()
            # Truncate to avoid token limits
            if len(page_html) > 50000:
                page_html = page_html[:50000] + "\n... (truncated)"
        except:
            page_html = ""

        # Call AI agent
        selector = await self.ai_agent.recover(intent, page_html)

        if not selector:
            return FindResult(layer=FinderLayer.COGNITIVE)

        # Try the AI-suggested selector
        try:
            element = await self.page.query_selector(selector)
            if element:
                return FindResult(
                    element=element,
                    layer=FinderLayer.COGNITIVE,
                    confidence=0.6  # AI confidence is lower
                )
        except Exception as e:
            logger.debug(f"[Layer 4] AI selector failed: {e}")

        return FindResult(layer=FinderLayer.COGNITIVE)

    # -------------------------------------------------------------------------
    # HELPER METHODS
    # -------------------------------------------------------------------------
    async def _get_interactive_elements(
        self,
        container_selector: Optional[str] = None,
        include_shadow_dom: bool = True,
        include_iframes: bool = True,
        iframe_depth: int = 0
    ) -> List[ElementCandidate]:
        """
        Get all interactive elements on the page.

        AUDIT FIXES:
        - MAX_CANDIDATES cap to prevent performance bomb
        - Shadow DOM piercing via >>> combinator
        - iFrame recursion (depth-limited)
        - Container scoping for contextual search
        - Early exit when limit reached
        - Position index tracking for disambiguation

        Args:
            container_selector: Optional CSS selector to scope search
            include_shadow_dom: Whether to pierce Shadow DOM
            include_iframes: Whether to search in iframes
            iframe_depth: Current iframe recursion depth

        Returns:
            List of ElementCandidate objects (max MAX_CANDIDATES)
        """
        candidates: List[ElementCandidate] = []
        position_index = 0

        # Determine base element (container or page)
        base = self.page
        if container_selector:
            try:
                container = await self.page.query_selector(container_selector)
                if container:
                    base = container
                    logger.debug(f"[Elements] Scoped to container: {container_selector}")
            except:
                pass  # Fall back to full page

        # Build selector - use simpler visibility checks for performance
        combined_selector = ", ".join([
            "button", "a", "input", "select", "textarea",
            "[role='button']", "[role='link']"
        ])

        try:
            # Query main page
            if hasattr(base, 'query_selector_all'):
                elements = await base.query_selector_all(combined_selector)
            else:
                elements = await self.page.query_selector_all(combined_selector)

            for element in elements:
                if len(candidates) >= self.MAX_CANDIDATES:
                    logger.debug(f"[Elements] Hit MAX_CANDIDATES limit ({self.MAX_CANDIDATES})")
                    break

                candidate = await self._extract_candidate(element, position_index)
                if candidate:
                    candidates.append(candidate)
                    position_index += 1

            # Shadow DOM piercing (if enabled and not at limit)
            if include_shadow_dom and len(candidates) < self.MAX_CANDIDATES:
                for shadow_selector in self.SHADOW_DOM_SELECTORS:
                    if len(candidates) >= self.MAX_CANDIDATES:
                        break
                    try:
                        shadow_elements = await self.page.query_selector_all(shadow_selector)
                        for element in shadow_elements:
                            if len(candidates) >= self.MAX_CANDIDATES:
                                break
                            candidate = await self._extract_candidate(element, position_index)
                            if candidate:
                                candidates.append(candidate)
                                position_index += 1
                    except Exception as e:
                        # Shadow DOM piercing may not be supported
                        logger.debug(f"[Shadow DOM] Piercing failed for {shadow_selector}: {e}")

            # iFrame recursion (if enabled and under depth limit)
            if include_iframes and iframe_depth < self.MAX_IFRAME_DEPTH and len(candidates) < self.MAX_CANDIDATES:
                for frame in self.page.frames:
                    if frame == self.page.main_frame:
                        continue  # Already processed main frame
                    if len(candidates) >= self.MAX_CANDIDATES:
                        break
                    try:
                        frame_elements = await frame.query_selector_all(combined_selector)
                        for element in frame_elements:
                            if len(candidates) >= self.MAX_CANDIDATES:
                                break
                            candidate = await self._extract_candidate(element, position_index)
                            if candidate:
                                candidates.append(candidate)
                                position_index += 1
                    except Exception as e:
                        logger.debug(f"[iFrame] Error scanning frame: {e}")

        except Exception as e:
            logger.warning(f"[SmartFinder] Error getting interactive elements: {e}")

        logger.debug(f"[Elements] Found {len(candidates)} candidates (limit: {self.MAX_CANDIDATES})")
        return candidates

    async def _extract_candidate(
        self,
        element: ElementHandle,
        position_index: int
    ) -> Optional[ElementCandidate]:
        """
        Extract metadata from a single element.

        Args:
            element: Element handle
            position_index: Position in DOM for disambiguation

        Returns:
            ElementCandidate or None if element is not visible/valid
        """
        try:
            # Check visibility first (fast reject)
            is_visible = await element.is_visible()
            if not is_visible:
                return None

            # Extract metadata
            tag = await element.evaluate("el => el.tagName.toLowerCase()")

            # Get text (avoid for input/select)
            text = ""
            if tag not in ["input", "select"]:
                try:
                    text = await element.inner_text()
                except:
                    pass

            # Get classes
            class_str = await element.get_attribute("class") or ""
            classes = class_str.split() if class_str else []

            # Get key attributes
            attributes = {"_position": str(position_index)}
            for attr in ["id", "name", "aria-label", "placeholder", "value", "data-testid", "type"]:
                try:
                    val = await element.get_attribute(attr)
                    if val:
                        attributes[attr] = val
                except:
                    pass

            return ElementCandidate(
                handle=element,
                tag=tag,
                text=text.strip()[:100],  # Limit text length
                classes=classes[:10],      # Limit classes
                attributes=attributes
            )

        except Exception:
            # Element might have been removed from DOM
            return None

    async def _compute_element_signature(
        self,
        element: ElementHandle
    ) -> Optional[Dict]:
        """
        Compute a full signature for an element.

        Used for self-healing - when we find an element, we compute
        its signature and store it for future fast lookups.
        """
        try:
            # Extract element data
            tag = await element.evaluate("el => el.tagName.toLowerCase()")
            text = await element.inner_text() if tag not in ["input", "select"] else ""

            class_str = await element.get_attribute("class") or ""
            classes = class_str.split() if class_str else []

            attributes = {}
            for attr in ["id", "name", "aria-label", "placeholder", "value", "data-testid"]:
                val = await element.get_attribute(attr)
                if val:
                    attributes[attr] = val

            # Compute signature
            simhash = compute_simhash(tag, text, classes, attributes)

            return {
                "simhash": simhash,
                "tag": tag,
                "text": normalize_text(text)[:50],
                "classes": classes[:5],
                "attributes": attributes
            }

        except Exception as e:
            logger.warning(f"[SmartFinder] Error computing signature: {e}")
            return None


# =============================================================================
# STATE MANAGER INTEGRATION (Mock)
# =============================================================================

class StateManagerMock:
    """
    Mock StateManager for self-healing updates.

    In production, this updates the recipe metadata in the database.
    """

    async def update_recipe_metadata(
        self,
        node_id: str,
        action_index: int,
        new_signature: Dict
    ) -> bool:
        """
        Update the recipe with new element signature for self-healing.

        Args:
            node_id: The node that contains the action
            action_index: Index of the action within the node
            new_signature: New element signature (simhash, etc.)

        Returns:
            True if update was successful
        """
        logger.info(
            f"[Self-Healing] Updating metadata for {node_id}[{action_index}] "
            f"with new simhash: {new_signature.get('simhash', 'N/A')[:16]}..."
        )

        # Mock: In production, this would:
        # 1. Load the recipe from database
        # 2. Find the action in the node
        # 3. Update its metadata.simhash
        # 4. Save back to database

        return True


# =============================================================================
# CONVENIENCE FUNCTION
# =============================================================================

async def find_element(
    page: Page,
    intent: str,
    metadata: Optional[Dict] = None
) -> FindResult:
    """
    Convenience function to find an element.

    Creates a SmartFinder instance and searches for the element.

    Args:
        page: Playwright Page
        intent: Natural language description
        metadata: Optional metadata with simhash

    Returns:
        FindResult
    """
    finder = SmartFinder(page)
    return await finder.find(intent, metadata)


# =============================================================================
# EXAMPLE / TESTING
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("SMART FINDER V2 - Architecture Overview")
    print("=" * 60)
    print("""
    LAYER 1 (REFLEX) - <10ms
    ├── Check metadata for SimHash fingerprint
    ├── Scan DOM for matching element
    └── Return immediately if found

    LAYER 2 (HEURISTIC) - ~50ms
    ├── Get all interactive elements
    ├── Compare text/aria-label with Levenshtein
    └── Return if score > 0.8

    LAYER 3 (SEMANTIC) - ~200ms
    ├── Query vector database
    ├── Find semantically similar elements
    └── Return if confidence > 0.7

    LAYER 4 (COGNITIVE) - Slow
    ├── Send page HTML to AI
    ├── Get selector recommendation
    └── Last resort fallback

    SELF-HEALING:
    └── If Layer 1 fails but Layer 2+ succeeds:
        ├── Compute new SimHash
        ├── Update recipe metadata
        └── Future lookups hit Layer 1
    """)
    print("=" * 60)
