"""
elementMatcher.py — Multi-Dimensional Element Scoring Engine

PURPOSE:
  Given an Intent and a DOMSnapshot, scores every visible element in the DOM
  against the intent across 4 orthogonal dimensions and returns the best
  candidate — without any LLM call.

SCORING MATRIX (4 dimensions, weights sum to 1.00):
  1. Spatial Geometry     (0.20) — Hard gate: invisible / zero-size → total 0.0
  2. Lexical Distance     (0.40) — Levenshtein + substring inclusion bonus
  3. Semantic Distance    (0.25) — Cosine similarity via external ONNX service
  4. Structural Depth     (0.15) — Tag semantic role + XPath nesting penalty

DELTA THRESHOLDING (after scoring):
  - Absolute Failure: best_score < 0.65 → MatchResult(found=False, requires_llm=True),
    unless QUANTA_MATCH_RAISE_ON_GATE=1 (legacy raise).
  - Ambiguity: delta(#1, #2) < 0.05 → still returns top-ranked element with ambiguous=True.

ASYNC DESIGN:
  - match() and getTopCandidates() are async coroutines.
  - ONNX embedding calls are batched via asyncio.gather() — non-blocking I/O.
  - The worker event loop is never blocked by tensor math.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

import aiohttp
from rapidfuzz.distance import Levenshtein

from core.browser.dom_harvester import DOMElement, DOMSnapshot
from core.planning.intent_parser import Intent
from exceptions import AIFallbackTriggered

logger = logging.getLogger("elementMatcher")

# ─── CONFIG ───────────────────────────────────────────────────────────────────

ONNX_SERVICE_URL: str = os.getenv(
    "ONNX_EMBEDDING_SERVICE_URL", "http://localhost:8100/v1/similarity"
)
ONNX_TIMEOUT_SECONDS: float = 2.0   # Hard cap — never block the worker longer

# When False (default), semantic score reuses lexical — no outbound HTTP to ONNX.
# Set QUANTA_SEMANTIC_EMBEDDINGS=1 to enable ONNX similarity calls.
_SEMANTIC_ENV = os.getenv("QUANTA_SEMANTIC_EMBEDDINGS", "").strip().lower()
SEMANTIC_EMBEDDINGS_ENABLED: bool = _SEMANTIC_ENV in ("1", "true", "yes", "on", "onnx")

# When True, match() raises AIFallbackTriggered on gate failures (legacy/tests).
_MATCH_RAISE_ENV = os.getenv("QUANTA_MATCH_RAISE_ON_GATE", "").strip().lower()
MATCH_RAISE_ON_GATE: bool = _MATCH_RAISE_ENV in ("1", "true", "yes")

# ─── THRESHOLDS ───────────────────────────────────────────────────────────────

# FIX RC4: Lowered from 0.65 to 0.50.
# With ONNX disabled and semantic score previously copied from lexical,
# a correct synonym match like "login" → "Sign in" produced total ~0.567,
# which fell below 0.65. Lowering to 0.50 accounts for the reduced
# independent signal when ONNX is unavailable.
CONFIDENCE_FLOOR     = 0.50   # Below this → absolute failure → LLM fallback
CONFIDENT_THRESHOLD  = 0.50   # Alias for temporal activity discovery logic
AMBIGUITY_DELTA      = 0.05   # If top-two gap < this → collision → LLM fallback

# ─── SYNONYM MAP (Tier 1 of 3-tier semantic scorer) ───────────────────────────
# FIX RC5 / RC3: Static vocabulary for universal web actions.
# Covers ~20% of real-world cases instantly at zero cost.
# The remaining 80% (domain-specific, brand voice, non-English) is handled
# by Tier 2 (structural DOM signals) and Tier 3 (TensorEngine embeddings).
SYNONYM_MAP: dict[str, list[str]] = {
    "login":        ["sign in", "log in", "signin", "login", "enter", "auth"],
    "sign in":      ["login", "log in", "signin", "enter"],
    "sign up":      ["register", "create account", "join", "get started"],
    "register":     ["sign up", "create account", "join"],
    "search":       ["find", "query", "lookup", "explore"],
    "submit":       ["send", "confirm", "apply", "go", "done", "ok"],
    "buy":          ["add to cart", "purchase", "checkout", "order"],
    "close":        ["dismiss", "cancel", "x", "exit"],
    "next":         ["continue", "forward", "proceed", "›", "»"],
    "previous":     ["back", "prev", "‹", "«"],
    "download":     ["save", "export", "get"],
    "delete":       ["remove", "trash", "clear"],
    "edit":         ["modify", "update", "change"],
    "save":         ["apply", "confirm", "done", "update"],
    "password":     ["pass", "secret", "pin"],
    "username":     ["user", "email", "login", "account"],
}

# ─── DIMENSION WEIGHTS (must sum to 1.00) ─────────────────────────────────────

W_SPATIAL    = 0.20
W_LEXICAL    = 0.40
W_SEMANTIC   = 0.25
W_STRUCTURAL = 0.15

# ─── HELPERS ──────────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def _lev(a: str, b: str) -> float:
    """Levenshtein normalized similarity via RapidFuzz [0.0, 1.0]."""
    if not a or not b:
        return 0.0
    return Levenshtein.normalized_similarity(a, b)


# ─── TAG ROLE TABLE ───────────────────────────────────────────────────────────
# Maps HTML tags to their inherent "interactivity" score for Structural Depth.

TAG_ROLE_SCORES: dict[str, float] = {
    "button":   1.0,
    "a":        1.0,
    "input":    1.0,
    "select":   0.8,
    "textarea": 0.8,
    "summary":  0.7,
    "details":  0.7,
    "label":    0.5,
    "li":       0.5,
    "td":       0.5,
    "th":       0.5,
    "h1":       0.4,
    "h2":       0.4,
    "h3":       0.4,
    "h4":       0.4,
    "article":  0.4,
    "section":  0.4,
    "p":        0.3,
    "div":      0.3,
    "span":     0.3,
}

XPATH_DEPTH_PENALTY_THRESHOLD = 15  # Depths > 15 indicate layout wrapper nesting


# ─── DATA CLASSES ─────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """
    Result of an element matching operation.
    A 'found' result always has an element and a confidence score.
    Soft-gate mode (default): low confidence / empty scored set returns found=False
    with requires_llm=True instead of raising. Ambiguity returns the top-ranked
    element with ambiguous=True.
    """
    found: bool
    element: Optional[DOMElement]    = None
    confidence: float                = 0.0
    scoreBreakdown: dict[str, float] = field(default_factory=dict)
    candidatesEvaluated: int         = 0
    ambiguous: bool                  = False
    failure_reason: Optional[str]    = None
    requires_llm: bool               = False

    @property
    def escalateToLlm(self) -> bool:
        """Alias for older call sites (testServer, hybridActivities)."""
        return self.requires_llm


# ─── SEMANTIC ROLE TABLE ──────────────────────────────────────────────────────
# Maps intent target keywords → expected DOM properties.
# Used as a secondary boost inside the Lexical dimension when tag/type/role
# alignment is detected. Kept from the previous implementation for continuity.

SEMANTIC_ROLES: dict[str, dict] = {
    # Search
    "search":        {"tags": ["input"], "types": ["search", "text"], "roles": ["searchbox"],       "ariaKeywords": ["search", "query", "find"]},
    "search bar":    {"tags": ["input"], "types": ["search", "text"], "roles": ["searchbox"],       "ariaKeywords": ["search", "query"]},
    "search box":    {"tags": ["input"], "types": ["search", "text"], "roles": ["searchbox"],       "ariaKeywords": ["search"]},

    # Login / Auth
    "login":         {"tags": ["button","input"], "types": ["submit"],                              "ariaKeywords": ["login","sign in","log in","enter"]},
    "sign in":       {"tags": ["button","input"], "types": ["submit"],                              "ariaKeywords": ["sign in","login","log in"]},
    "sign up":       {"tags": ["button","input"], "types": ["submit"],                              "ariaKeywords": ["sign up","register","create account"]},
    "email":         {"tags": ["input"],          "types": ["email","text"],                        "ariaKeywords": ["email","e-mail","username"]},
    "password":      {"tags": ["input"],          "types": ["password"],                            "ariaKeywords": ["password","pass","secret"]},
    "username":      {"tags": ["input"],          "types": ["text","email"],                        "ariaKeywords": ["username","user","login","email"]},

    # Submit / CTA
    "submit":        {"tags": ["button","input"], "types": ["submit"],      "roles": ["button"],    "ariaKeywords": ["submit","send","confirm","ok","done"]},
    "button":        {"tags": ["button"],                                    "roles": ["button"],    "ariaKeywords": []},

    # Navigation
    "next":          {"tags": ["a","button"],                               "roles": ["button","link"], "ariaKeywords": ["next","forward","continue","›","»"]},
    "previous":      {"tags": ["a","button"],                               "roles": ["button","link"], "ariaKeywords": ["previous","back","prev","‹","«"]},
    "link":          {"tags": ["a"],                                        "roles": ["link"],          "ariaKeywords": []},
    "menu":          {"tags": ["button","a","nav"],                         "roles": ["menu","navigation","menuitem"],"ariaKeywords": ["menu", "nav"]},
    "home":          {"tags": ["a"],                                                                 "ariaKeywords": ["home","main","start"]},

    # Content / Scrape targets
    "title":         {"tags": ["h1","h2","h3","h4","span","p","div"],                               "ariaKeywords": []},
    "heading":       {"tags": ["h1","h2","h3","h4"],                                                "ariaKeywords": ["heading","title"]},
    "result":        {"tags": ["a","li","article","div"],                                           "ariaKeywords": ["result","article","item"]},
    "price":         {"tags": ["span","div","p"],                                                   "ariaKeywords": ["price","cost","amount","$","£","€"]},
    "article":       {"tags": ["article","div","section","p"],                                      "ariaKeywords": ["article","post","story"]},
    "image":         {"tags": ["img"],                                                              "ariaKeywords": []},
    "text":          {"tags": ["p","span","div","article"],                                         "ariaKeywords": []},

    # Forms
    "checkbox":      {"tags": ["input"],          "types": ["checkbox"],     "roles": ["checkbox"], "ariaKeywords": ["check","agree","accept"]},
    "dropdown":      {"tags": ["select"],                                    "roles": ["combobox","listbox"], "ariaKeywords": ["select","choose","dropdown"]},
    "select":        {"tags": ["select"],                                    "roles": ["combobox"],  "ariaKeywords": []},

    # Actions
    "close":         {"tags": ["button"],                                   "roles": ["button"],    "ariaKeywords": ["close","dismiss","cancel","×","x"]},
    "accept":        {"tags": ["button"],                                   "roles": ["button"],    "ariaKeywords": ["accept","agree","allow","ok","got it"]},
    "add to cart":   {"tags": ["button"],                                   "roles": ["button"],    "ariaKeywords": ["add to cart","buy","purchase","add"]},
    "download":      {"tags": ["a","button"],                               "roles": ["button","link"],"ariaKeywords": ["download","save","get"]},
}


# ─── ELEMENT MATCHER ──────────────────────────────────────────────────────────

class ElementMatcher:
    """
    Async multi-dimensional element scorer.
    Given an Intent + DOMSnapshot, scores every visible DOM element across
    4 orthogonal dimensions and returns the deterministic best match.

    Raises AIFallbackTriggered if the math cannot resolve a confident winner.

    Usage:
        matcher = ElementMatcher()
        result  = await matcher.match(intent, snapshot)
        # If we reach this line, result.element is the deterministic winner.
        # If AIFallbackTriggered was raised, pass result.top_candidates to LLM.
    """

    async def match(self, intent: Intent, snapshot: DOMSnapshot) -> MatchResult:
        """
        Main matching entrypoint.

        Args:
            intent:   The parsed Intent for this step.
            snapshot: The full DOMSnapshot from DOMHarvester.

        Returns:
            MatchResult with the best candidate and confidence score.

        Raises (only when QUANTA_MATCH_RAISE_ON_GATE=1):
            AIFallbackTriggered on no candidates, empty scores, absolute failure,
            or ambiguity collision.
        """
        target = intent.targetDescription.lower().strip()

        # ── Pre-filter: only visible elements with nonzero bounding boxes ─────
        candidates = [
            el for el in snapshot.elements
            if el.isVisible and el.width > 0 and el.height > 0
        ]

        if not candidates:
            logger.warning(
                f"[ElementMatcher] No visible elements for intent: '{target}'"
            )
            if MATCH_RAISE_ON_GATE:
                raise AIFallbackTriggered.absolute_failure(
                    best_score=0.0,
                    top_candidates=[],
                )
            return MatchResult(
                found=False,
                confidence=0.0,
                candidatesEvaluated=0,
                failure_reason="NO_VISIBLE_CANDIDATES",
                requires_llm=True,
            )

        # ── Score every candidate (async — ONNX calls batched via gather) ─────
        scoring_tasks = [
            self._scoreElement(el, target, intent.action)
            for el in candidates
        ]
        results = await asyncio.gather(*scoring_tasks)

        # Pair results with elements, drop near-zero scores
        scored: list[tuple[float, dict[str, float], DOMElement]] = []
        for (totalScore, breakdown), el in zip(results, candidates):
            if totalScore > 0.05:
                scored.append((totalScore, breakdown, el))

        if not scored:
            if MATCH_RAISE_ON_GATE:
                raise AIFallbackTriggered.absolute_failure(
                    best_score=0.0,
                    top_candidates=[],
                )
            return MatchResult(
                found=False,
                confidence=0.0,
                candidatesEvaluated=len(candidates),
                failure_reason="SCORED_EMPTY",
                requires_llm=True,
            )

        # Sort descending by score
        scored.sort(key=lambda x: x[0], reverse=True)

        # ── Apply positional qualifier (first / last / nth) ───────────────────
        index = self._resolveQualifier(intent.qualifier, len(scored))
        bestScore, breakdown, bestElement = scored[index]

        # ── Build diagnostic payload for potential fallback ────────────────────
        topN = scored[:5]
        diagnosticCandidates = [
            (s, el.qId, el.tag, (el.text or "")[:50])
            for s, _, el in topN
        ]

        # ── GATE 1: Absolute Failure ──────────────────────────────────────────
        if bestScore < CONFIDENCE_FLOOR:
            logger.warning(
                f"[ElementMatcher] ABSOLUTE_FAILURE | Intent='{target}' | "
                f"BestScore={bestScore:.3f} < floor={CONFIDENCE_FLOOR}"
            )
            if MATCH_RAISE_ON_GATE:
                raise AIFallbackTriggered.absolute_failure(
                    best_score=bestScore,
                    top_candidates=diagnosticCandidates,
                )
            return MatchResult(
                found=False,
                confidence=bestScore,
                scoreBreakdown=breakdown,
                candidatesEvaluated=len(candidates),
                failure_reason="ABSOLUTE_FAILURE",
                requires_llm=True,
            )

        # ── GATE 2: Ambiguity Collision (deterministic: still return #1) ─────
        ambiguous = False
        delta = 0.0
        if len(scored) > 1:
            delta = scored[0][0] - scored[1][0]
            if delta < AMBIGUITY_DELTA:
                logger.warning(
                    f"[ElementMatcher] AMBIGUITY_COLLISION | Intent='{target}' | "
                    f"#1={scored[0][0]:.3f} #2={scored[1][0]:.3f} delta={delta:.3f}"
                )
                if MATCH_RAISE_ON_GATE:
                    raise AIFallbackTriggered.ambiguity_collision(
                        best_score=bestScore,
                        delta=delta,
                        top_candidates=diagnosticCandidates,
                    )
                ambiguous = True

        # ── PASS: Deterministic match (ambiguous may still be True) ───────────
        logger.info(
            f"[ElementMatcher] MATCH | Intent='{target}' | "
            f"Score={bestScore:.3f} | Element=<{bestElement.tag}> "
            f"'{(bestElement.text or '')[:30]}' | qId={bestElement.qId} | "
            f"Candidates={len(scored)} | ambiguous={ambiguous}"
        )

        return MatchResult(
            found=True,
            element=bestElement,
            confidence=bestScore,
            scoreBreakdown=breakdown,
            candidatesEvaluated=len(candidates),
            ambiguous=ambiguous,
            requires_llm=False,
        )

    async def getTopCandidates(
        self, intent: Intent, snapshot: DOMSnapshot, n: int = 5
    ) -> list[tuple[float, DOMElement]]:
        """
        Returns the top N scored candidates for an intent.
        Used when escalating to LLM — we pre-filter 600 elements down to 5
        so the LLM receives a clean, small context to decide from.
        """
        target = intent.targetDescription.lower().strip()
        candidates = [
            el for el in snapshot.elements
            if el.isVisible and el.width > 0 and el.height > 0
        ]

        scoring_tasks = [
            self._scoreElement(el, target, intent.action)
            for el in candidates
        ]
        results = await asyncio.gather(*scoring_tasks)

        scored = []
        for (score, _), el in zip(results, candidates):
            if score > 0.05:
                scored.append((score, el))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:n]

    # ── PRIVATE ────────────────────────────────────────────────────────────

    async def _scoreElement(
        self, el: DOMElement, target: str, action: str
    ) -> tuple[float, dict[str, float]]:
        """
        Computes a weighted 4-dimensional similarity score.
        Returns (total_score, dimension_breakdown).

        If the Spatial gate fails (invisible / zero-size), returns 0.0 immediately.
        """
        breakdown: dict[str, float] = {}

        # ── Dimension 1: Spatial Geometry (hard gate) ─────────────────────────
        spatialScore = self._scoreSpatial(el)
        breakdown["spatial"] = spatialScore
        if spatialScore == 0.0:
            # Hard gate — element is a honeypot or hidden. Abort.
            return 0.0, breakdown

        # ── Dimension 2: Lexical Distance ─────────────────────────────────────
        lexicalScore = self._scoreLexical(el, target)
        breakdown["lexical"] = lexicalScore

        # ── Dimension 3: Semantic Distance ────────────────────────────────────
        elementText = self._concatenateTextSignals(el)
        if SEMANTIC_EMBEDDINGS_ENABLED:
            semanticScore = await self._fetchSemanticSimilarity(target, elementText)
        else:
            # FIX RC3: Replace the no-op "semanticScore = lexicalScore" with a
            # real 3-tier local semantic scorer that provides independent signal.
            semanticScore = await self._scoreSemanticLocal(target, elementText, el)
        breakdown["semantic"] = semanticScore

        # ── Dimension 4: Structural Depth ─────────────────────────────────────
        structuralScore = self._scoreStructural(el)
        breakdown["structural"] = structuralScore

        # ── Weighted Sum ──────────────────────────────────────────────────────
        total = (
            spatialScore    * W_SPATIAL +
            lexicalScore    * W_LEXICAL +
            semanticScore   * W_SEMANTIC +
            structuralScore * W_STRUCTURAL
        )

        return max(0.0, min(1.0, total)), breakdown

    # ── DIMENSION SCORERS ─────────────────────────────────────────────────

    @staticmethod
    def _scoreSpatial(el: DOMElement) -> float:
        """
        Spatial Geometry gate.
        If width == 0 or height == 0 or not visible → 0.0 (hard kill).
        Otherwise → 1.0 (element occupies real screen space).
        """
        if not el.isVisible:
            return 0.0
        if el.width <= 0 or el.height <= 0:
            return 0.0
        return 1.0

    def _scoreLexical(self, el: DOMElement, target: str) -> float:
        """
        Lexical Distance scorer.
        Concatenates all text signals, normalizes both sides, and computes
        Levenshtein normalized similarity.

        FIX RC5: Synonym expansion added.
        After the base Levenshtein score, we also score against all synonyms
        of the intent (from SYNONYM_MAP) and take the maximum. This ensures
        "login" vs "sign in" scores ~0.90 instead of ~0.43.

        Substring Inclusion Bonus:
            If normalized target is fully contained within element text,
            floor this dimension at 0.90.
        """
        elementText = self._concatenateTextSignals(el)
        normTarget = _norm(target)
        normElement = _norm(elementText[:200])  # Cap to prevent explosion

        if not normTarget or not normElement:
            return 0.0

        # Base Levenshtein similarity
        ratio = _lev(normTarget, normElement)

        # Substring inclusion bonus
        if normTarget in normElement:
            ratio = max(ratio, 0.90)
        elif normElement in normTarget:
            ratio = max(ratio, 0.85)

        # ── FIX RC5: Synonym expansion (forward direction) ────────────────────
        # Score against all synonyms of the intent and take the max.
        # e.g. intent="login" → synonyms=["sign in","log in",...] → score vs "sign in" = 0.90
        for syn in SYNONYM_MAP.get(normTarget, []):
            syn_norm = _norm(syn)
            if not syn_norm:
                continue
            syn_ratio = _lev(syn_norm, normElement)
            if syn_norm in normElement:
                syn_ratio = max(syn_ratio, 0.90)
            elif normElement in syn_norm:
                syn_ratio = max(syn_ratio, 0.85)
            ratio = max(ratio, syn_ratio)

        # ── FIX RC5: Synonym expansion (reverse direction) ────────────────────
        # Check if the intent appears in any synonym list, and if the key of
        # that list has high similarity to the element text.
        # e.g. element="sign in", intent="login" → "login" is in SYNONYM_MAP["sign in"]
        for key, synonyms in SYNONYM_MAP.items():
            if normTarget in [_norm(s) for s in synonyms]:
                key_norm = _norm(key)
                key_ratio = _lev(key_norm, normElement)
                if key_norm in normElement:
                    key_ratio = max(key_ratio, 0.90)
                if key_ratio >= 0.70:
                    ratio = max(ratio, 0.85)
                    break

        return ratio

    async def _scoreSemanticLocal(
        self, target: str, elementText: str, el: DOMElement
    ) -> float:
        """
        FIX RC3: 3-tier local semantic scorer. No external service required.

        Replaces the broken "semanticScore = lexicalScore" fallback with a
        real independent signal that covers the full range of real-world websites.

        Tier 1 — SYNONYM_MAP (zero-cost, ~20% coverage):
            Instant lookup for universal web vocabulary.
            "login" ↔ "sign in", "submit" ↔ "send", etc.

        Tier 2 — Structural DOM signals (zero-cost, ~40% additional coverage):
            Checks aria-label, role, type, placeholder against SEMANTIC_ROLES.
            Handles accessibility-annotated elements correctly.

        Tier 3 — TensorEngine embedding cosine similarity (~40% additional):
            Uses the all-MiniLM-L6-v2 singleton already loaded in the worker.
            Handles domain-specific CTAs ("Book Now"), brand voice ("Let's Go"),
            and non-English text ("Anmelden", "登录") that no static map can cover.
            This is a local CPU operation (~2ms), NOT an external API call.
            Graceful degradation: if TensorEngine unavailable, falls back to
            Levenshtein baseline.
        """
        normTarget = _norm(target)
        normElement = _norm(elementText[:200])

        if not normTarget or not normElement:
            return 0.0

        # ── Tier 1: SYNONYM_MAP (zero-cost) ──────────────────────────────────
        for key, synonyms in SYNONYM_MAP.items():
            group = {_norm(key)} | {_norm(s) for s in synonyms}
            target_in_group = any(
                t == normTarget or t in normTarget or normTarget in t
                for t in group if t
            )
            element_in_group = any(
                e == normElement or e in normElement or normElement in e
                for e in group if e
            )
            if target_in_group and element_in_group:
                return 0.85

        # ── Tier 2: Structural DOM signals (zero-cost) ────────────────────────
        role_entry = SEMANTIC_ROLES.get(normTarget)
        if role_entry:
            aria_keywords  = role_entry.get("ariaKeywords", [])
            el_aria        = _norm(el.ariaLabel   or "")
            el_role        = _norm(el.role        or "")
            el_type        = _norm(el.type        or "")
            el_placeholder = _norm(el.placeholder or "")
            for kw in aria_keywords:
                kw_norm = _norm(kw)
                if kw_norm and (
                    kw_norm in el_aria or
                    kw_norm in el_role or
                    kw_norm in el_type or
                    kw_norm in el_placeholder or
                    kw_norm in normElement
                ):
                    return 0.80

        # ── Tier 3: TensorEngine embedding cosine similarity (~2ms local CPU) ─
        # The all-MiniLM-L6-v2 model is already loaded as a singleton in the
        # worker process. No cold-start cost, no external service.
        if normTarget and normElement:
            try:
                from core.TensorEngine import TensorEngine
                engine = TensorEngine()
                element_vector = engine.model.encode(
                    normElement,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                )
                score = engine.compute_relevance(element_vector, normTarget)
                # Clamp: cosine similarity can be negative; floor at 0
                return max(0.0, float(score))
            except Exception:
                # Graceful degradation — TensorEngine unavailable
                pass

        # Fallback: Levenshtein baseline
        return _lev(normTarget, normElement)

    @staticmethod
    def _scoreStructural(el: DOMElement) -> float:
        """
        Structural Depth scorer.
        Two components:
          1. Tag semantic role — button/a/input → 1.0, div/span → 0.3
          2. XPath depth penalty — count('/') > 15 → proportional deduction

        The deeper an element is nested, the more likely it is a layout
        wrapper rather than a core interactive target.
        """
        # Tag role score
        tagScore = TAG_ROLE_SCORES.get(el.tag.lower(), 0.1)

        # XPath depth penalty
        depth = el.xpath.count("/") if el.xpath else 0
        depthPenalty = 0.0
        if depth > XPATH_DEPTH_PENALTY_THRESHOLD:
            # Linear penalty: each level past 15 costs 0.03, capped at 0.30
            excessDepth = depth - XPATH_DEPTH_PENALTY_THRESHOLD
            depthPenalty = min(excessDepth * 0.03, 0.30)

        return max(0.0, tagScore - depthPenalty)

    # ── SEMANTIC DISTANCE (ONNX MICROSERVICE) ─────────────────────────────

    @staticmethod
    async def _fetchSemanticSimilarity(
        targetIntent: str, elementText: str
    ) -> float:
        """
        Non-blocking REST call to the ONNX embedding microservice.
        Returns cosine similarity [0.0, 1.0].

        On timeout, connection error, or any failure → graceful degradation
        to 0.0. The worker event loop is NEVER blocked.

        Endpoint contract:
            POST /v1/similarity
            Body:  {"text_a": "...", "text_b": "..."}
            Reply: {"similarity": 0.87}
        """
        if not targetIntent or not elementText:
            return 0.0

        try:
            timeout = aiohttp.ClientTimeout(total=ONNX_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    ONNX_SERVICE_URL,
                    json={"text_a": targetIntent, "text_b": elementText},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get("similarity", 0.0))
                    else:
                        logger.warning(
                            f"[ElementMatcher] ONNX service returned {resp.status}"
                        )
                        return 0.0
        except asyncio.TimeoutError:
            logger.warning("[ElementMatcher] ONNX service timed out — using 0.0")
            return 0.0
        except (aiohttp.ClientError, OSError) as exc:
            logger.debug(f"[ElementMatcher] ONNX service unreachable: {exc}")
            return 0.0
        except Exception as exc:
            logger.warning(f"[ElementMatcher] ONNX unexpected error: {exc}")
            return 0.0

    # ── TEXT HELPERS ───────────────────────────────────────────────────────

    @staticmethod
    def _concatenateTextSignals(el: DOMElement) -> str:
        """
        Builds a single text blob from all relevant textual attributes
        of the DOMElement. Used by both Lexical and Semantic dimensions.
        """
        signals = [
            el.text        or "",
            el.ariaLabel   or "",
            el.placeholder or "",
            el.title       or "",
            el.name        or "",
        ]
        return " ".join(s for s in signals if s).strip()

    # ── UTILITIES ─────────────────────────────────────────────────────────

    @staticmethod
    def _isAutoGeneratedId(elementId: str) -> bool:
        """
        Detects auto-generated IDs that are not stable across page renders.
        Frameworks like React, Radix, HeadlessUI generate IDs like:
        ":r0:", "radix-:r12:", "headlessui-listbox-1744".
        """
        unstablePatterns = [
            r"^:r\d+:$",               # React auto-id
            r"^headlessui-",            # HeadlessUI
            r"^radix-",                 # Radix UI
            r"^react-",                 # Create React App
            r"^\d+$",                   # Pure numeric (dynamic)
            r"^[a-f0-9]{8,}$",         # UUID-like hash
        ]
        for pattern in unstablePatterns:
            if re.match(pattern, elementId, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _resolveQualifier(qualifier: Optional[str], count: int) -> int:
        """
        Converts a qualifier string to an index into the sorted candidate list.
        """
        if qualifier is None or qualifier == "first":
            return 0
        if qualifier == "last":
            return max(0, count - 1)
        if qualifier == "second" and count >= 2:
            return 1
        if qualifier == "third" and count >= 3:
            return 2
        if qualifier and qualifier.startswith("nth:"):
            try:
                n = int(qualifier.split(":")[1]) - 1
                return max(0, min(n, count - 1))
            except (ValueError, IndexError):
                return 0
        return 0
