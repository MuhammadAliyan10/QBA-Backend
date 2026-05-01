"""
selector/ - SmartFinder Selector Engine Module

This module contains the hybrid selector engine for resilient element finding:
- smartFinder.py: 4-layer fallback engine (Reflex, Heuristic, Semantic, Cognitive)
- utils/: Mathematical utilities (SimHash, Levenshtein, hybrid scoring)

The SmartFinder replaces brittle CSS selectors with intent-based element finding,
enabling automation that survives UI changes through self-healing.
"""

from core.selector.smart_finder import (
    SmartFinder,
    FindResult,
    FinderLayer,
    ElementCandidate,
    # Production services
    QdrantVectorDB,
    LLMAgent,
    # Backwards compatibility aliases
    MockVectorDB,
    MockAIAgent,
    find_element,
)

from core.selector.utils import (
    compute_simhash,
    simhash_distance,
    simhash_similarity,
    levenshtein_distance,
    levenshtein_ratio,
    ngram_similarity,
    word_overlap_ratio,
    hybrid_similarity,
    normalize_text,
    is_dynamic_class,
    filter_stable_classes,
)

__all__ = [
    # SmartFinder
    "SmartFinder",
    "FindResult",
    "FinderLayer",
    "ElementCandidate",
    # Services
    "QdrantVectorDB",
    "LLMAgent",
    "MockVectorDB",
    "MockAIAgent",
    "find_element",
    # Utils
    "compute_simhash",
    "simhash_distance",
    "simhash_similarity",
    "levenshtein_distance",
    "levenshtein_ratio",
    "ngram_similarity",
    "word_overlap_ratio",
    "hybrid_similarity",
    "normalize_text",
    "is_dynamic_class",
    "filter_stable_classes",
]
