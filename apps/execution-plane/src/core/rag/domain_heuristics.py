# src/core/rag/domain_heuristics.py
"""
Static Domain Feasibility Heuristics (Fix for 2.2 — Zero LLM)

Problem being solved:
  The Preflight Oracle calls the LLM to check feasibility for EVERY request.
  Many feasibility decisions are deterministic and require no AI reasoning.
  Calling LLM for "can I scrape Google search results?" wastes tokens and
  adds 1-3 seconds of latency.

Solution — Two-layer feasibility:
  1. STATIC (this module): domain category mapping + rule engine.
     Zero LLM. Zero latency. Runs synchronously.
     Handles 80%+ of requests deterministically.

  2. DYNAMIC (preflight.py Oracle): LLM reasoning for ambiguous cases.
     Only reached if static layer returns UNKNOWN.

Feasibility categories:
  - ALLOWED:    Request is clearly feasible, skip Oracle check entirely
  - BLOCKED:    Request is structurally impossible (e.g., scrape YouTube DMs)
  - BYOS_REQUIRED: Possible only with authenticated session
  - UNKNOWN:    Cannot determine statically, escalate to Oracle LLM

Maintenance:
  Add new domains or rules to DOMAIN_REGISTRY or INTENT_BLOCK_PATTERNS.
  No code changes needed — this is a pure data configuration.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger("domain_heuristics")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class FeasibilityVerdict(str, Enum):
    ALLOWED       = "allowed"
    BLOCKED       = "blocked"
    BYOS_REQUIRED = "byos_required"
    UNKNOWN       = "unknown"


@dataclass
class StaticFeasibilityResult:
    verdict: FeasibilityVerdict
    reason: str
    category: str = "unknown"
    requires_auth: bool = False
    is_dynamic: bool = False     # Dynamic site (SPA/XHR-heavy) — flag for sniffer
    is_api_friendly: bool = False  # Site exposes public JSON APIs


# ---------------------------------------------------------------------------
# Domain Registry — curated knowledge about common scraping targets
# ---------------------------------------------------------------------------
# Structure:
#   domain: {
#     "category": str,
#     "public_scraping": bool,     # Can be scraped without auth
#     "auth_required": bool,       # Requires session for data access
#     "is_dynamic": bool,          # SPA / heavy XHR
#     "is_api_friendly": bool,     # Exposes public JSON APIs
#     "blocked_intents": list[str] # Intent keyword substrings that are impossible
#   }

DOMAIN_REGISTRY: Dict[str, Dict] = {
    # --- E-Commerce ---
    "amazon.com":       {"category": "ecommerce", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": ["private messages", "order history", "wallet", "saved payment"]},
    "ebay.com":         {"category": "ecommerce", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["private messages", "bids history"]},
    "shopify.com":      {"category": "ecommerce", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["admin", "orders", "customers"]},
    "etsy.com":         {"category": "ecommerce", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": []},
    "aliexpress.com":   {"category": "ecommerce", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": []},
    "daraz.pk":         {"category": "ecommerce", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": []},
    "noon.com":         {"category": "ecommerce", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": []},
    # --- Social Media ---
    "linkedin.com":     {"category": "social",    "public_scraping": False, "auth_required": True,  "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": ["private messages", "connection requests", "send message", "personal email", "phone number"]},
    "twitter.com":      {"category": "social",    "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["direct messages", "private tweets", "dm", "drafts"]},
    "x.com":            {"category": "social",    "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["direct messages", "private tweets", "dm", "drafts"]},
    "instagram.com":    {"category": "social",    "public_scraping": False, "auth_required": True,  "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": ["direct messages", "private account", "stories of private"]},
    "facebook.com":     {"category": "social",    "public_scraping": False, "auth_required": True,  "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": ["private messages", "messenger", "personal info", "phone number"]},
    "reddit.com":       {"category": "social",    "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["private messages", "mod mail"]},
    "tiktok.com":       {"category": "social",    "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": ["private messages", "private account"]},
    # --- Professional / Job ---
    "glassdoor.com":    {"category": "jobs",      "public_scraping": False, "auth_required": True,  "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": []},
    "indeed.com":       {"category": "jobs",      "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": []},
    "upwork.com":       {"category": "jobs",      "public_scraping": False, "auth_required": True,  "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": []},
    "fiverr.com":       {"category": "jobs",      "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": []},
    # --- Search / Media ---
    "google.com":       {"category": "search",    "public_scraping": True,  "auth_required": False, "is_dynamic": False, "is_api_friendly": False, "blocked_intents": ["gmail", "private emails", "drive files", "calendar", "contacts", "personal data"]},
    "youtube.com":      {"category": "media",     "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["private messages", "studio analytics", "channel password", "private videos of others"]},
    "bing.com":         {"category": "search",    "public_scraping": True,  "auth_required": False, "is_dynamic": False, "is_api_friendly": True,  "blocked_intents": []},
    # --- Finance ---
    "yahoo.com":        {"category": "finance",   "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["private email", "yahoo mail", "personal account"]},
    "bloomberg.com":    {"category": "finance",   "public_scraping": False, "auth_required": True,  "is_dynamic": True,  "is_api_friendly": False, "blocked_intents": []},
    "reuters.com":      {"category": "news",      "public_scraping": True,  "auth_required": False, "is_dynamic": False, "is_api_friendly": False, "blocked_intents": []},
    # --- Tech / Dev ---
    "github.com":       {"category": "developer", "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": ["private repositories", "private gists", "personal tokens", "secrets"]},
    "stackoverflow.com":{"category": "developer", "public_scraping": True,  "auth_required": False, "is_dynamic": False, "is_api_friendly": True,  "blocked_intents": []},
    "producthunt.com":  {"category": "tech",      "public_scraping": True,  "auth_required": False, "is_dynamic": True,  "is_api_friendly": True,  "blocked_intents": []},
}

# TLD-level category fallback (when domain is not in DOMAIN_REGISTRY)
TLD_CATEGORIES: Dict[str, str] = {
    ".gov": "government",
    ".edu": "education",
    ".org": "organization",
    ".ac.uk": "education",
}

# ---------------------------------------------------------------------------
# Intent patterns that are ALWAYS blocked regardless of site
# — Zero-LLM enforcement of universal impossibilities
# ---------------------------------------------------------------------------
GLOBAL_BLOCK_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(password|passwd|credentials?)\s+(of|for|from)\s+\w+", re.IGNORECASE),
    re.compile(r"\bcredit\s+card\s+(numbers?|details?|cvv)\b", re.IGNORECASE),
    re.compile(r"\b(social\s+security|ssn|national\s+id)\s+(number|details?)\b", re.IGNORECASE),
    re.compile(r"\bhack\s+(into|account|server|database)\b", re.IGNORECASE),
    re.compile(r"\b(bypass|circumvent|crack)\s+(authentication|login|security|firewall)\b", re.IGNORECASE),
    re.compile(r"\bscrape\s+.{0,40}\bprivate\s+(messages?|chats?|dms?)\b", re.IGNORECASE),
    re.compile(r"\bextract\s+.{0,30}\bpersonal\s+(emails?|phone\s+numbers?|addresses?)\b", re.IGNORECASE),
]

# Intent substrings that are NEVER feasible on any site
UNIVERSAL_BLOCK_SUBSTRINGS: FrozenSet[str] = frozenset({
    "2fa code", "otp code", "authentication code from",
    "bypass captcha for me",
    "private keys", "seed phrase", "wallet private",
})


# ---------------------------------------------------------------------------
# Main function — single entrypoint
# ---------------------------------------------------------------------------

def evaluate_feasibility(url: str, intent: str) -> StaticFeasibilityResult:
    """
    Perform zero-LLM static feasibility analysis.

    Args:
        url:    The target URL provided by the user
        intent: The natural-language prompt / navigation objective

    Returns:
        StaticFeasibilityResult with a verdict and reason string.

    This function never raises. All errors produce verdict=UNKNOWN
    so the Oracle LLM can make the final call.
    """
    try:
        return _evaluate(url, intent)
    except Exception as exc:
        logger.warning(f"[DomainHeuristics] Evaluation error (non-fatal): {exc}")
        return StaticFeasibilityResult(
            verdict=FeasibilityVerdict.UNKNOWN,
            reason=f"Static evaluation failed: {exc}",
        )


def _evaluate(url: str, intent: str) -> StaticFeasibilityResult:
    """Core evaluation logic."""
    intent_lower = intent.lower().strip()

    # -----------------------------------------------------------------------
    # Gate 1: Global intent block patterns (universal impossibilities)
    # -----------------------------------------------------------------------
    for pattern in GLOBAL_BLOCK_PATTERNS:
        if pattern.search(intent_lower):
            logger.info(f"[DomainHeuristics] BLOCKED by global pattern: {pattern.pattern[:60]}")
            return StaticFeasibilityResult(
                verdict=FeasibilityVerdict.BLOCKED,
                reason=(
                    f"Intent matches universally blocked pattern: {pattern.pattern[:80]}. "
                    "Quanta does not support extraction of private credentials, "
                    "financial instruments, or personal identifying information."
                ),
            )

    for substring in UNIVERSAL_BLOCK_SUBSTRINGS:
        if substring in intent_lower:
            logger.info(f"[DomainHeuristics] BLOCKED by universal substring: {substring}")
            return StaticFeasibilityResult(
                verdict=FeasibilityVerdict.BLOCKED,
                reason=f"Intent contains universally blocked phrase: '{substring}'.",
            )

    # -----------------------------------------------------------------------
    # Gate 2: Parse domain from URL
    # -----------------------------------------------------------------------
    domain = _extract_domain(url)
    if not domain:
        return StaticFeasibilityResult(
            verdict=FeasibilityVerdict.UNKNOWN,
            reason="Could not parse domain from URL.",
        )

    # -----------------------------------------------------------------------
    # Gate 3: Domain registry lookup (exact + subdomain fallback)
    # -----------------------------------------------------------------------
    registry_entry = _lookup_domain(domain)

    if registry_entry is not None:
        # Domain is known — apply domain-specific rules
        category = registry_entry["category"]
        is_dynamic = registry_entry.get("is_dynamic", False)
        is_api_friendly = registry_entry.get("is_api_friendly", False)

        # Check domain-specific blocked intent substrings
        for blocked_intent in registry_entry.get("blocked_intents", []):
            if blocked_intent.lower() in intent_lower:
                logger.info(
                    f"[DomainHeuristics] BLOCKED: '{blocked_intent}' is not "
                    f"accessible on {domain}"
                )
                return StaticFeasibilityResult(
                    verdict=FeasibilityVerdict.BLOCKED,
                    reason=(
                        f"The intent '{blocked_intent}' cannot be fulfilled on "
                        f"{domain} — this data is private, protected, or not "
                        "accessible via browser automation."
                    ),
                    category=category,
                    requires_auth=registry_entry.get("auth_required", False),
                    is_dynamic=is_dynamic,
                    is_api_friendly=is_api_friendly,
                )

        # Auth-gated sites
        if registry_entry.get("auth_required", False):
            return StaticFeasibilityResult(
                verdict=FeasibilityVerdict.BYOS_REQUIRED,
                reason=(
                    f"{domain} requires authentication. "
                    "Provide a vaulted BYOS session to proceed."
                ),
                category=category,
                requires_auth=True,
                is_dynamic=is_dynamic,
                is_api_friendly=is_api_friendly,
            )

        # Public site, request is feasible
        return StaticFeasibilityResult(
            verdict=FeasibilityVerdict.ALLOWED,
            reason=f"Domain {domain} is a known public {category} site.",
            category=category,
            requires_auth=False,
            is_dynamic=is_dynamic,
            is_api_friendly=is_api_friendly,
        )

    # -----------------------------------------------------------------------
    # Gate 4: TLD-level fallback category
    # -----------------------------------------------------------------------
    for tld, tld_category in TLD_CATEGORIES.items():
        if domain.endswith(tld):
            return StaticFeasibilityResult(
                verdict=FeasibilityVerdict.UNKNOWN,
                reason=f"Domain {domain} is in TLD category '{tld_category}'. Escalating to Oracle.",
                category=tld_category,
            )

    # -----------------------------------------------------------------------
    # Gate 5: Unknown domain — escalate to Oracle LLM
    # -----------------------------------------------------------------------
    return StaticFeasibilityResult(
        verdict=FeasibilityVerdict.UNKNOWN,
        reason=f"Domain {domain} is not in the heuristic registry. Escalating to Oracle LLM.",
        category="unknown",
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> Optional[str]:
    """Safely extract and normalize base domain from URL."""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        # Strip port
        netloc = netloc.split(":")[0]
        # Strip www
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc or None
    except Exception:
        return None


def _lookup_domain(domain: str) -> Optional[Dict]:
    """
    Look up domain in registry.
    1. Try exact match first (amazon.com → amazon.com)
    2. Try base domain extraction for subdomains (shop.amazon.com → amazon.com)
    """
    # Exact match
    if domain in DOMAIN_REGISTRY:
        return DOMAIN_REGISTRY[domain]

    # Subdomain fallback: strip one level and retry
    parts = domain.split(".")
    if len(parts) > 2:
        base_domain = ".".join(parts[-2:])
        if base_domain in DOMAIN_REGISTRY:
            return DOMAIN_REGISTRY[base_domain]

    return None
