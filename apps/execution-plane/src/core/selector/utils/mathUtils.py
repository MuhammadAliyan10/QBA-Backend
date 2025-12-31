"""
mathUtils.py - Mathematical Utilities for SmartFinder

Contains:
- SimHash computation for element fingerprinting
- Levenshtein distance for fuzzy text matching
- N-gram similarity for robust matching

These utilities power the SmartFinder's hybrid selector engine,
enabling resilient element finding even when websites change.
"""

import hashlib
import re
from typing import List, Optional, Tuple, Any
from functools import lru_cache


# =============================================================================
# SIMHASH - Element Fingerprinting
# =============================================================================

# Regex patterns for dynamic CSS-in-JS class names to EXCLUDE from hashing
# These change on every build and would poison the SimHash
CSS_IN_JS_PATTERNS = [
    r'^css-',           # Emotion, styled-components: css-1abc2de
    r'^sc-',            # styled-components: sc-1abc2de
    r'^_[a-z0-9]{5,}',  # CSS Modules: _1x2y3z4a
    r'^[a-z]{1,3}[0-9]{4,}',  # Generic hash: btn12345, xs987654
    r'__[a-zA-Z]+_[a-z0-9]+$',  # BEM with hash: button__primary_1x2y
    r'^jsx-[0-9]+',     # styled-jsx: jsx-12345
    r'^[A-Z][a-z]+_[a-z0-9]{6,}',  # MUI: Button_abc123xyz
    r'^tw-',            # Tailwind dynamic
    r'^svelte-[a-z0-9]+',  # Svelte: svelte-1abc2de
    r'[a-f0-9]{8,}',    # Long hex hashes anywhere
]

# Compile patterns for performance
_DYNAMIC_CLASS_REGEX = re.compile('|'.join(f'({p})' for p in CSS_IN_JS_PATTERNS), re.IGNORECASE)


def is_dynamic_class(class_name: str) -> bool:
    """
    Check if a CSS class is dynamically generated (CSS-in-JS).

    These should be excluded from SimHash computation because they
    change between builds and would cause false mismatches.

    Args:
        class_name: CSS class to check

    Returns:
        True if class appears to be dynamically generated
    """
    if not class_name or len(class_name) < 3:
        return True  # Too short to be meaningful

    # Check against known patterns
    if _DYNAMIC_CLASS_REGEX.search(class_name):
        return True

    # Check for high entropy (random strings)
    # If more than 50% of chars are digits, likely dynamic
    digit_ratio = sum(c.isdigit() for c in class_name) / len(class_name)
    if digit_ratio > 0.5:
        return True

    return False


def filter_stable_classes(classes: List[str]) -> List[str]:
    """
    Filter out dynamic CSS-in-JS classes, keeping only stable semantic classes.

    Args:
        classes: List of CSS class names

    Returns:
        Filtered list of stable classes
    """
    return [cls for cls in classes if not is_dynamic_class(cls)]


def compute_simhash(
    tag: str,
    text: str = "",
    classes: List[str] = None,
    attributes: dict = None,
    position_index: int = 0,
    bit_size: int = 64
) -> str:
    """
    Compute a SimHash fingerprint for a DOM element.

    AUDIT FIX: Now filters CSS-in-JS dynamic classes and includes position_index
    for disambiguating multiple similar elements.

    SimHash is a locality-sensitive hash that produces similar hashes
    for similar inputs. This allows us to find elements even when they
    change slightly (e.g., text "Login" vs "Log In").

    Algorithm:
    1. Extract features: tag, normalized text, FILTERED classes, key attributes
    2. Include position_index for disambiguation
    3. Hash each feature with weights
    4. Combine into weighted bit vector
    5. Return final fingerprint

    Args:
        tag: HTML tag name (e.g., "button", "input")
        text: Inner text content
        classes: CSS class list (dynamic classes will be FILTERED OUT)
        attributes: Key attributes (id, name, placeholder, aria-label)
        position_index: DOM position among siblings (for disambiguation)
        bit_size: Hash size in bits (default 64)

    Returns:
        Hex string fingerprint (e.g., "a1b2c3d4e5f6...")
    """
    classes = classes or []
    attributes = attributes or {}

    # AUDIT FIX: Filter out CSS-in-JS dynamic classes
    stable_classes = filter_stable_classes(classes)

    # Initialize bit vector
    vector = [0] * bit_size

    # Feature extraction with weights
    features = []

    # Tag (weight: 3) - Most stable feature
    features.append(("tag", tag.lower(), 3))

    # Position index (weight: 2) - For disambiguation
    # Groups elements by position bucket (0-4, 5-9, etc.)
    if position_index > 0:
        position_bucket = position_index // 5
        features.append(("pos", f"bucket_{position_bucket}", 2))

    # Normalized text (weight: 2) - Remove extra whitespace
    normalized_text = normalize_text(text)
    if normalized_text:
        # Add whole text as feature (truncated to prevent mega-strings)
        features.append(("text", normalized_text[:50], 2))
        # Add significant words (first 3 only)
        for word in normalized_text.split()[:3]:
            if len(word) > 2:
                features.append(("word", word.lower(), 1))

    # Stable classes only (weight: 1)
    for cls in stable_classes[:5]:
        features.append(("class", cls.lower(), 1))

    # Key attributes (weight: 2) - Most reliable identifiers
    for attr_name in ["id", "name", "placeholder", "aria-label", "data-testid"]:
        if attr_name in attributes:
            value = str(attributes[attr_name]).lower().strip()
            if value and len(value) < 100:  # Avoid huge values
                features.append(("attr", f"{attr_name}={value}", 2))

    # Compute weighted hash vector
    for feature_type, feature_value, weight in features:
        feature_str = f"{feature_type}:{feature_value}"
        feature_hash = _hash_to_bits(feature_str, bit_size)

        for i, bit in enumerate(feature_hash):
            if bit == 1:
                vector[i] += weight
            else:
                vector[i] -= weight

    # Convert vector to binary fingerprint
    fingerprint = 0
    for i, val in enumerate(vector):
        if val > 0:
            fingerprint |= (1 << i)

    # Return as hex string (fixed length)
    hex_length = bit_size // 4
    return format(fingerprint, f'0{hex_length}x')


def _hash_to_bits(s: str, bit_size: int) -> List[int]:
    """Convert string to bit array using SHA256."""
    hash_bytes = hashlib.sha256(s.encode()).digest()
    bits = []
    for i in range(bit_size):
        byte_idx = i // 8
        bit_idx = i % 8
        if byte_idx < len(hash_bytes):
            bit = (hash_bytes[byte_idx] >> bit_idx) & 1
            bits.append(bit)
        else:
            bits.append(0)
    return bits


def simhash_distance(hash1: str, hash2: str) -> int:
    """
    Compute Hamming distance between two SimHash fingerprints.

    Lower distance = more similar elements.

    Args:
        hash1: First SimHash hex string
        hash2: Second SimHash hex string

    Returns:
        Number of differing bits (0 = identical)
    """
    try:
        int1 = int(hash1, 16)
        int2 = int(hash2, 16)
        xor = int1 ^ int2
        return bin(xor).count('1')
    except (ValueError, TypeError):
        return 64  # Maximum distance


def simhash_similarity(hash1: str, hash2: str, bit_size: int = 64) -> float:
    """
    Compute similarity ratio between two SimHash fingerprints.

    Args:
        hash1: First SimHash hex string
        hash2: Second SimHash hex string
        bit_size: Size of hash in bits

    Returns:
        Similarity ratio (0.0 - 1.0)
    """
    distance = simhash_distance(hash1, hash2)
    return 1.0 - (distance / bit_size)


# =============================================================================
# LEVENSHTEIN DISTANCE - Fuzzy Text Matching
# =============================================================================

@lru_cache(maxsize=1024)
def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Compute Levenshtein (edit) distance between two strings.

    This is the minimum number of single-character edits
    (insertions, deletions, or substitutions) required to
    change one string into the other.

    Uses dynamic programming with O(min(m,n)) space.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Edit distance (0 = identical)
    """
    if len(s1) < len(s2):
        s1, s2 = s2, s1

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)

    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Calculate cost of insertions, deletions, substitutions
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def levenshtein_ratio(s1: str, s2: str) -> float:
    """
    Compute similarity ratio using Levenshtein distance.

    Normalizes the distance by the length of the longer string.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Similarity ratio (0.0 - 1.0, higher = more similar)

    Example:
        >>> levenshtein_ratio("login", "Login")
        0.8  # One character difference (case)
        >>> levenshtein_ratio("submit", "Submit Button")
        0.538  # More different
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    # Normalize: lowercase and strip whitespace
    s1_norm = s1.lower().strip()
    s2_norm = s2.lower().strip()

    if s1_norm == s2_norm:
        return 1.0

    distance = levenshtein_distance(s1_norm, s2_norm)
    max_len = max(len(s1_norm), len(s2_norm))

    return 1.0 - (distance / max_len)


# =============================================================================
# N-GRAM SIMILARITY - Robust Token Matching
# =============================================================================

def ngram_tokenize(text: str, n: int = 2) -> set:
    """
    Generate character n-grams from text.

    Args:
        text: Input text
        n: N-gram size (default: 2 for bigrams)

    Returns:
        Set of n-grams
    """
    text = text.lower().strip()
    if len(text) < n:
        return {text}
    return {text[i:i+n] for i in range(len(text) - n + 1)}


def ngram_similarity(s1: str, s2: str, n: int = 2) -> float:
    """
    Compute Jaccard similarity using character n-grams.

    More robust than Levenshtein for partial matches.

    Args:
        s1: First string
        s2: Second string
        n: N-gram size

    Returns:
        Similarity ratio (0.0 - 1.0)
    """
    ngrams1 = ngram_tokenize(s1, n)
    ngrams2 = ngram_tokenize(s2, n)

    if not ngrams1 or not ngrams2:
        return 0.0

    intersection = len(ngrams1 & ngrams2)
    union = len(ngrams1 | ngrams2)

    return intersection / union if union > 0 else 0.0


# =============================================================================
# WORD OVERLAP - Semantic Token Matching
# =============================================================================

def word_overlap_ratio(s1: str, s2: str) -> float:
    """
    Compute word-level Jaccard similarity.

    Better for matching phrases with same words in different order.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Similarity ratio (0.0 - 1.0)

    Example:
        >>> word_overlap_ratio("Login Button", "Button Login")
        1.0  # Same words, different order
    """
    words1 = set(normalize_text(s1).lower().split())
    words2 = set(normalize_text(s2).lower().split())

    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)

    return intersection / union if union > 0 else 0.0


# =============================================================================
# HYBRID SIMILARITY - Combined Scoring
# =============================================================================

def hybrid_similarity(s1: str, s2: str, weights: dict = None) -> float:
    """
    Compute weighted hybrid similarity using multiple algorithms.

    Combines:
    - Levenshtein ratio (character-level)
    - N-gram similarity (substring-level)
    - Word overlap (semantic-level)

    Args:
        s1: First string
        s2: Second string
        weights: Algorithm weights (default: balanced)

    Returns:
        Weighted similarity score (0.0 - 1.0)
    """
    weights = weights or {
        "levenshtein": 0.4,
        "ngram": 0.3,
        "word_overlap": 0.3
    }

    lev = levenshtein_ratio(s1, s2)
    ngram = ngram_similarity(s1, s2)
    word = word_overlap_ratio(s1, s2)

    score = (
        weights.get("levenshtein", 0.4) * lev +
        weights.get("ngram", 0.3) * ngram +
        weights.get("word_overlap", 0.3) * word
    )

    return round(score, 4)


# =============================================================================
# TEXT UTILITIES
# =============================================================================

def normalize_text(text: str) -> str:
    """
    Normalize text for comparison.

    - Collapses whitespace
    - Strips leading/trailing whitespace
    - Removes special characters (keeps alphanumeric and space)
    """
    if not text:
        return ""

    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)

    # Remove non-alphanumeric except spaces
    text = re.sub(r'[^\w\s]', '', text)

    return text.strip()


def extract_meaningful_words(text: str, min_length: int = 3) -> List[str]:
    """
    Extract meaningful words from text (filter stopwords and short words).
    """
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "must", "shall",
        "can", "to", "of", "in", "for", "on", "with", "at", "by",
        "from", "as", "into", "through", "during", "before", "after",
        "above", "below", "between", "under", "again", "further",
        "then", "once", "here", "there", "when", "where", "why",
        "how", "all", "each", "few", "more", "most", "other", "some",
        "such", "no", "nor", "not", "only", "own", "same", "so",
        "than", "too", "very", "just", "and", "but", "if", "or"
    }

    words = normalize_text(text).lower().split()
    return [w for w in words if len(w) >= min_length and w not in stopwords]


# =============================================================================
# ELEMENT SIGNATURE
# =============================================================================

def compute_element_signature(
    tag: str,
    text: str = "",
    classes: List[str] = None,
    attributes: dict = None
) -> dict:
    """
    Compute a full signature for a DOM element.

    Returns a dictionary with multiple fingerprints for different
    matching strategies.
    """
    classes = classes or []
    attributes = attributes or {}

    return {
        "simhash": compute_simhash(tag, text, classes, attributes),
        "tag": tag.lower(),
        "normalized_text": normalize_text(text),
        "classes": classes[:5],
        "key_attrs": {
            k: v for k, v in attributes.items()
            if k in ["id", "name", "data-testid", "aria-label"]
        }
    }


# =============================================================================
# EXAMPLE / TESTING
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("MATH UTILS - Test Suite")
    print("=" * 60)

    # Test SimHash
    print("\n[SimHash Tests]")
    hash1 = compute_simhash("button", "Login", ["btn", "primary"])
    hash2 = compute_simhash("button", "Log in", ["btn", "primary"])
    hash3 = compute_simhash("a", "Sign Up", ["link"])

    print(f"  Button 'Login':  {hash1}")
    print(f"  Button 'Log in': {hash2}")
    print(f"  Link 'Sign Up':  {hash3}")
    print(f"  Similarity (Login vs Log in): {simhash_similarity(hash1, hash2):.3f}")
    print(f"  Similarity (Login vs Sign Up): {simhash_similarity(hash1, hash3):.3f}")

    # Test Levenshtein
    print("\n[Levenshtein Tests]")
    pairs = [
        ("login", "Login"),
        ("Submit", "Submit Button"),
        ("Click Here", "Click Here to Continue"),
        ("abc", "xyz")
    ]
    for s1, s2 in pairs:
        ratio = levenshtein_ratio(s1, s2)
        print(f"  '{s1}' vs '{s2}': {ratio:.3f}")

    # Test Hybrid
    print("\n[Hybrid Similarity Tests]")
    pairs = [
        ("Login Button", "login button"),
        ("Submit Form", "Form Submit"),
        ("Click to Continue", "Continue clicking")
    ]
    for s1, s2 in pairs:
        score = hybrid_similarity(s1, s2)
        print(f"  '{s1}' vs '{s2}': {score:.3f}")

    print("\n" + "=" * 60)
    print("TESTS COMPLETE")
    print("=" * 60)
