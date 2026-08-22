# src/core/extraction/schema_inferencer.py
"""
SchemaInferencer — Automatic Field Inference for No-Schema Mode (Phase 4)

Problem being solved:
  Currently, if a user doesn't provide an `extraction_schema`, the agent
  returns only {page_title, url}. Useless for 90% of real requests like:
    "scrape the top 10 products from amazon.com"
  — no schema is provided, but the expected output is obvious.

Solution:
  When extraction_schema is None or empty, infer a schema from:
    1. The navigation objective (text analysis for likely fields)
    2. A compact DOM preview (LLM identifies what structured data is present)

  The inferred schema is used EXACTLY like a user-provided schema for the
  rest of the extraction pipeline (including SelectorSynthesizer).

Two-stage inference:
  Stage 1 — Heuristic: keyword matching on the objective to propose fields.
    "get products" → {name, price, rating, url}
    "find jobs"    → {title, company, location, salary, url}
    No LLM, instant.

  Stage 2 — LLM refinement: if heuristic stage produces > 0 fields, skip.
    If heuristic produces 0 fields (ambiguous objective), call LLM with
    DOM preview to identify what structured data is actually on the page.

The inferred schema is always returned as a list[dict] (multi-item mode)
since schema inference is only triggered for extraction-style requests.
"""

from __future__ import annotations

import logging
import json
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("schema_inferencer")


# ---------------------------------------------------------------------------
# Stage 1 — Keyword-to-schema heuristic map
# ---------------------------------------------------------------------------

_INTENT_SCHEMA_MAP: List[tuple[frozenset[str], Dict[str, str]]] = [
    # E-commerce products
    (frozenset({"product", "products", "item", "items", "shop", "store", "amazon", "ebay", "listing"}), {
        "name": "string",
        "price": "string",
        "rating": "string",
        "review_count": "string",
        "url": "string",
        "image": "string",
        "availability": "string",
    }),
    # Jobs / careers
    (frozenset({"job", "jobs", "career", "careers", "hiring", "vacancy", "opening", "position", "role"}), {
        "title": "string",
        "company": "string",
        "location": "string",
        "salary": "string",
        "date_posted": "string",
        "url": "string",
    }),
    # News / articles / blogs
    (frozenset({"news", "article", "articles", "blog", "post", "posts", "headline", "headlines", "story"}), {
        "title": "string",
        "author": "string",
        "date": "string",
        "summary": "string",
        "url": "string",
    }),
    # Real estate / property
    (frozenset({"house", "houses", "property", "properties", "apartment", "apartments", "real estate", "listing", "rent", "buy"}), {
        "address": "string",
        "price": "string",
        "bedrooms": "string",
        "bathrooms": "string",
        "area": "string",
        "url": "string",
    }),
    # Reviews
    (frozenset({"review", "reviews", "rating", "ratings", "feedback", "testimonial"}), {
        "reviewer": "string",
        "rating": "string",
        "date": "string",
        "review_text": "string",
        "verified": "string",
    }),
    # Companies / businesses
    (frozenset({"company", "companies", "business", "businesses", "startup", "startups", "firm", "organization"}), {
        "name": "string",
        "industry": "string",
        "location": "string",
        "employees": "string",
        "website": "string",
        "description": "string",
    }),
    # People / profiles
    (frozenset({"person", "people", "profile", "profiles", "contact", "contacts", "team", "member", "members"}), {
        "name": "string",
        "title": "string",
        "company": "string",
        "email": "string",
        "location": "string",
    }),
    # Videos / YouTube
    (frozenset({"video", "videos", "youtube", "channel", "playlist", "clip", "watch"}), {
        "title": "string",
        "channel": "string",
        "views": "string",
        "duration": "string",
        "published": "string",
        "url": "string",
    }),
    # Events
    (frozenset({"event", "events", "conference", "concert", "meetup", "webinar", "workshop"}), {
        "name": "string",
        "date": "string",
        "location": "string",
        "price": "string",
        "url": "string",
    }),
    # Generic "data" / "results" fallback
    (frozenset({"data", "results", "result", "records", "rows", "entries", "information", "info", "details"}), {
        "title": "string",
        "description": "string",
        "url": "string",
    }),
]

_QUANTITY_PATTERN = re.compile(r"\b(\d+)\b")


def _infer_schema_from_objective(objective: str) -> Optional[Dict[str, str]]:
    """
    Stage 1: keyword match against the objective.
    Returns a schema dict or None if nothing matches.
    """
    lower = objective.lower()
    words = set(re.findall(r"\b\w+\b", lower))

    best_match: Optional[Dict[str, str]] = None
    best_overlap: int = 0

    for keyword_set, schema in _INTENT_SCHEMA_MAP:
        overlap = len(words & keyword_set)
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = schema

    if best_overlap >= 1:
        logger.info(
            f"[SchemaInferencer] Stage 1 hit: overlap={best_overlap}, "
            f"fields={list(best_match.keys()) if best_match else []}"
        )
        return best_match

    return None


# ---------------------------------------------------------------------------
# Stage 2 — LLM-based DOM inference
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = """You are a data schema analyst.
Given a user's extraction objective and a partial DOM preview of a web page,
identify what structured fields are present and extractable.

OUTPUT: A valid JSON object where keys are field names (snake_case) and
values are the data type ("string", "number", "boolean", "url", "date").
Output ONLY the JSON. No explanation.

RULES:
1. Output 3-8 fields maximum. Do not over-engineer.
2. Only include fields that are visually present in the DOM preview.
3. Always include "url" if there are clickable links to detail pages.
4. Use snake_case for field names.
5. If the page contains a list of items, infer fields for ONE item (not the list).

Example output:
{"product_name": "string", "price": "string", "rating": "string", "url": "url"}"""


async def infer_schema(
    objective: str,
    page=None,          # Optional: playwright Page for DOM preview
    *,
    job_id: str = "unknown",
    llm_client=None,
) -> Optional[List[Dict[str, str]]]:
    """
    Infer an extraction schema from the objective and optionally the live page.

    Returns the schema as list[dict] (multi-item mode) or None if inference
    fails completely.

    Args:
        objective:   User's navigation objective string
        page:        Optional live Playwright page (for Stage 2 DOM preview)
        job_id:      For logging
        llm_client:  Optional SafeLLMClient instance
    """
    if not objective:
        return None

    # -----------------------------------------------------------------------
    # Stage 1: heuristic keyword match (zero LLM cost)
    # -----------------------------------------------------------------------
    schema_dict = _infer_schema_from_objective(objective)

    if schema_dict:
        logger.info(f"[{job_id}] SchemaInferencer: Stage 1 inferred {len(schema_dict)} fields")
        return [schema_dict]

    # -----------------------------------------------------------------------
    # Stage 2: LLM inference from DOM preview
    # -----------------------------------------------------------------------
    if page is None:
        logger.warning(f"[{job_id}] SchemaInferencer: Stage 1 miss, no page for Stage 2. Returning None.")
        return None

    logger.info(f"[{job_id}] SchemaInferencer: Stage 1 miss — attempting Stage 2 LLM inference")

    # Extract compact DOM preview
    dom_preview = ""
    try:
        result = await page.evaluate("""
        () => {
            const clone = document.body.cloneNode(true);
            ['script','style','svg','noscript','path','meta','link',
             'header','footer','nav','[aria-hidden="true"]'].forEach(s => {
                try { clone.querySelectorAll(s).forEach(e => e.remove()); } catch(_) {}
            });
            return clone.innerHTML.replace(/\\s+/g, ' ').slice(0, 4000);
        }
        """)
        dom_preview = result or ""
    except Exception as exc:
        logger.warning(f"[{job_id}] SchemaInferencer: DOM preview failed: {exc}")

    if not dom_preview:
        return None

    try:
        if llm_client is None:
            from core.llm.safe_client import SafeLLMClient
            llm_client = SafeLLMClient(use_extraction_model=True)

        user_prompt = (
            f"Objective: {objective}\n\n"
            f"DOM Preview:\n```html\n{dom_preview[:3000]}\n```\n\n"
            "Output the JSON schema for one item."
        )

        raw = await llm_client.call(
            system_prompt=_LLM_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
        )

        # Parse response
        clean = raw.strip()
        start, end = clean.find("{"), clean.rfind("}")
        if start != -1 and end > start:
            clean = clean[start:end + 1]

        inferred: Dict = json.loads(clean)
        if not isinstance(inferred, dict) or not inferred:
            raise ValueError("Empty or non-dict schema")

        # Sanitise keys
        schema_dict = {
            re.sub(r"[^a-z0-9_]", "_", k.lower().strip()): str(v)
            for k, v in inferred.items()
            if isinstance(k, str) and k.strip()
        }

        logger.info(
            f"[{job_id}] SchemaInferencer: Stage 2 inferred "
            f"{len(schema_dict)} fields: {list(schema_dict.keys())}"
        )
        return [schema_dict]

    except Exception as exc:
        logger.warning(f"[{job_id}] SchemaInferencer: Stage 2 failed: {exc}")
        return None
