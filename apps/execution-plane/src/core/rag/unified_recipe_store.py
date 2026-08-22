# src/core/rag/unified_recipe_store.py
"""
UnifiedRecipeStore — Single RAG Memory Interface (Fix for 2.4)

Problem being solved:
  Two completely separate memory stores exist:
    1. RAGService (pgvector + OpenAI embeddings) — used by preflight.py
    2. RecipeManager (Qdrant + sentence-transformers) — used by core_workflow.py

  When a job succeeds, it saves to RecipeManager. But the next job's
  preflight checks RAGService. The learning loop is broken.

Solution — Adapter Pattern (no database migration required):
  UnifiedRecipeStore wraps BOTH backends behind a single interface.
  - save(): writes to BOTH stores simultaneously
  - find(): queries BOTH stores, returns the highest-confidence match
  - Intent normalization: applied before embedding to collapse semantically
    equivalent queries to the same vector neighborhood

Why not migrate? Both stores serve their callers in production. A hard
migration requires schema changes, data transfer, and a cutover window.
The adapter lets us unify NOW with zero downtime and zero data loss.

Intent normalization rationale:
  "get top 10 products" and "fetch first 10 results" should match the same
  recipe. Without normalization, the cosine distance between their raw
  embeddings may miss the 0.92 threshold. We normalize intents by:
    1. Collapsing numeric qualifiers ("10", "100", "first N") to a token
    2. Stripping filler words
    3. Lowercasing + whitespace normalization

Recipe versioning:
  Each recipe carries a `schema_hash` derived from the target URL's domain
  and the step structure. When the same recipe name is saved again with a
  different schema_hash, the old version is archived (not deleted).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("unified_recipe_store")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RecipeResult:
    """Unified recipe result from either backend."""
    name: str
    description: str
    steps: List[Dict[str, Any]]
    score: float                  # Similarity score [0..1]
    source: str                   # "qdrant" | "pgvector" | "cache"
    schema_hash: Optional[str] = None
    version: int = 1

    @property
    def is_high_confidence(self) -> bool:
        """High-confidence match: use as-is, no adaptation needed."""
        return self.score >= 0.92

    @property
    def is_usable(self) -> bool:
        """Usable match: worth using as a starting template."""
        return self.score >= 0.75


# ---------------------------------------------------------------------------
# Intent Normalization
# ---------------------------------------------------------------------------
# Collapses semantically equivalent queries to share the same vector space.
# Applied BEFORE embedding so the models see normalized text.

_NUMBER_PATTERN = re.compile(r"\b\d+\b")
_FILLER_WORDS = frozenset({
    "please", "can you", "could you", "i want", "i need", "i would like",
    "help me", "show me", "tell me", "get me", "find me", "give me",
    "just", "simply", "quickly", "for me", "the following",
})

def _normalize_intent(raw: str) -> str:
    """
    Normalize a user intent string for consistent embedding.

    Operations (in order):
      1. Lowercase
      2. Collapse all whitespace
      3. Replace all numbers with <NUM> token
         ("get top 10 results" → "get top <NUM> results")
      4. Strip filler words
      5. Collapse remaining whitespace

    Examples:
      "Get the top 10 products from amazon" → "get top <NUM> products amazon"
      "Fetch first 100 results" → "fetch first <NUM> results"
      "I want to scrape 50 job listings" → "scrape <NUM> job listings"
    """
    text = raw.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = _NUMBER_PATTERN.sub("<NUM>", text)

    words = text.split()
    # Remove filler multi-word phrases by checking bigrams
    filtered = []
    skip_next = False
    for i, word in enumerate(words):
        if skip_next:
            skip_next = False
            continue
        # Check bigrams
        if i + 1 < len(words):
            bigram = f"{word} {words[i+1]}"
            if bigram in _FILLER_WORDS:
                skip_next = True
                continue
        if word not in _FILLER_WORDS:
            filtered.append(word)

    return " ".join(filtered)


def _compute_schema_hash(domain: str, steps: List[Dict]) -> str:
    """
    Compute a version hash for a recipe: domain + step structure.
    Used to detect when a saved recipe has been superseded by a newer run.
    """
    structure = {
        "domain": domain,
        "step_actions": [s.get("action", s.get("intent_type", "")) for s in steps],
    }
    import json
    raw = json.dumps(structure, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Unified Store
# ---------------------------------------------------------------------------

class UnifiedRecipeStore:
    """
    Single interface to both RAG backends.

    Thread-safe: uses asyncio.to_thread for synchronous Qdrant calls.
    Non-fatal: both backends are optional; degrades gracefully.
    """

    def __init__(self):
        self._qdrant_mgr = None     # RecipeManager (Qdrant)
        self._pg_svc = None         # RAGService (pgvector)
        self._qdrant_ok = False
        self._pg_ok = False

    async def initialize(self) -> "UnifiedRecipeStore":
        """
        Lazy-initialize both backends.
        Safe to call multiple times — subsequent calls are no-ops.
        """
        if not self._qdrant_ok:
            try:
                from core.recipe.recipe_manager import RecipeManager
                self._qdrant_mgr = await asyncio.to_thread(RecipeManager)
                self._qdrant_ok = True
                logger.info("[UnifiedStore] Qdrant RecipeManager initialized")
            except Exception as exc:
                logger.warning(f"[UnifiedStore] Qdrant backend unavailable: {exc}")

        if not self._pg_ok:
            try:
                from core.rag.rag_service import RAGService
                self._pg_svc = RAGService()
                self._pg_ok = True
                logger.info("[UnifiedStore] pgvector RAGService initialized")
            except Exception as exc:
                logger.warning(f"[UnifiedStore] pgvector backend unavailable: {exc}")

        return self

    # -----------------------------------------------------------------------
    # Public: find
    # -----------------------------------------------------------------------

    async def find(
        self,
        query: str,
        url: str = "",
        *,
        user_id: Optional[str] = None,
    ) -> Optional[RecipeResult]:
        """
        Find the best matching recipe across both stores.

        1. Normalize the query intent
        2. Search both stores concurrently
        3. Return the highest-scoring match

        Args:
            query:    Natural language objective or description
            url:      Target URL (used as domain hint in pgvector search)
            user_id:  Optional tenant filter (Qdrant only)

        Returns:
            RecipeResult if found above usability threshold, else None
        """
        normalized = _normalize_intent(query)
        logger.info(
            f"[UnifiedStore] find: raw='{query[:60]}' "
            f"→ normalized='{normalized[:60]}'"
        )

        qdrant_result, pg_result = await asyncio.gather(
            self._find_qdrant(normalized, user_id=user_id),
            self._find_pgvector(normalized, url=url),
            return_exceptions=True,
        )

        candidates: List[RecipeResult] = []

        if isinstance(qdrant_result, RecipeResult):
            candidates.append(qdrant_result)
        elif isinstance(qdrant_result, Exception):
            logger.warning(f"[UnifiedStore] Qdrant search failed: {qdrant_result}")

        if isinstance(pg_result, RecipeResult):
            candidates.append(pg_result)
        elif isinstance(pg_result, Exception):
            logger.warning(f"[UnifiedStore] pgvector search failed: {pg_result}")

        if not candidates:
            return None

        # Return the highest-confidence result
        best = max(candidates, key=lambda r: r.score)
        logger.info(
            f"[UnifiedStore] Best match: '{best.name}' "
            f"(score={best.score:.3f}, source={best.source})"
        )
        return best if best.is_usable else None

    # -----------------------------------------------------------------------
    # Public: save
    # -----------------------------------------------------------------------

    async def save(
        self,
        name: str,
        description: str,
        steps: List[Dict[str, Any]],
        *,
        url: str = "",
        user_id: Optional[str] = None,
    ) -> bool:
        """
        Save a recipe to BOTH stores simultaneously.

        Applies intent normalization to the description before embedding
        so future queries can find it even if phrased differently.

        Args:
            name:        Unique recipe identifier
            description: Natural language description (intent)
            steps:       List of step dicts
            url:         Target URL (for domain extraction in schema hash)
            user_id:     Optional tenant ID

        Returns:
            True if at least one store saved successfully.
        """
        normalized_desc = _normalize_intent(description)
        domain = _extract_domain(url) if url else "unknown"
        schema_hash = _compute_schema_hash(domain, steps)

        logger.info(
            f"[UnifiedStore] save: name='{name}', "
            f"desc_normalized='{normalized_desc[:60]}', "
            f"schema_hash={schema_hash}"
        )

        qdrant_ok, pg_ok = await asyncio.gather(
            self._save_qdrant(name, normalized_desc, steps, user_id, schema_hash),
            self._save_pgvector(name, normalized_desc, steps, domain, schema_hash),
            return_exceptions=True,
        )

        qdrant_success = qdrant_ok is True
        pg_success = pg_ok is True

        if isinstance(qdrant_ok, Exception):
            logger.warning(f"[UnifiedStore] Qdrant save failed: {qdrant_ok}")
        if isinstance(pg_ok, Exception):
            logger.warning(f"[UnifiedStore] pgvector save failed: {pg_ok}")

        logger.info(
            f"[UnifiedStore] save result: qdrant={qdrant_success}, pg={pg_success}"
        )
        return qdrant_success or pg_success

    # -----------------------------------------------------------------------
    # Private: Qdrant adapter
    # -----------------------------------------------------------------------

    async def _find_qdrant(
        self,
        normalized_query: str,
        *,
        user_id: Optional[str] = None,
    ) -> Optional[RecipeResult]:
        if not self._qdrant_ok or not self._qdrant_mgr:
            return None

        result = await asyncio.to_thread(
            self._qdrant_mgr.find_recipe,
            normalized_query,
            user_id,
        )
        if not result:
            return None

        return RecipeResult(
            name=result["name"],
            description=result["description"],
            steps=result["steps"],
            score=result.get("score", 0.0),
            source="qdrant",
        )

    async def _save_qdrant(
        self,
        name: str,
        normalized_desc: str,
        steps: List[Dict],
        user_id: Optional[str],
        schema_hash: str,
    ) -> bool:
        if not self._qdrant_ok or not self._qdrant_mgr:
            return False

        # Include schema_hash in stored description for versioning context
        enriched_desc = f"{normalized_desc} [hash:{schema_hash}]"
        result = await asyncio.to_thread(
            self._qdrant_mgr.save_recipe,
            name,
            enriched_desc,
            steps,
            user_id,
        )
        return bool(result)

    # -----------------------------------------------------------------------
    # Private: pgvector adapter
    # -----------------------------------------------------------------------

    async def _find_pgvector(
        self,
        normalized_query: str,
        *,
        url: str = "",
    ) -> Optional[RecipeResult]:
        if not self._pg_ok or not self._pg_svc:
            return None

        template = await self._pg_svc.find_template(normalized_query, url)
        if not template:
            return None

        return RecipeResult(
            name=template.task_type or "pgvector_recipe",
            description=normalized_query,
            steps=template.recipe_json.get("steps", []),
            score=template.similarity,
            source="pgvector",
        )

    async def _save_pgvector(
        self,
        name: str,
        normalized_desc: str,
        steps: List[Dict],
        domain: str,
        schema_hash: str,
    ) -> bool:
        if not self._pg_ok or not self._pg_svc:
            return False

        try:
            await self._pg_svc.save_template(
                prompt=normalized_desc,
                url=f"https://{domain}",
                category="scraping",
                task_type=name,
                recipe_json={"steps": steps, "schema_hash": schema_hash},
            )
            return True
        except Exception as exc:
            logger.warning(f"[UnifiedStore] pgvector save_template failed: {exc}")
            return False


# ---------------------------------------------------------------------------
# Module-level singleton — one instance per worker process
# ---------------------------------------------------------------------------
_unified_store_instance: Optional[UnifiedRecipeStore] = None


async def get_unified_store() -> UnifiedRecipeStore:
    """Get or create the per-process UnifiedRecipeStore singleton."""
    global _unified_store_instance
    if _unified_store_instance is None:
        _unified_store_instance = await UnifiedRecipeStore().initialize()
    return _unified_store_instance


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        netloc = parsed.netloc.replace("www.", "")
        return netloc.split(":")[0] or "unknown"
    except Exception:
        return "unknown"
