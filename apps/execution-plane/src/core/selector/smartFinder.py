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
from typing import Any, Dict, List, Optional

import re
import json
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
from core.GlassBox import GlassBoxEngine

logger = logging.getLogger("smartFinderV2")


# =============================================================================
# ENUMS & DATA CLASSES
# =============================================================================

class FinderLayer(Enum):
    """Which layer found the element."""
    DOMAIN_MEMORY = -2 # Domain-specific history prior
    STRUCTURAL = 0  # Keyword→CSS deterministic lookup (<5ms)
    REFLEX = 1      # SimHash match (<10ms)
    HEURISTIC = 2   # Levenshtein match (~50ms)
    SEMANTIC = 3    # Vector DB match (~200ms)
    RECOVERY = 4    # Structured LLM recovery (from ladder/rescue)
    COGNITIVE = 5   # Legacy AI recovery (slow)
    NONE = -1       # Not found


@dataclass
class FindResult:
    """Result of element finding operation."""
    element: Optional[ElementHandle] = None
    # --- Phase 15 Selector Bundle Contract ---
    selector_id: str = ""
    locator_type: str = ""     # css | xpath | text | role
    locator_value: str = ""
    confidence: float = 0.0
    reason_codes: list[str] = field(default_factory=list)
    fingerprint: Optional[Dict] = None
    # --- Legacy Fields (Preserved for backwards compat during migration) ---
    layer: FinderLayer = FinderLayer.NONE
    duration_ms: int = 0
    new_signature: Optional[Dict] = None  # For self-healing
    candidates_checked: int = 0
    error: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.element is not None or self.locator_value != ""

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
    classes: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    score: float = 0.0
    simhash: str = ""


@dataclass
class CandidateRow:
    """One row in the Candidate Table — compact element representation."""
    handle: ElementHandle
    role: str = ""           # "button", "link", "input", "checkbox", "select"
    text: str = ""           # innerText (truncated to 80 chars)
    aria_name: str = ""      # aria-label or name attribute
    selector: str = ""       # unique CSS selector for this element
    visible: bool = True
    enabled: bool = True
    region: str = "main"     # "header", "sidebar", "main", "footer", "modal", "nav"
    tag: str = ""
    input_type: str = ""     # for inputs: "text", "email", "password", "checkbox"
    score: float = 0.0
    classes: list[str] = field(default_factory=list)
    # --- Phase 15 Proximity Fields ---
    center_x: float = 0.0
    center_y: float = 0.0
    dom_path: str = ""
    container_id: str = ""


# =============================================================================
# ACTION CONSTRAINTS — Hard-filter candidates by action type
# =============================================================================
ACTION_CONSTRAINTS: dict[str, dict] = {
    "type_text": {
        "tags": {"input", "textarea"},
        "roles": {"textbox", "searchbox", "combobox", "spinbutton"},
        "attrs": {"contenteditable"},
    },
    "find_and_click": {
        "tags": {"a", "button", "label", "summary", "div", "span", "li", "td"},
        "roles": {"button", "link", "tab", "menuitem", "checkbox", "switch",
                  "option", "treeitem", "radio", "menuitemcheckbox", "menuitemradio"},
    },
    "extract_text": {
        "tags": {"*"},  # any visible element
        "roles": {"*"},
    },
    "extract_table": {
        "tags": {"table", "tbody", "thead", "div"},
        "roles": {"table", "grid", "treegrid"},
    },
    "select_option": {
        "tags": {"select", "div", "ul"},
        "roles": {"listbox", "combobox", "menu"},
    },
    "check_element": {
        "tags": {"input", "div", "span", "button"},
        "roles": {"checkbox", "switch", "radio"},
        "input_types": {"checkbox", "radio"},
    },
    "hover_element": {
        "tags": {"a", "button", "div", "span", "li", "td", "img"},
        "roles": {"button", "link", "menuitem", "tooltip"},
    },
    "scroll_page": {
        "tags": {"*"},
        "roles": {"*"},
    },
}

# Region detection tag/role mapping
REGION_SELECTORS: dict[str, str] = {
    "header": "header, [role='banner']",
    "nav": "nav, [role='navigation']",
    "sidebar": "aside, [role='complementary'], .sidebar, #sidebar",
    "footer": "footer, [role='contentinfo']",
    "modal": "[role='dialog'], dialog, .modal, [data-modal]",
    "main": "main, [role='main'], #content, .content",
}


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
                from qdrant_client import AsyncQdrantClient
                from qdrant_client.http import models

                self._client = AsyncQdrantClient(
                    url=self.url,
                    api_key=self.api_key,
                    timeout=5.0  # Fast timeout for element finding
                )

                # Check if collection exists, create if not
                try:
                    collections = (await self._client.get_collections()).collections
                    exists = any(c.name == self.collection_name for c in collections)

                    if not exists:
                        logger.info(f"[VectorDB] Creating collection '{self.collection_name}'")
                        await self._client.create_collection(
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

            # Search Qdrant (v2 API: query_points replaces search)
            response = await self._client.query_points(
                collection_name=self.collection_name,
                query=embedding,
                limit=1,
                score_threshold=0.7
            )

            if response.points:
                match = response.points[0]
                payload = match.payload or {}
                return {
                    "selector": payload.get("selector"),
                    "score": match.score,
                    "intent": payload.get("intent"),
                    "attributes": payload.get("attributes", {})
                }

        except Exception as e:
            logger.warning(f"[VectorDB] Search failed: {e}")

        return None

    async def _get_embedding(self, text: str) -> Optional[list[float]]:
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

            await self._client.upsert(
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
        self.model = model or os.getenv("LLM_MODEL", "meta/llama-3.3-70b-instruct")
        self.api_key = api_key or os.getenv("NVIDIA_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
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
        axtree_map: str = ""
    ) -> Optional[str]:
        """
        Use AI to identify a Node_ID from an AXTree map.
        """
        await self._ensure_client()

        if not self._initialized:
            logger.debug("[AIAgent] Not initialized, returning None")
            return None

        try:
            from core.rag.prompts import SELECTOR_RECOVERY_SYSTEM_PROMPT, SELECTOR_RECOVERY_USER_PROMPT

            # Call LLM
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": SELECTOR_RECOVERY_SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": SELECTOR_RECOVERY_USER_PROMPT.format(
                            intent=intent,
                            axtree_map=axtree_map
                        )
                    }
                ],
                max_tokens=20,
                temperature=0
            )

            node_id_str = response.choices[0].message.content.strip()
            # Extract just the number if AI adds fluff
            match = re.search(r"(\d+)", node_id_str)
            return match.group(1) if match else None

        except Exception as e:
            logger.warning(f"[AIAgent] Recovery failed: {e}")
            return None



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
    MAX_CANDIDATES = 1000  # Cap element scanning to prevent freeze
    MAX_IFRAME_DEPTH = 3  # Limit iframe recursion

    # Relevant element selectors
    RELEVANT_SELECTORS = [
        # Interactive
        "button:visible", "a:visible", "input:visible", "select:visible", "textarea:visible",
        "[role='button']:visible", "[role='link']:visible", "[onclick]:visible",
        "div[class*='btn']:visible", "div[class*='button']:visible",
        "span[class*='btn']:visible", "span[class*='button']:visible",

        # Semantic / Content
        "h1:visible", "h2:visible", "h3:visible", "h4:visible", "h5:visible", "h6:visible",
        "p:visible", "article:visible", "section:visible", "label:visible",
        "span:visible",  # Common for prop values on doc sites

        # Data / Structure
        "table:visible", "tr:visible", "td:visible", "th:visible",
        "ul:visible", "ol:visible", "li:visible",
        "dl:visible", "dt:visible", "dd:visible",  # Definition lists (common in prop docs)
        "[data-testid]:visible", "[data-cy]:visible", "[data-qa]:visible",

        # Code / Documentation
        "code:visible",   # Inline code (prop values, defaults)
        "pre:visible",    # Code blocks
        "kbd:visible",    # Keyboard/key values

        # Visual
        "img:visible", "svg:visible",
    ]

    # Shadow DOM piercing selectors (Playwright >>> combinator)
    SHADOW_DOM_SELECTORS = [
        ">>> button",
        ">>> a",
        ">>> input",
        ">>> [role='button']",
    ]

    # Minimum scores for each layer
    # FIX RC2: Lowered from 0.8 to 0.55.
    # 0.8 required near-perfect text equality, rejecting synonym pairs that
    # score 0.55–0.79 (e.g. "login" → "Sign in" ≈ 0.62 with fixed weights).
    # 0.55 is above random noise (unrelated strings score 0.1–0.3) while
    # allowing confident synonym matches through.
    LAYER2_THRESHOLD = 0.55   # Heuristic match threshold
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
        self.glass = GlassBoxEngine()

        # Heuristic Intent Map (Synonyms)
        self.INTENT_SYNONYMS = {
            "buy": ["add to cart", "checkout", "purchase", "order", "subscribe"],
            "login": ["sign in", "log in", "next", "continue", "submit", "auth"],
            "register": ["sign up", "join", "create account", "start"],
            "search": ["find", "query", "lookup"],
            "delete": ["remove", "trash", "cancel", "clear"],
            "edit": ["change", "update", "modify"],
            "save": ["submit", "apply", "confirm", "done"],
        }

        # =======================================================================
        # LAYER 0: STRUCTURAL CSS MAP
        # =======================================================================
        # Keyword → ordered list of CSS selectors to try.
        # Playwright selectors support case-insensitive attribute matching (:i flag).
        # These are EXACT DOM lookups — pure logic, zero AI, zero fuzzy math.
        # =======================================================================
        self.STRUCTURAL_SELECTORS: dict[str, list[str]] = {
            # ── Search ───────────────────────────────────────────────
            "search": [
                "input[type='search']", "[role='searchbox']",
                "input[placeholder*='search' i]", "input[aria-label*='search' i]",
                "input[name*='search' i]", "input[name='q']", "input[name='query']",
                "[data-testid*='search' i]",
            ],
            # ── Auth ─────────────────────────────────────────────────
            "password": ["input[type='password']", "input[name*='password' i]", "input[placeholder*='password' i]"],
            "email": ["input[type='email']", "input[name*='email' i]", "input[placeholder*='email' i]", "input[autocomplete='email']"],
            "username": ["input[name='username']", "input[name='user']", "input[name='login']", "input[placeholder*='username' i]"],
            "submit": ["[type='submit']", "button[type='submit']"],
            "login": ["button[type='submit']", "[type='submit']", "button:has-text('Sign in')", "button:has-text('Log in')", "a:has-text('Sign in')"],
            "sign in": ["button:has-text('Sign in')", "a:has-text('Sign in')", "button:has-text('Sign In')", "[type='submit']"],
            "sign up": ["a:has-text('Sign up')", "button:has-text('Sign up')", "a:has-text('Register')", "button:has-text('Create account')"],

            # ── Navigation ───────────────────────────────────────────
            "home": ["a[href='/']", "a[href='#home']", "[aria-label*='home' i]", "a:has-text('Home')"],
            "back": ["[aria-label*='back' i]", "button:has-text('Back')", "a:has-text('Back')"],
            "next": ["button:has-text('Next')", "a:has-text('Next')", "[aria-label*='next' i]", "a[rel='next']"],
            "previous": ["button:has-text('Prev')", "button:has-text('Previous')", "[aria-label*='prev' i]", "a[rel='prev']"],
            "navbar": ["nav a", "[role='navigation'] a", ".nav-link", "header a"],

            # ── Dialogs ──────────────────────────────────────────────
            "accept": ["button:has-text('Accept')", "button:has-text('Allow')", "button:has-text('OK')", "button:has-text('Agree')"],
            "close": ["[aria-label*='close' i]", "button:has-text('Close')", "[data-testid*='close' i]", ".close"],
            "cancel": ["button:has-text('Cancel')", "[aria-label*='cancel' i]", "button:has-text('Dismiss')"],
            "confirm": ["button:has-text('Confirm')", "button:has-text('Yes')", "button:has-text('Proceed')"],

            # ── Commerce ─────────────────────────────────────────────
            "cart": ["[aria-label*='cart' i]", "a[href*='cart']", "[data-testid*='cart' i]"],
            "checkout": ["button:has-text('Checkout')", "a:has-text('Checkout')", "a[href*='checkout']"],
            "add to cart": ["button:has-text('Add to cart')", "button:has-text('Add to bag')", "[data-testid*='add-to-cart' i]"],
            "buy": ["button:has-text('Buy')", "button:has-text('Purchase')", "button:has-text('Order')"],

            # ── Filter / Sidebar ─────────────────────────────────────
            "filter": [
                "aside a", "[role='complementary'] a", "nav.filter a",
                "[data-testid*='filter' i]", ".sidebar a", ".filter a",
                "[aria-label*='filter' i]", "button:has-text('Filter')",
            ],
            "sidebar": [
                "aside", "[role='complementary']", ".sidebar", "#sidebar",
                "nav[aria-label*='filter' i]", "nav.sidebar",
            ],

            # ── Language filters (common on GitHub, npm, SO) ─────────
            "python": ["a:has-text('Python')", "label:has-text('Python')", "input[value*='python' i]", "[data-value='python' i]"],
            "javascript": ["a:has-text('JavaScript')", "label:has-text('JavaScript')", "input[value*='javascript' i]"],
            "typescript": ["a:has-text('TypeScript')", "label:has-text('TypeScript')", "input[value*='typescript' i]"],
            "java": ["a:has-text('Java')", "label:has-text('Java')", "input[value*='java' i]"],
            "go": ["a:has-text('Go')", "label:has-text('Go')", "input[value*='go' i]"],
            "rust": ["a:has-text('Rust')", "label:has-text('Rust')", "input[value*='rust' i]"],
            "c++": ["a:has-text('C++')", "label:has-text('C++')", "input[value*='cpp' i]"],

            # ── Tabs ─────────────────────────────────────────────────
            "tab": ["[role='tab']", "[role='tablist'] button", ".tab", "[data-tab]", "button[aria-selected]"],

            # ── Dropdown / Select ────────────────────────────────────
            "dropdown": ["select", "[role='listbox']", "[role='combobox']", "[data-dropdown]", ".dropdown"],
            "select": ["select", "[role='listbox']", "[role='combobox']"],
            "menu": ["[role='menu']", "[role='menubar']", "nav", ".menu"],

            # ── Checkboxes / Toggles ─────────────────────────────────
            "checkbox": ["input[type='checkbox']", "[role='checkbox']"],
            "toggle": ["[role='switch']", ".toggle", "input[type='checkbox']"],
            "radio": ["input[type='radio']", "[role='radio']"],

            # ── Pagination ───────────────────────────────────────────
            "pagination": ["[aria-label*='pagination']", "nav.pagination", ".pagination", "a[rel='next']"],
            "page": ["[aria-label*='pagination'] a", "nav.pagination a", ".pagination a"],
            "load more": ["button:has-text('Load more')", "button:has-text('Show more')", "a:has-text('Load more')"],

            # ── Sort ─────────────────────────────────────────────────
            "sort": ["[aria-label*='sort' i]", "button:has-text('Sort')", "select[name*='sort']", "[data-testid*='sort' i]"],

            # ── Date / Calendar ──────────────────────────────────────
            "date": ["input[type='date']", "input[type='datetime-local']", "[role='grid']", ".calendar", "[data-testid*='date']"],

            # ── File Upload ──────────────────────────────────────────
            "upload": ["input[type='file']", "[role='button']:has-text('Upload')", ".dropzone", "button:has-text('Upload')"],
            "file": ["input[type='file']", "button:has-text('Choose file')", "button:has-text('Browse')"],

            # ── Result / List Items ──────────────────────────────────
            "first result": ["li:first-of-type a", "article:first-of-type a", "[data-testid*='result']:first-of-type a"],
            "first": ["li:first-of-type a", "article:first-of-type a", "tr:first-of-type a"],
            "result": ["article a", "li a", "[data-testid*='result'] a", ".search-result a"],

            # ── About / Description ──────────────────────────────────
            "about": [".about", "[itemprop='description']", "p.description", "h2:has-text('About')", ".BorderGrid-cell p"],
            "description": ["[itemprop='description']", "meta[name='description']", "p.description", ".repo-description"],

            # ── Content ──────────────────────────────────────────────
            "star": ["[aria-label*='star' i]", "button:has-text('Star')", "[data-testid*='star' i]"],
            "comment": ["textarea[name*='comment' i]", "textarea[placeholder*='comment' i]", "textarea"],
            "message": ["textarea[name*='message' i]", "textarea[placeholder*='message' i]", "textarea"],

            # ── Form Elements ────────────────────────────────────────
            "phone": ["input[type='tel']", "input[name*='phone' i]", "input[placeholder*='phone' i]"],
            "name": ["input[name*='name' i]", "input[placeholder*='name' i]", "input[autocomplete='name']"],
            "address": ["input[name*='address' i]", "input[placeholder*='address' i]", "textarea[name*='address' i]"],
        }

        # Cache for element signatures (avoid recomputing)
        self._signature_cache: dict[str, Dict] = {}

    async def find(
        self,
        intent: str,
        metadata: Optional[Dict] = None,
        container_selector: Optional[str] = None,
        scan_mode: str = "auto",  # "interactive", "all", or "auto"
        timeout: int = 3000,      # NEW: Timeout in ms
        interval: int = 500,      # NEW: Polling interval in ms
        position: Optional[int] = None,  # NEW: Index for list items (0=first, 1=second)
        discovery_mode: bool = False,  # NEW: Lowers Layer 2 threshold for first-time generation
        action_type: str = "interactive"
    ) -> FindResult:
        """
        Find an element using the 4-layer fallback system with Smart Wait.

        Args:
            intent: Natural language description (e.g., "Login Button")
            metadata: Optional metadata with 'simhash' for Layer 1
            container_selector: Optional CSS selector to scope search
            scan_mode: "interactive" (faster) or "all" (deeper)
            timeout: Max time to wait for element (ms)
            interval: Time between retries (ms)

        Returns:
            FindResult with element, layer used, and healing info
        """
        metadata = metadata or {}
        start_time = time.time()

        # AUDIT FIX: Adaptive Scan Mode — includes doc/prop site keywords
        if scan_mode == "auto":
            extraction_keywords = [
                "read", "get", "verify", "extract", "check", "text", "price", "status",
                "value", "default", "prop", "color", "parameter", "option", "setting",
                "find", "what", "which",
            ]
            if any(k in intent.lower() for k in extraction_keywords):
                scan_mode = "all"
            else:
                scan_mode = "interactive"

        # Parse container hint from intent (e.g., "Login Button in the header")
        container_hint = self._parse_container_hint(intent)
        if container_hint and not container_selector:
            container_selector = container_hint

        logger.info(f"[SmartFinder] Searching for: '{intent}' (mode: {scan_mode}, timeout: {timeout}ms)" +
                    (f" in container: {container_selector}" if container_selector else ""))

        # =====================================================================
        # LAYER 0: STRUCTURAL (Deterministic CSS) - <5ms
        # =====================================================================
        # Pure keyword → CSS lookup. No fuzzy math, no AI, no ML.
        # Handles ~80% of common interactive elements instantly.
        # =====================================================================
        logger.debug("[Layer 0] STRUCTURAL: Keyword → CSS lookup...")
        layer0_start = time.time()
        try:
            result = await self._layer0_structural(intent)
            if result.found:
                result.duration_ms = int((time.time() - start_time) * 1000)
                logger.info(
                    f"[Layer 0] ✅ STRUCTURAL HIT in {time.time() - layer0_start:.3f}s "
                    f"(confidence: {result.confidence:.2f})"
                )
                # LEARN from success
                try:
                    selector = await result.element.evaluate("el => { \
                        if (el.id) return '#' + el.id; \
                        if (el.name) return '[name=\"' + el.name + '\"]'; \
                        return null; \
                    }")
                    if selector:
                        memory.learn(self.page.url, intent, {
                            "selector": selector,
                            "layer": result.layer.name,
                            "timestamp": time.time()
                        })
                except Exception:
                    pass
                return result
            else:
                logger.debug("[Layer 0] No structural match, falling through to deterministic scorer.")
        except Exception as e:
            logger.warning(f"[Layer 0] ⚠️ STRUCTURAL ERROR: {e}")

        # =====================================================================
        # LAYER 0.5: DETERMINISTIC SCORER (Candidate Table) - ~30ms
        # =====================================================================
        # Build top-20 candidate table filtered by action type,
        # score using exact text match + containment. No AI.
        # =====================================================================
        action_type = self._infer_action_type(intent, scan_mode)
        logger.debug(f"[Layer 0.5] DETERMINISTIC: Candidate Table (action={action_type})...")
        try:
            det_result = await self.find_deterministic(
                intent=intent,
                action_type=action_type,
                container_selector=container_selector,
                position=position,
            )
            if det_result.found:
                det_result.new_signature = await self._compute_element_signature(det_result.element)
                det_result.duration_ms = int((time.time() - start_time) * 1000)
                logger.info(
                    f"[Layer 0.5] ✅ DETERMINISTIC HIT in {det_result.duration_ms}ms "
                    f"(confidence: {det_result.confidence:.2f})"
                )
                # LEARN from success
                try:
                    selector = await det_result.element.evaluate("el => { \
                        if (el.id) return '#' + el.id; \
                        if (el.name) return '[name=\"' + el.name + '\"]'; \
                        return null; \
                    }")
                    if selector:
                        memory.learn(self.page.url, intent, {
                            "selector": selector,
                            "layer": det_result.layer.name,
                            "timestamp": time.time()
                        })
                except Exception:
                    pass
                return det_result
            else:
                logger.debug("[Layer 0.5] Deterministic miss, falling through to legacy layers.")
        except Exception as e:
            logger.warning(f"[Layer 0.5] ⚠️ DETERMINISTIC ERROR: {e}")

        # SMART WAIT + LAZY-SCROLL LOOP
        iteration = 0
        scroll_pass = 0
        MAX_SCROLL_PASSES = 3  # Scroll down at most 3 times to reveal lazy content
        last_scroll_y = 0

        while (time.time() - start_time) * 1000 < timeout:
            iteration += 1
            if iteration > 1:
                logger.debug(f"[SmartFinder] ⏳ Retry #{iteration} for '{intent}'...")

            # =====================================================================
            # LAYER 1: REFLEX (SimHash Matching) - <10ms
            # =====================================================================
            if metadata.get("simhash"):
                logger.debug("[Layer 1] REFLEX: Checking SimHash fingerprint...")
                layer1_start = time.time()

                try:
                    result = await self._layer1_reflex(intent, metadata["simhash"], container_selector, scan_mode=scan_mode)
                    if result.found:
                        result.duration_ms = int((time.time() - start_time) * 1000)
                        logger.info(
                            f"[Layer 1] ✅ REFLEX HIT in {time.time() - layer1_start:.3f}s "
                            f"(confidence: {result.confidence:.2f})"
                        )
                        # LEARN from success
                        try:
                            selector = await result.element.evaluate("el => { \
                                if (el.id) return '#' + el.id; \
                                if (el.name) return '[name=\"' + el.name + '\"]'; \
                                return null; \
                            }")
                            if selector:
                                memory.learn(self.page.url, intent, {
                                    "selector": selector,
                                    "layer": result.layer.name,
                                    "timestamp": time.time()
                                })
                        except Exception:
                            pass
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
                result = await self._layer2_heuristic(intent, container_selector, scan_mode=scan_mode, position=position, discovery_mode=discovery_mode)
                if result.found:
                    result.new_signature = await self._compute_element_signature(result.element)

                    # LAYER 3 POPULATE: Learn from Layer 2
                    if result.new_signature and "selector" in result.new_signature:
                        asyncio.create_task(self.vector_db.store(
                            intent=intent,
                            selector=result.new_signature.get("selector", "unknown"),
                            attributes=result.new_signature.get("attributes", {})
                        ))

                    result.duration_ms = int((time.time() - start_time) * 1000)
                    logger.info(
                        f"[Layer 2] ✅ HEURISTIC HIT in {time.time() - layer2_start:.3f}s "
                        f"(score: {result.confidence:.2f}, checked: {result.candidates_checked})"
                    )
                    # LEARN from success
                    try:
                        selector = await result.element.evaluate("el => { \
                            if (el.id) return '#' + el.id; \
                            if (el.name) return '[name=\"' + el.name + '\"]'; \
                            return null; \
                        }")
                        if selector:
                            memory.learn(self.page.url, intent, {
                                "selector": selector,
                                "layer": result.layer.name,
                                "timestamp": time.time()
                            })
                    except Exception:
                        pass
                    return result
                else:
                    logger.info(
                        f"[Layer 2] ❌ HEURISTIC MISS: No match > {result.confidence:.2f} threshold "
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
                result = await self._layer3_semantic(intent, container_selector, scan_mode=scan_mode)
                if result.found:
                    result.new_signature = await self._compute_element_signature(result.element)
                    result.duration_ms = int((time.time() - start_time) * 1000)
                    logger.info(
                        f"[Layer 3] ✅ SEMANTIC HIT in {time.time() - layer3_start:.3f}s "
                        f"(confidence: {result.confidence:.2f})"
                    )
                    # LEARN from success
                    try:
                        selector = await result.element.evaluate("el => { \
                            if (el.id) return '#' + el.id; \
                            if (el.name) return '[name=\"' + el.name + '\"]'; \
                            return null; \
                        }")
                        if selector:
                            memory.learn(self.page.url, intent, {
                                "selector": selector,
                                "layer": result.layer.name,
                                "timestamp": time.time()
                            })
                    except Exception:
                        pass
                    return result
                else:
                    logger.info("[Layer 3] ❌ SEMANTIC MISS: No vector match found")
            except Exception as e:
                logger.warning(f"[Layer 3] ⚠️ SEMANTIC ERROR: {e}")

            # =========================================================================
            # LAZY-SCROLL FALLBACK: Element may be below viewport (accordion/infinite list)
            # After each full pass failure, scroll down and re-scan.
            # =========================================================================
            elapsed = (time.time() - start_time) * 1000
            remaining = timeout - elapsed

            if scroll_pass < MAX_SCROLL_PASSES and remaining > (interval * 2):
                try:
                    page_height = await self.page.evaluate("() => document.body.scrollHeight")
                    scroll_target = min(last_scroll_y + 600, page_height)
                    if scroll_target > last_scroll_y:
                        await self.page.evaluate(f"window.scrollTo(0, {scroll_target})")
                        last_scroll_y = scroll_target
                        scroll_pass += 1
                        logger.debug(f"[SmartFinder] 📜 Lazy-scroll pass {scroll_pass}: scrolled to y={scroll_target}")
                        await asyncio.sleep(0.5)  # Wait for lazy-loaded elements
                        continue  # Re-scan immediately without sleeping
                except Exception as e:
                    logger.debug(f"[SmartFinder] Scroll failed: {e}")

            if remaining > 100:
                sleep_time = min(interval, remaining) / 1000
                await asyncio.sleep(sleep_time)
            else:
                break

        # =====================================================================
        # RECOVERY LADDER (replaces old Layer 4 full-AXTree)
        # Step 1: Expanded candidate pool (top 40)
        # Step 2: Re-scan DOM after 500ms (lazy/dynamic content)
        # Step 3: LLM rescue with compact top-20 table (~200 tokens)
        # =====================================================================
        logger.info(f"[SmartFinder] ⚠️ Timeout ({timeout}ms) reached. Invoking recovery ladder...")

        try:
            recovery_result = await self._recovery_ladder(
                intent=intent,
                action_type=action_type if 'action_type' in dir() else self._infer_action_type(intent, scan_mode),
                container_selector=container_selector,
            )
            if recovery_result.found:
                recovery_result.duration_ms = int((time.time() - start_time) * 1000)
                # LEARN from success
                try:
                    selector = await recovery_result.element.evaluate("el => { \
                        if (el.id) return '#' + el.id; \
                        if (el.name) return '[name=\"' + el.name + '\"]'; \
                        return null; \
                    }")
                    if selector:
                        memory.learn(self.page.url, intent, {
                            "selector": selector,
                            "layer": recovery_result.layer.name,
                            "timestamp": time.time()
                        })
                except Exception:
                    pass
                return recovery_result
        except Exception as e:
            logger.warning(f"[Recovery] ⚠️ Recovery ladder error: {e}")

        # =====================================================================
        # ALL LAYERS + RECOVERY FAILED
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

    # -------------------------------------------------------------------------
    # LAYER 0: STRUCTURAL (Deterministic CSS)
    # -------------------------------------------------------------------------
    async def _layer0_structural(self, intent: str) -> FindResult:
        """
        Layer 0: Find element via deterministic keyword→CSS lookup.

        This is the FASTEST layer. No fuzzy matching, no AI, no ML.
        Simply maps known intent keywords to precise CSS attribute selectors
        and tries them in order. Covers ~80% of common interactive actions.

        Examples:
            "Search Input" → tries input[type='search'], input[placeholder*='search' i], ...
            "Password Field" → tries input[type='password']
            "Sign In Button" → tries button[type='submit'], button:has-text('Sign in'), ...
        """
        intent_lower = intent.lower()

        # Tokenize intent into keywords (skip short filler words)
        stop_words = {"the", "a", "an", "of", "on", "in", "at", "to", "for", "and", "or"}
        intent_words = [w for w in intent_lower.split() if len(w) > 2 and w not in stop_words]

        # Collect matching selectors from the map
        selectors_to_try: list[str] = []
        for keyword, css_list in self.STRUCTURAL_SELECTORS.items():
            keyword_words = keyword.split()
            # Match if ALL keyword words are present in the intent
            if all(kw in intent_lower for kw in keyword_words):
                selectors_to_try.extend(css_list)
                logger.debug(f"[Layer 0] Keyword match: '{keyword}' → {len(css_list)} selectors")

        if not selectors_to_try:
            return FindResult(layer=FinderLayer.STRUCTURAL)

        # Try each selector in order — first visible match wins
        for selector in selectors_to_try:
            try:
                element = await self.page.query_selector(selector)
                if element:
                    is_visible = await element.is_visible()
                    if is_visible:
                        logger.info(f"[Layer 0] ✅ Match: `{selector}`")
                        return FindResult(
                            element=element,
                            layer=FinderLayer.STRUCTURAL,
                            confidence=0.95,  # High confidence — this is deterministic
                            candidates_checked=len(selectors_to_try)
                        )
            except Exception as e:
                logger.debug(f"[Layer 0] Selector `{selector}` failed: {e}")

        return FindResult(layer=FinderLayer.STRUCTURAL, candidates_checked=len(selectors_to_try))

    # -------------------------------------------------------------------------
    # DOM-AWARE EXECUTION: Candidate Table + Deterministic Scorer
    # -------------------------------------------------------------------------

    async def _build_candidate_table(
        self,
        action_type: str = "find_and_click",
        container_selector: Optional[str] = None,
        max_candidates: int = 20
    ) -> list[CandidateRow]:
        """
        Build a compact table of the top interactable elements
        relevant to the given action type from the LIVE page.

        This replaces full DOM scanning. Only elements that match
        the action constraints are included. Region detection
        gives context (header/sidebar/main/footer/modal).
        """
        constraints = ACTION_CONSTRAINTS.get(action_type, ACTION_CONSTRAINTS["find_and_click"])
        allowed_tags = constraints.get("tags", {"*"})
        allowed_roles = constraints.get("roles", {"*"})
        allowed_input_types = constraints.get("input_types", set())
        allowed_attrs = constraints.get("attrs", set())

        # Build CSS selector list based on constraints
        css_parts: list[str] = []
        if "*" not in allowed_tags:
            for tag in allowed_tags:
                css_parts.append(f"{tag}:visible")
        if "*" not in allowed_roles:
            for role in allowed_roles:
                css_parts.append(f"[role='{role}']:visible")
        if allowed_attrs:
            for attr in allowed_attrs:
                css_parts.append(f"[{attr}]:visible")

        # Fallback: if wildcard, use broad interactive selectors
        if "*" in allowed_tags:
            css_parts = [
                "a:visible", "button:visible", "input:visible", "select:visible",
                "textarea:visible", "[role]:visible", "label:visible",
                "h1:visible", "h2:visible", "h3:visible", "h4:visible",
                "p:visible", "span:visible", "li:visible", "td:visible",
            ]

        # Scope to container if provided
        scope = self.page
        if container_selector:
            try:
                container = await self.page.query_selector(container_selector)
                if container:
                    scope = container
            except Exception:
                pass

        # Collect unique elements
        seen_handles: set = set()
        candidates: list[CandidateRow] = []

        for css in css_parts:
            if len(candidates) >= max_candidates:
                break
            try:
                if hasattr(scope, 'query_selector_all'):
                    elements = await scope.query_selector_all(css)
                else:
                    elements = await self.page.query_selector_all(css)
            except Exception:
                continue

            for el in elements:
                if len(candidates) >= max_candidates:
                    break

                # Deduplicate by handle identity
                el_id = id(el)
                if el_id in seen_handles:
                    continue
                seen_handles.add(el_id)

                try:
                    # Extract properties
                    props = await self.page.evaluate("""(el) => {
                        const rect = el.getBoundingClientRect();

                        // Simple deterministic DOM path
                        let path = '';
                        let temp = el;
                        while(temp && temp.nodeType === 1) {
                            path = temp.tagName + '/' + path;
                            temp = temp.parentNode;
                        }

                        // Find closest semantic container
                        let containerId = '';
                        let p = el.parentElement;
                        while(p) {
                            if (p.tagName === 'FORM' || p.tagName === 'DIALOG' || p.getAttribute('role') === 'dialog' || p.tagName === 'NAV') {
                                containerId = p.tagName + (p.id ? '#' + p.id : '');
                                break;
                            }
                            p = p.parentElement;
                        }

                        return {
                            tag: el.tagName.toLowerCase(),
                            text: (el.innerText || el.textContent || '').trim().substring(0, 80),
                            role: el.getAttribute('role') || '',
                            aria: el.getAttribute('aria-label') || el.getAttribute('name') || el.getAttribute('placeholder') || '',
                            type: el.getAttribute('type') || '',
                            disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                            classes: Array.from(el.classList).slice(0, 5),
                            visible: rect.width > 0 && rect.height > 0,
                            center_x: rect.x + (rect.width / 2),
                            center_y: rect.y + (rect.height / 2),
                            dom_path: path,
                            container_id: containerId
                        };
                    }""", el)

                    if not props.get("visible", False):
                        continue

                    # Apply input_type filter for check_element actions
                    if allowed_input_types and props.get("type", "") not in allowed_input_types:
                        if props.get("tag") == "input" and not props.get("role"):
                            continue

                    region = await self._detect_region(el)

                    candidates.append(CandidateRow(
                        handle=el,
                        role=props.get("role") or self._infer_role(props.get("tag", ""), props.get("type", "")),
                        text=props.get("text", ""),
                        aria_name=props.get("aria", ""),
                        selector="",  # Computed lazily if needed
                        visible=True,
                        enabled=not props.get("disabled", False),
                        region=region,
                        tag=props.get("tag", ""),
                        input_type=props.get("type", ""),
                        classes=props.get("classes", []),
                        center_x=props.get("center_x", 0.0),
                        center_y=props.get("center_y", 0.0),
                        dom_path=props.get("dom_path", ""),
                        container_id=props.get("container_id", "")
                    ))
                except Exception:
                    continue

        logger.debug(f"[CandidateTable] Built {len(candidates)} candidates for action={action_type}")
        return candidates

    def _infer_role(self, tag: str, input_type: str) -> str:
        """Infer ARIA role from tag name and input type."""
        role_map = {
            "a": "link", "button": "button", "input": "textbox",
            "select": "combobox", "textarea": "textbox", "label": "label",
            "h1": "heading", "h2": "heading", "h3": "heading", "h4": "heading",
            "p": "text", "span": "text", "li": "listitem", "td": "cell",
            "img": "img", "table": "table",
        }
        if tag == "input":
            type_role_map = {
                "checkbox": "checkbox", "radio": "radio", "submit": "button",
                "search": "searchbox", "email": "textbox", "password": "textbox",
                "file": "button", "button": "button",
            }
            return type_role_map.get(input_type, "textbox")
        return role_map.get(tag, "generic")

    async def _detect_region(self, element: ElementHandle) -> str:
        """Detect which page region (header/sidebar/main/footer/modal/nav) an element belongs to."""
        try:
            region = await self.page.evaluate("""(el) => {
                let node = el;
                while (node && node !== document.body) {
                    const tag = node.tagName ? node.tagName.toLowerCase() : '';
                    const role = node.getAttribute ? (node.getAttribute('role') || '') : '';
                    const cls = node.className || '';

                    if (tag === 'header' || role === 'banner') return 'header';
                    if (tag === 'footer' || role === 'contentinfo') return 'footer';
                    if (tag === 'aside' || role === 'complementary' ||
                        cls.includes('sidebar') || cls.includes('Sidebar')) return 'sidebar';
                    if (tag === 'nav' || role === 'navigation') return 'nav';
                    if (role === 'dialog' || tag === 'dialog' ||
                        cls.includes('modal') || cls.includes('Modal')) return 'modal';
                    if (tag === 'main' || role === 'main') return 'main';
                    node = node.parentElement;
                }
                return 'main';
            }""", element)
            return region
        except Exception:
            return "main"

    def _score_candidate_deterministic(
        self,
        candidate: CandidateRow,
        anchors: list[str]
    ) -> float:
        """
        Score a candidate against intent anchors using ONLY deterministic
        string operations. No Levenshtein. No N-grams. No embeddings.

        Signal 1: EXACT TEXT MATCH → 1.0
          candidate.text.lower() == anchor.lower()

        Signal 2: CONTAINMENT → 0.85
          anchor.lower() in candidate.text.lower() and len(text) < 80

        Signal 3: ARIA/NAME MATCH → 0.8
          anchor.lower() in candidate.aria_name.lower()

        Signal 4: CLASS MATCH → 0.6
          anchor.lower() in any class name

        Returns the highest score across all anchors.
        """
        best_score = 0.0
        candidate_text = candidate.text.lower().strip()
        candidate_aria = candidate.aria_name.lower().strip()
        candidate_classes = " ".join(candidate.classes).lower()

        for anchor in anchors:
            anchor_clean = anchor.lower().strip()
            if not anchor_clean:
                continue

            # Signal 1: Exact match
            if candidate_text == anchor_clean:
                return 1.0

            # Signal 2: Containment (bidirectional for short strings)
            if len(candidate_text) < 80 and len(anchor_clean) >= 2:
                if anchor_clean in candidate_text:
                    score = 0.85
                    # Boost if text is very close in length (near-exact)
                    if len(candidate_text) <= len(anchor_clean) * 1.5:
                        score = 0.92
                    best_score = max(best_score, score)
                elif candidate_text in anchor_clean and len(candidate_text) >= 3:
                    best_score = max(best_score, 0.82)

            # Signal 3: ARIA/name match
            if candidate_aria and anchor_clean in candidate_aria:
                best_score = max(best_score, 0.8)

            # Signal 4: Class name match (weak signal)
            if anchor_clean in candidate_classes:
                best_score = max(best_score, 0.6)

        return best_score

    async def find_deterministic(
        self,
        intent: str,
        action_type: str = "find_and_click",
        container_selector: Optional[str] = None,
        position: Optional[int] = None,
    ) -> FindResult:
        """
        DOM-Aware Execution entry point: find element using Candidate Table
        + deterministic scoring. No AI, no fuzzy math.

        Pipeline:
        1. Build Candidate Table (top 20 interactables filtered by action)
        2. Parse intent into anchors
        3. Score each candidate deterministically
        4. Return best match above threshold (0.7)

        Returns FindResult with layer=HEURISTIC if found.
        """
        start_time = time.time()

        # Parse anchors from comma-separated intent
        anchors = [a.strip() for a in intent.split(",") if a.strip()]

        # Build constrained candidate table
        candidates = await self._build_candidate_table(
            action_type=action_type,
            container_selector=container_selector,
            max_candidates=20
        )

        if not candidates:
            return FindResult(layer=FinderLayer.NONE, candidates_checked=0)

        # Score all candidates
        scored: list[tuple[CandidateRow, float]] = []
        for candidate in candidates:
            score = self._score_candidate_deterministic(candidate, anchors)
            candidate.score = score
            if score > 0.0:
                scored.append((candidate, score))

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)

        # Check position hint (e.g., "first result" → position=0)
        if position is not None and scored:
            # Filter to only matching candidates, then pick by position
            matching = [(c, s) for c, s in scored if s >= 0.5]
            if position < len(matching):
                best_candidate, best_score = matching[position]
                duration = int((time.time() - start_time) * 1000)
                logger.info(
                    f"[Deterministic] ✅ Position match #{position}: "
                    f"'{best_candidate.text[:40]}' (score: {best_score:.2f}, "
                    f"region: {best_candidate.region}, {duration}ms)"
                )
                return FindResult(
                    element=best_candidate.handle,
                    layer=FinderLayer.HEURISTIC,
                    confidence=best_score,
                    duration_ms=duration,
                    candidates_checked=len(candidates),
                )

        # Return best match above threshold
        threshold = 0.7
        if scored and scored[0][1] >= threshold:
            best_candidate, best_score = scored[0]
            duration = int((time.time() - start_time) * 1000)
            logger.info(
                f"[Deterministic] ✅ HIT: '{best_candidate.text[:40]}' "
                f"(score: {best_score:.2f}, role: {best_candidate.role}, "
                f"region: {best_candidate.region}, {duration}ms)"
            )
            return FindResult(
                element=best_candidate.handle,
                layer=FinderLayer.HEURISTIC,
                confidence=best_score,
                duration_ms=duration,
                candidates_checked=len(candidates),
            )

        # Log top candidates for debugging
        duration = int((time.time() - start_time) * 1000)
        if scored:
            top3 = scored[:3]
            for i, (c, s) in enumerate(top3):
                logger.debug(
                    f"[Deterministic] Candidate #{i+1}: '{c.text[:40]}' "
                    f"score={s:.2f} role={c.role} region={c.region}"
                )
            logger.info(
                f"[Deterministic] ❌ MISS: best={scored[0][1]:.2f} < {threshold} "
                f"(checked: {len(candidates)}, {duration}ms)"
            )
        else:
            logger.info(f"[Deterministic] ❌ MISS: No candidates scored > 0 ({duration}ms)")

        return FindResult(
            layer=FinderLayer.NONE,
            confidence=scored[0][1] if scored else 0.0,
            duration_ms=duration,
            candidates_checked=len(candidates),
        )

    async def _recovery_ladder(
        self,
        intent: str,
        action_type: str = "find_and_click",
        container_selector: Optional[str] = None,
    ) -> FindResult:
        """
        3-step recovery when deterministic matching fails.

        Step 1: Retry with expanded candidate pool (top 40)
        Step 2: Re-scan DOM after 500ms wait (handles lazy/dynamic content)
        Step 3: LLM rescue with compact Candidate Table (~200 tokens)
        """
        start_time = time.time()
        anchors = [a.strip() for a in intent.split(",") if a.strip()]

        # ── Step 1: Expanded candidate pool ──────────────────────────
        logger.info("[Recovery] Step 1: Expanding candidate pool to 40...")
        candidates = await self._build_candidate_table(
            action_type=action_type,
            container_selector=container_selector,
            max_candidates=40
        )
        scored = []
        for c in candidates:
            s = self._score_candidate_deterministic(c, anchors)
            c.score = s
            if s > 0.0:
                scored.append((c, s))
        scored.sort(key=lambda x: x[1], reverse=True)

        if scored and scored[0][1] >= 0.6:
            best, best_score = scored[0]
            duration = int((time.time() - start_time) * 1000)
            logger.info(f"[Recovery] ✅ Step 1 HIT: '{best.text[:40]}' (score: {best_score:.2f}, {duration}ms)")
            return FindResult(
                element=best.handle, layer=FinderLayer.HEURISTIC,
                confidence=best_score, duration_ms=duration,
                candidates_checked=len(candidates),
            )

        # ── Step 2: Re-scan after 500ms wait ─────────────────────────
        logger.info("[Recovery] Step 2: Waiting 500ms for dynamic content...")
        await asyncio.sleep(0.5)
        candidates = await self._build_candidate_table(
            action_type=action_type,
            container_selector=container_selector,
            max_candidates=40
        )
        scored = []
        for c in candidates:
            s = self._score_candidate_deterministic(c, anchors)
            c.score = s
            if s > 0.0:
                scored.append((c, s))
        scored.sort(key=lambda x: x[1], reverse=True)

        if scored and scored[0][1] >= 0.55:
            best, best_score = scored[0]
            duration = int((time.time() - start_time) * 1000)
            logger.info(f"[Recovery] ✅ Step 2 HIT: '{best.text[:40]}' (score: {best_score:.2f}, {duration}ms)")
            return FindResult(
                element=best.handle, layer=FinderLayer.HEURISTIC,
                confidence=best_score, duration_ms=duration,
                candidates_checked=len(candidates),
            )

        # ── Step 3: LLM rescue with compact top-20 table ─────────────
        logger.info("[Recovery] Step 3: LLM rescue with compact candidate table...")
        try:
            # Build compact text representation for the LLM
            compact_table = self._format_candidate_table_for_llm(candidates[:20], intent)
            tree_context = compact_table

            result = await self._layer4_cognitive(intent, tree_context=tree_context)
            if result.found:
                result.new_signature = await self._compute_element_signature(result.element)
                result.duration_ms = int((time.time() - start_time) * 1000)
                logger.info(f"[Recovery] ✅ Step 3 LLM HIT in {result.duration_ms}ms")
                return result
        except Exception as e:
            logger.warning(f"[Recovery] Step 3 LLM failed: {e}")

        duration = int((time.time() - start_time) * 1000)
        logger.error(f"[Recovery] ❌ ALL 3 STEPS FAILED for '{intent}' ({duration}ms)")
        return FindResult(
            layer=FinderLayer.NONE, confidence=0.0,
            duration_ms=duration, error=f"Recovery ladder exhausted: {intent}",
        )

    def _format_candidate_table_for_llm(
        self,
        candidates: list[CandidateRow],
        intent: str
    ) -> str:
        """Format compact candidate table for LLM rescue (~200 tokens)."""
        lines = [f"Target: \"{intent}\"", "", "CANDIDATES:"]
        for i, c in enumerate(candidates):
            enabled_str = "enabled" if c.enabled else "disabled"
            text_display = c.text[:50] if c.text else "(empty)"
            aria_display = f" aria=\"{c.aria_name[:30]}\"" if c.aria_name else ""
            lines.append(
                f"#{i+1}  {c.role:<10} \"{text_display}\"{aria_display}  "
                f"{c.region:<8} {enabled_str}"
            )
        lines.append("")
        lines.append("Return ONLY the candidate number (1-20).")
        return "\n".join(lines)


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

    def _infer_action_type(self, intent: str, scan_mode: str) -> str:
        """
        Infer action constraint type from intent keywords and scan mode.

        Maps the semantic intent to ACTION_CONSTRAINTS keys so the
        Candidate Table is filtered appropriately.
        """
        intent_lower = intent.lower()

        # Explicit extraction signals
        extraction_keywords = {"extract", "read", "get", "text", "value", "price", "description", "about", "title"}
        if any(k in intent_lower for k in extraction_keywords) or scan_mode == "all":
            return "extract_text"

        # Type/input signals
        type_keywords = {"type", "enter", "input", "write", "fill", "search term", "query"}
        if any(k in intent_lower for k in type_keywords):
            return "type_text"

        # Select/dropdown signals
        select_keywords = {"select", "choose", "pick", "dropdown", "option"}
        if any(k in intent_lower for k in select_keywords):
            return "select_option"

        # Checkbox/toggle signals
        check_keywords = {"check", "toggle", "switch", "enable", "disable"}
        if any(k in intent_lower for k in check_keywords):
            return "check_element"

        # Hover signals
        hover_keywords = {"hover", "tooltip", "mouseover"}
        if any(k in intent_lower for k in hover_keywords):
            return "hover_element"

        # Default: click
        return "find_and_click"

    # -------------------------------------------------------------------------
    # LAYER 1: REFLEX (SimHash)
    # -------------------------------------------------------------------------
    async def _layer1_reflex(
        self,
        intent: str,
        target_simhash: str,
        container_selector: Optional[str] = None,
        scan_mode: str = "interactive"
    ) -> FindResult:
        """
        Layer 1: Find element by SimHash fingerprint.
        """
        candidates = await self._get_interactive_elements(
            container_selector=container_selector,
            scan_mode=scan_mode
        )

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
        container_selector: Optional[str] = None,
        scan_mode: str = "interactive",
        position: Optional[int] = None,
        discovery_mode: bool = False
    ) -> FindResult:
        """
        Layer 2: Find element by fuzzy text matching.
        """
        candidates = await self._get_interactive_elements(
            container_selector=container_selector,
            scan_mode=scan_mode
        )

        best_match: Optional[ElementCandidate] = None
        best_score = 0.0

        # Normalize intent for comparison
        intent_normalized = normalize_text(intent).lower()

        # ANCHOR-FIRST SCORING: Split comma-separated anchors into individual terms
        # Planner generates: "repository, name, title, header, top, trending"
        # We score EACH anchor independently against element signals
        anchors = [a.strip() for a in intent_normalized.split(",") if a.strip()]
        if not anchors:
            anchors = [intent_normalized]

        # Check for synonyms across all anchors
        synonyms = []
        for key, syn_list in self.INTENT_SYNONYMS.items():
            if key in intent_normalized or intent_normalized in key:
                synonyms.extend(syn_list)
            for anchor in anchors:
                if key in anchor or anchor in key:
                    synonyms.extend(syn_list)

        for candidate in candidates:
            # Collect all text signals from this element
            element_signals = [
                candidate.text.lower(),
                candidate.attributes.get("aria-label", "").lower(),
                candidate.attributes.get("placeholder", "").lower(),
                candidate.attributes.get("value", "").lower(),
                candidate.attributes.get("title", "").lower(),
                candidate.attributes.get("data-tooltip", "").lower(),
                candidate.attributes.get("alt", "").lower(),
                candidate.attributes.get("data-label", "").lower(),
                candidate.attributes.get("name", "").lower(),
                " ".join(candidate.classes).lower(),
            ]

            # Score each anchor INDIVIDUALLY against each signal, take the best
            best_anchor_score = 0.0
            for anchor in anchors:
                for signal in element_signals:
                    if not signal:
                        continue
                    score = hybrid_similarity(anchor, signal)
                    best_anchor_score = max(best_anchor_score, score)

            # Also score the full intent (handles cases where element text is multi-word)
            full_intent_score = 0.0
            for signal in element_signals:
                if not signal:
                    continue
                full_intent_score = max(full_intent_score, hybrid_similarity(intent_normalized, signal))

            # WORD-OVERLAP BOOST (Jaccard similarity on word level)
            intent_words = set()
            for anchor in anchors:
                intent_words.update(anchor.split())

            # Metadata signals for Jaccard
            meta_signals = " ".join(filter(None, [
                candidate.attributes.get("placeholder", "").lower(),
                candidate.attributes.get("aria-label", "").lower(),
                candidate.attributes.get("title", "").lower(),
                candidate.attributes.get("name", "").lower(),
                candidate.attributes.get("itemprop", "").lower(),
                candidate.attributes.get("id", "").lower(),
                candidate.attributes.get("data-testid", "").lower(),
                " ".join(candidate.classes).lower()
            ]))
            meta_words = set(meta_signals.split())
            text_words = set(candidate.text.lower().split())

            meta_jaccard = 0.0
            if intent_words and meta_words:
                overlap = len(intent_words & meta_words)
                union = len(intent_words | meta_words)
                meta_jaccard = overlap / union if union > 0 else 0.0

            text_jaccard = 0.0
            if intent_words and text_words:
                overlap = len(intent_words & text_words)
                union = len(intent_words | text_words)
                text_jaccard = overlap / union if union > 0 else 0.0

            word_score = max(min(meta_jaccard * 2.0, 1.0), text_jaccard)

            # Final score: best of per-anchor, full-intent, and word-overlap
            candidate.score = max(best_anchor_score, full_intent_score, word_score)

            # SYNONYM BOOST
            if synonyms:
                candidate_text = candidate.text.lower()
                for syn in synonyms:
                    if syn in candidate_text:
                        candidate.score += 0.2
                        break

            # FUNCTIONAL KEYWORD BOOST
            if "input" in intent_normalized and (candidate.tag == "input" or candidate.tag == "textarea"):
                candidate.score += 0.3
            elif "button" in intent_normalized and (candidate.tag == "button" or "btn" in candidate.classes):
                candidate.score += 0.2
            elif "link" in intent_normalized and candidate.tag == "a":
                candidate.score += 0.2
            elif "select" in intent_normalized and candidate.tag == "select":
                candidate.score += 0.3

            # LIST ITEM HEURISTICS
            list_keywords = ["repository", "article", "item", "result", "post", "product"]
            if any(k in intent_normalized for k in list_keywords):
                if candidate.tag in ["h1", "h2", "h3", "h4", "h5", "a"]:
                    candidate.score += 0.15

            # Cap score at 1.0
            candidate.score = min(candidate.score, 1.0)

        # ---------------------------------------------------------------------
        # PHASE 15 DETERMINISTIC PROXIMITY HEURISTIC
        # ---------------------------------------------------------------------
        # 1. Select the top text-matching anchor candidate
        anchor_candidate = None
        best_text_score = 0.0
        for candidate in candidates:
            if candidate.score > best_text_score:
                best_text_score = candidate.score
                anchor_candidate = candidate

        # 2. Rescore all candidates against the geometric/DOM anchor
        best_score = 0.0
        best_match = None
        if anchor_candidate and len(candidates) > 1:
            for candidate in candidates:
                geo_closeness = 0.0
                dom_closeness = 0.0
                same_container = 0.0
                import math

                if candidate != anchor_candidate:
                    dist = math.hypot(candidate.center_x - anchor_candidate.center_x, candidate.center_y - anchor_candidate.center_y)
                    geo_closeness = max(0.0, 1.0 - (dist / 1500.0))

                    if candidate.container_id and candidate.container_id == anchor_candidate.container_id:
                        same_container = 1.0

                    cp1 = candidate.dom_path.split('/')
                    cp2 = anchor_candidate.dom_path.split('/')
                    common = sum(1 for a, b in zip(cp1, cp2) if a == b and a)
                    dom_closeness = float(common) / max(len(cp1), 1)
                else:
                    geo_closeness = 1.0
                    dom_closeness = 1.0
                    same_container = 1.0

                # Check for role matches defined in layer configuration
                roles = ACTION_CONSTRAINTS.get(scan_mode, {}).get("roles", set())
                role_match = 1.0 if any(r in intent_normalized for r in roles) and candidate.role in intent_normalized else 0.0
                reading_order = 0.5  # Neutral baseline

                # Proximity Formula
                prox = 0.35 * dom_closeness + 0.35 * geo_closeness + 0.15 * same_container + 0.10 * reading_order + 0.05 * role_match

                # Combine base textual score with the deterministic proximity context
                candidate.score = (candidate.score * 0.4) + (prox * 0.6)
                candidate.score = min(candidate.score, 1.0)

                if candidate.score > best_score:
                    best_score = candidate.score
                    best_match = candidate
        else:
            best_match = anchor_candidate
            best_score = best_text_score

        # ---------------------------------------------------------------------
        # VISUAL SORT & POSITION SELECTION
        # ---------------------------------------------------------------------
        if position is not None:
            # 1. Collect all "Loose Matches" (score > 0.35)
            potential_matches = [c for c in candidates if c.score >= 0.35]

            if potential_matches:
                logger.debug(f"[Visual Sort] Found {len(potential_matches)} candidates for position {position}")

                # 2. Enrich with Y-coordinates (Bounding Box)
                enriched_matches = []
                for pm in potential_matches[:20]:  # Limit to top 20
                    try:
                        box = await pm.handle.bounding_box()
                        if box:
                            enriched_matches.append((pm, box['y'], box['x']))
                    except:
                        pass

                # 3. Sort by Y (top-to-bottom), then X (left-to-right)
                enriched_matches.sort(key=lambda item: (item[1], item[2]))

                # 4. Select by index — SUPPORTS NEGATIVE (-1 = last)
                if enriched_matches:
                    try:
                        # Python natively supports negative indexing but clamp to valid range
                        idx = position if position >= 0 else max(0, len(enriched_matches) + position)
                        selected_match, y, x = enriched_matches[idx]
                        logger.info(f"[Visual Sort] Selected item {idx} (pos={position}) at Y={y} (score: {selected_match.score:.2f})")
                        import uuid
                        return FindResult(
                            element=selected_match.handle,
                            selector_id=f"sel_{uuid.uuid4().hex[:8]}",
                            locator_type="semantic",
                            locator_value=selected_match.dom_path,
                            reason_codes=["VISUAL_SORT_SUCCESS", "HEURISTIC_MATCH"],
                            layer=FinderLayer.HEURISTIC,
                            confidence=selected_match.score,
                            candidates_checked=len(candidates)
                        )
                    except IndexError:
                        logger.warning(f"[Visual Sort] Position {position} out of range (found {len(enriched_matches)})")
                        # Fallback to normal best match logic below

        # discovery_mode takes precedence (preflight first-time verification)
        if discovery_mode:
            threshold = 0.65
        elif scan_mode == "all":
            threshold = 0.7
        else:
            threshold = self.LAYER2_THRESHOLD

        import uuid
        if best_match and best_score >= threshold:
            return FindResult(
                element=best_match.handle,
                selector_id=f"sel_{uuid.uuid4().hex[:8]}",
                locator_type="semantic",
                locator_value=best_match.dom_path,
                reason_codes=["DETERMINISTIC_PROXIMITY_SUCCESS"],
                layer=FinderLayer.HEURISTIC,
                confidence=best_score,
                candidates_checked=len(candidates)
            )

        # FALLBACK: Check for "Best Effort" match (> 0.55)
        if best_match and best_score >= 0.55:
            logger.info(f"[Layer 2] ⚠️ BEST EFFORT MATCH: {best_score:.2f} (threshold: {self.LAYER2_THRESHOLD})")
            return FindResult(
                element=best_match.handle,
                selector_id=f"sel_{uuid.uuid4().hex[:8]}",
                locator_type="semantic",
                locator_value=best_match.dom_path,
                reason_codes=["LOW_CONFIDENCE_BEST_EFFORT"],
                layer=FinderLayer.HEURISTIC,
                confidence=best_score,
                candidates_checked=len(candidates),
                error="Low confidence match (Best Effort)"
            )

        return FindResult(
            layer=FinderLayer.HEURISTIC,
            candidates_checked=len(candidates),
            confidence=best_score,
            error="ELEMENT_NOT_FOUND",
            reason_codes=["SCORE_BELOW_THRESHOLD"]
        )

    # -------------------------------------------------------------------------
    # LAYER 3: SEMANTIC (Vector DB)
    # -------------------------------------------------------------------------
    async def _layer3_semantic(
        self,
        intent: str,
        container_selector: Optional[str] = None,
        scan_mode: str = "interactive"
    ) -> FindResult:
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
    async def _layer4_cognitive(self, intent: str, tree_context: str = None) -> FindResult:
        """
        Layer 4: Use AI to recover the element via AXTree mapping.
        """
        try:
            from core.GlassBox import GlassBoxEngine
            glass = GlassBoxEngine()

            # 1. Get all candidates (including those outside current search scope)
            candidates = await self._get_interactive_elements(scan_mode="all")
            handles = [c.handle for c in candidates]

            # 2. Extract Pruned AXTree
            axtree_text, id_map = await glass.get_pruned_axtree(self.page, handles)

            # 3. Call AI agent to get Node_ID
            node_id_str = await self.ai_agent.recover(intent, axtree_map=axtree_text)

            if node_id_str and node_id_str.isdigit():
                node_id = int(node_id_str)
                element = id_map.get(node_id)
                if element:
                    logger.info(f"[Layer 4] AI matched intent to Node_ID: {node_id}")
                    return FindResult(
                        element=element,
                        layer=FinderLayer.COGNITIVE,
                        confidence=0.85
                    )

        except Exception as e:
            logger.warning(f"[Layer 4] Cognitive recovery failed: {e}")

        return FindResult(layer=FinderLayer.COGNITIVE)

    # -------------------------------------------------------------------------
    # HELPER METHODS
    # -------------------------------------------------------------------------
    async def _get_interactive_elements(
        self,
        container_selector: Optional[str] = None,
        include_shadow_dom: bool = True,
        include_iframes: bool = True,
        iframe_depth: int = 0,
        scan_mode: str = "interactive"
    ) -> list[ElementCandidate]:
        """
        Get relevant elements on the page.
        """
        candidates: list[ElementCandidate] = []
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

        # Build selector based on scan_mode
        if scan_mode == "all":
            # join all relevant selectors
            combined_selector = ", ".join(self.RELEVANT_SELECTORS)
        else:
            # interactive mode (filter definition of 'interactive')
            interactive_only = [
                s for s in self.RELEVANT_SELECTORS
                if not any(tag in s for tag in ["h1", "h2", "p", "table", "div", "span", "section", "article", "img"])
                or "btn" in s or "button" in s or "onclick" in s
            ]
            combined_selector = ", ".join(interactive_only)

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

            # Get text — input/textarea have no innerText, use placeholder or name instead
            text = ""
            if tag in ["input", "textarea"]:
                # Priority: value > placeholder > name > id (in order of usefulness)
                val = await element.get_attribute("value") or ""
                placeholder = await element.get_attribute("placeholder") or ""
                name = await element.get_attribute("name") or ""
                el_id = await element.get_attribute("id") or ""
                text = val or placeholder or name or el_id
            elif tag == "select":
                # Use name or id for select elements
                name = await element.get_attribute("name") or ""
                el_id = await element.get_attribute("id") or ""
                text = name or el_id
            else:
                try:
                    text = await element.inner_text()
                except:
                    pass

            # Get classes
            class_str = await element.get_attribute("class") or ""
            classes = class_str.split() if class_str else []

            # Get key attributes - AUDIT FIX: Blacklist approach to capture all useful data
            attributes = {"_position": str(position_index)}

            # Attributes to EXCLUDE (noise)
            ignored_attrs = {
                "style", "class", "width", "height", "tabindex", "spellcheck",
                "autocorrect", "autocapitalize", "action", "method"
            }

            # Get all attributes via JS evaluation (Playwright doesn't expose .attrs directly on handle)
            all_attrs = await element.evaluate("""el => {
                const attrs = {};
                for (const attr of el.attributes) {
                    attrs[attr.name] = attr.value;
                }
                return attrs;
            }""")

            for name, val in all_attrs.items():
                name_lower = name.lower()
                # Skip event handlers (on*) and ignored attributes
                if name_lower.startswith("on") or name_lower in ignored_attrs or name_lower.startswith("min-") or name_lower.startswith("max-"):
                    continue

                attributes[name_lower] = val

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
            selector = await self._get_optimized_selector(element)

            return {
                "selector": selector,
                "simhash": simhash,
                "tag": tag,
                "text": normalize_text(text)[:50],
                "classes": classes[:5],
                "attributes": attributes
            }

        except Exception as e:
            logger.warning(f"[SmartFinder] Error computing signature: {e}")
            return None

    async def _get_optimized_selector(self, element: ElementHandle) -> str:
        """
        Generates a stable CSS selector for an element.
        Priority: data-testid > id > name > aria-label > tag + specific class
        """
        try:
            # 1. Try stable attributes
            for attr in ["data-testid", "data-cy", "data-qa", "id", "name"]:
                val = await element.get_attribute(attr)
                if val:
                    if attr == "id":
                        return f"#{val}"
                    return f"[{attr}='{val}']"

            # 2. Falling back to tag + placeholder (for inputs)
            tag = await element.evaluate("el => el.tagName.toLowerCase()")
            placeholder = await element.get_attribute("placeholder")
            if placeholder:
                return f"{tag}[placeholder='{placeholder}']"

            # 3. Falling back to tag + first unique class
            class_str = await element.get_attribute("class") or ""
            classes = [c for c in class_str.split() if ":" not in c and " " not in c]
            if classes:
                # Use first class if it's not too generic (heuristic)
                for c in classes:
                    if len(c) > 3 and not any(x in c.lower() for x in ["hidden", "visible", "active"]):
                        return f"{tag}.{c}"

            # 4. Ultimate fallback: Simple tag (risky but better than nothing)
            return tag
        except:
            return tag if 'tag' in locals() else "unknown"


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
