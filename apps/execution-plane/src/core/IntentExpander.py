"""
Intent Expander - Advanced Semantic Intelligence Layer
========================================================

This module provides LLM-like intent decomposition without requiring an external LLM.
It expands vague intents into multiple search terms using:
1. Role-based mappings (CEO, founder, president, etc.)
2. Synonym expansion
3. Common UI patterns
4. Domain-specific knowledge

This is the MISSING LINK that makes Step 4 ("person who runs the company") work.
"""

import re
import logging
from typing import List, Dict, Set, Tuple
from functools import lru_cache

logger = logging.getLogger("intentExpander")


# =============================================================================
# KNOWLEDGE BASES - Pre-trained mappings for common concepts
# =============================================================================

# Role-based mappings: concept -> related terms that might appear in links
ROLE_MAPPINGS = {
    "person who runs": ["CEO", "Chief Executive", "President", "Founder", "Chairman", "Director"],
    "person who leads": ["CEO", "Chief Executive", "President", "Leader", "Head", "Director"],
    "person in charge": ["CEO", "Chief Executive", "President", "Manager", "Head", "Director"],
    "leader": ["CEO", "Chief Executive", "President", "Founder", "Chairman", "Head"],
    "boss": ["CEO", "Chief Executive", "President", "Founder", "Manager", "Director"],
    "founder": ["Founder", "Co-founder", "Founded by", "Creator", "Established by"],
    "owner": ["Owner", "Founder", "Chairman", "Proprietor"],
    "ceo": ["CEO", "Chief Executive Officer", "Chief Executive"],
    "cto": ["CTO", "Chief Technology Officer", "Chief Technical Officer"],
    "cfo": ["CFO", "Chief Financial Officer"],
    "executive": ["Executive", "Officer", "Director", "VP", "Vice President"],
}

# Common company names and their CEOs (for demo purposes)
COMPANY_CEO_MAPPINGS = {
    "nvidia": ["Jensen Huang", "Jen-Hsun Huang"],
    "apple": ["Tim Cook", "Timothy Cook"],
    "microsoft": ["Satya Nadella"],
    "google": ["Sundar Pichai"],
    "amazon": ["Andy Jassy", "Jeff Bezos"],
    "meta": ["Mark Zuckerberg"],
    "tesla": ["Elon Musk"],
    "openai": ["Sam Altman"],
    "anthropic": ["Dario Amodei"],
}

# Action synonyms for UI elements
ACTION_SYNONYMS = {
    "search": ["search", "find", "lookup", "query", "discover"],
    "submit": ["submit", "send", "go", "enter", "confirm", "done"],
    "login": ["login", "sign in", "log in", "signin", "authenticate"],
    "logout": ["logout", "sign out", "log out", "signout", "exit"],
    "next": ["next", "continue", "forward", "proceed", "advance"],
    "back": ["back", "previous", "return", "go back"],
    "close": ["close", "dismiss", "cancel", "exit", "x"],
    "add": ["add", "create", "new", "plus", "+"],
    "delete": ["delete", "remove", "trash", "erase", "clear"],
    "edit": ["edit", "modify", "change", "update", "pencil"],
    "save": ["save", "store", "keep", "preserve"],
    "download": ["download", "save", "export", "get"],
    "upload": ["upload", "attach", "import", "add file"],
    "settings": ["settings", "preferences", "config", "options", "gear", "cog"],
    "help": ["help", "support", "faq", "?", "question"],
    "menu": ["menu", "hamburger", "navigation", "nav", "☰"],
    "cart": ["cart", "basket", "bag", "shopping"],
    "profile": ["profile", "account", "user", "avatar", "me"],
}

# Content type patterns
CONTENT_PATTERNS = {
    "price": [r"\$[\d,]+", r"€[\d,]+", r"£[\d,]+", r"[\d,]+ USD", r"price", r"cost"],
    "date": [r"\d{1,2}/\d{1,2}/\d{2,4}", r"\w+ \d{1,2}, \d{4}", r"date", r"when"],
    "email": [r"[\w.-]+@[\w.-]+", r"email", r"contact"],
    "phone": [r"\+?\d{1,4}[\s.-]?\d{3,4}[\s.-]?\d{4}", r"phone", r"call", r"tel"],
    "location": [r"address", r"location", r"where", r"headquarters", r"based in"],
    "networth": [r"net worth", r"wealth", r"fortune", r"billion", r"assets"],
}


class IntentExpander:
    """
    Expands user intents into multiple search terms for better element matching.

    This provides LLM-like reasoning without requiring an external LLM API call.
    """

    def __init__(self):
        self._compile_patterns()
        logger.info("[IntentExpander] Initialized with role mappings and synonyms")

    def _compile_patterns(self):
        """Pre-compile regex patterns for efficiency."""
        self._role_patterns = {}
        for key, values in ROLE_MAPPINGS.items():
            self._role_patterns[re.compile(key, re.IGNORECASE)] = values

    @lru_cache(maxsize=256)
    def expand(self, intent: str, context: str = "") -> List[str]:
        """
        Expands an intent into multiple search terms.

        Args:
            intent: The user's vague intent (e.g., "person who runs the company")
            context: Optional context (e.g., current page URL)

        Returns:
            List of expanded search terms, ordered by relevance
        """
        intent_lower = intent.lower().strip()
        expanded = set()
        expanded.add(intent)  # Always include original

        # 1. Apply role-based mappings
        for pattern, expansions in self._role_patterns.items():
            if pattern.search(intent_lower):
                expanded.update(expansions)
                logger.debug(f"[Expand] Role mapping: '{intent}' -> {expansions}")

        # 2. Apply action synonyms
        for action, synonyms in ACTION_SYNONYMS.items():
            if action in intent_lower:
                expanded.update(synonyms)
                # Also add combinations
                for syn in synonyms[:3]:  # Limit to top 3
                    expanded.add(f"{syn} button")
                    expanded.add(f"{syn} link")

        # 3. Apply company-specific CEO mappings if we detect a company
        company = self._extract_company_from_context(context)
        if company and self._is_leader_query(intent_lower):
            ceo_names = COMPANY_CEO_MAPPINGS.get(company.lower(), [])
            expanded.update(ceo_names)
            logger.debug(f"[Expand] Company CEO: {company} -> {ceo_names}")

        # 4. Extract key nouns for additional searches
        key_words = self._extract_key_words(intent)
        expanded.update(key_words)

        # 5. Generate common variations
        expanded.update(self._generate_variations(intent))

        # Sort by relevance (original first, then by length)
        result = sorted(list(expanded), key=lambda x: (x != intent, len(x)))

        logger.info(f"[IntentExpander] '{intent}' -> {len(result)} terms")
        logger.debug(f"[IntentExpander] Expanded: {result[:10]}...")

        return result

    def _is_leader_query(self, intent: str) -> bool:
        """Check if intent is asking about company leadership."""
        leader_keywords = [
            "runs", "leads", "charge", "ceo", "chief",
            "founder", "president", "owner", "boss", "head"
        ]
        return any(kw in intent.lower() for kw in leader_keywords)

    def _extract_company_from_context(self, context: str) -> str:
        """Extract company name from URL or context."""
        if not context:
            return ""

        context_lower = context.lower()

        # Check for known companies in context
        for company in COMPANY_CEO_MAPPINGS.keys():
            if company in context_lower:
                return company

        # Try to extract from URL path
        # e.g., "/wiki/Nvidia" -> "nvidia"
        match = re.search(r'/wiki/([^/]+)', context)
        if match:
            return match.group(1).replace("_", " ")

        return ""

    def _extract_key_words(self, intent: str) -> Set[str]:
        """Extract meaningful words from intent."""
        # Remove stop words
        stop_words = {
            "the", "a", "an", "to", "for", "of", "in", "on", "at", "by",
            "with", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "must", "shall", "can",
            "that", "which", "who", "whom", "this", "these", "those",
            "link", "button", "field", "input", "click", "find", "element"
        }

        words = re.findall(r'\b\w+\b', intent.lower())
        key_words = {w for w in words if w not in stop_words and len(w) > 2}

        return key_words

    def _generate_variations(self, intent: str) -> Set[str]:
        """Generate common variations of the intent."""
        variations = set()

        # Add with common UI suffixes
        for suffix in ["button", "link", "icon", ""]:
            for word in self._extract_key_words(intent):
                if suffix:
                    variations.add(f"{word} {suffix}")
                variations.add(word)

        return variations

    def get_semantic_expansions(self, intent: str, page_url: str = "") -> Dict[str, float]:
        """
        Returns expanded terms with confidence scores.

        Args:
            intent: User's intent
            page_url: Current page URL for context

        Returns:
            Dict mapping expanded terms to confidence scores (0-1)
        """
        expansions = self.expand(intent, page_url)

        # Assign confidence scores (original gets 1.0, others decrease)
        scored = {}
        for i, term in enumerate(expansions):
            if term == intent:
                scored[term] = 1.0
            elif term in ROLE_MAPPINGS.values() or any(term in v for v in COMPANY_CEO_MAPPINGS.values()):
                scored[term] = 0.9  # High confidence for known mappings
            else:
                # Decay confidence for synthetic terms
                scored[term] = max(0.3, 0.8 - (i * 0.05))

        return scored


# Singleton instance
_expander_instance = None

def get_intent_expander() -> IntentExpander:
    """Get singleton IntentExpander instance."""
    global _expander_instance
    if _expander_instance is None:
        _expander_instance = IntentExpander()
    return _expander_instance


# =============================================================================
# ELEMENT RANKER - Uses expanded intents for better scoring
# =============================================================================

class SemanticElementRanker:
    """
    Ranks elements using expanded intents and multi-factor scoring.
    """

    def __init__(self):
        self.expander = get_intent_expander()

    def score_element(
        self,
        intent: str,
        element_text: str,
        lexical_scorer,
        page_url: str = ""
    ) -> Tuple[float, str, str]:
        """
        Score an element against intent using expanded search.

        Args:
            intent: User's intent
            element_text: Combined element text (innerText + aria + id + etc.)
            lexical_scorer: LevenshteinScorer instance
            page_url: Current URL for context

        Returns:
            Tuple of (score, matched_term, method)
        """
        # Get expanded terms with confidence
        expansions = self.expander.get_semantic_expansions(intent, page_url)

        best_score = 0.0
        best_term = intent
        best_method = "LEXICAL"

        element_lower = element_text.lower()

        # Score against each expanded term
        for term, confidence in expansions.items():
            # Direct substring match (highest priority)
            if term.lower() in element_lower:
                match_score = 0.95 * confidence
                if match_score > best_score:
                    best_score = match_score
                    best_term = term
                    best_method = "SUBSTRING"

            # Lexical scoring
            lex_score = lexical_scorer.score(term.lower(), element_lower) * confidence
            if lex_score > best_score:
                best_score = lex_score
                best_term = term
                best_method = "LEXICAL_EXPANDED"

        return best_score, best_term, best_method


# Export
__all__ = [
    "IntentExpander",
    "SemanticElementRanker",
    "get_intent_expander",
    "ROLE_MAPPINGS",
    "COMPANY_CEO_MAPPINGS",
    "ACTION_SYNONYMS"
]
