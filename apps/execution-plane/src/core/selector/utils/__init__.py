"""
__init__.py for utils module
"""

from core.selector.utils.mathUtils import (
    compute_simhash,
    simhash_distance,
    simhash_similarity,
    levenshtein_distance,
    levenshtein_ratio,
    ngram_similarity,
    word_overlap_ratio,
    hybrid_similarity,
    normalize_text,
    compute_element_signature,
    # Audit fix exports
    is_dynamic_class,
    filter_stable_classes
)

__all__ = [
    "compute_simhash",
    "simhash_distance",
    "simhash_similarity",
    "levenshtein_distance",
    "levenshtein_ratio",
    "ngram_similarity",
    "word_overlap_ratio",
    "hybrid_similarity",
    "normalize_text",
    "compute_element_signature",
    "is_dynamic_class",
    "filter_stable_classes"
]

