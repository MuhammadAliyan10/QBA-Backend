# src/core/extraction/selector_synthesizer.py
"""
SelectorSynthesizer — AI-Generated CSS Selectors at Plan Time (Fix for 2.3)

Problem being solved:
  dom_extractor.py uses a hardcoded library of ~16 known field selectors.
  For arbitrary/custom user fields ("employee count", "founder name",
  "review sentiment score"), the fallback is generic and misses most data.
  The real cost: calling the LLM at EXTRACTION time (per field, per page)
  is expensive and slow.

Solution — Selector Synthesis at Plan Time:
  1. Once, before execution begins, call the LLM with a compact DOM preview
     of the target page.
  2. The LLM outputs a JSON map: { field_name → [css_selector, ...] }
  3. These selectors are stored in the recipe alongside the plan.
  4. At extraction time, the DOM extractor uses these synthesized selectors
     FIRST (before falling back to the heuristic library).
  5. Result: zero LLM calls during extraction for fields that were synthesized.

Edge cases handled:
  - Synthesized selectors that no longer match (site changed): fall back to
    heuristic library, then LLM per-field.
  - Multiple candidate selectors per field: validated in priority order,
    first match wins.
  - Schema with no extraction_schema provided: returns empty dict (no-op).
  - DOM preview that fails to load: returns empty dict, falls back gracefully.
  - LLM returns partial/incomplete selector map: merge with heuristic library.
  - LLM returns invalid CSS selectors: silently skip invalid ones.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page

logger = logging.getLogger("selector_synthesizer")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Maximum characters of DOM preview to pass to the LLM
DOM_PREVIEW_MAX_CHARS: int = 6000
# Maximum CSS selectors per field stored in the synthesized map
MAX_SELECTORS_PER_FIELD: int = 3
# Minimum number of fields in schema before synthesis is worth the LLM call
MIN_FIELDS_FOR_SYNTHESIS: int = 1


# ---------------------------------------------------------------------------
# DOM preview extraction (navigation-free, JS-injected)
# ---------------------------------------------------------------------------

_DOM_PREVIEW_JS = """
() => {
    // Remove purely decorative/noise nodes before serializing
    const NOISE_SELECTORS = [
        'script', 'style', 'svg', 'noscript', 'path', 'meta',
        'link[rel]', '[aria-hidden="true"]', '.cookie-banner',
        '[role="banner"]', '[role="navigation"]', 'header', 'footer', 'nav'
    ];

    // Clone body so we don't mutate the live DOM
    const clone = document.body.cloneNode(true);
    NOISE_SELECTORS.forEach(sel => {
        try {
            clone.querySelectorAll(sel).forEach(el => el.remove());
        } catch (_) {}
    });

    // Serialize the pruned DOM — limit to first 8000 chars
    const html = clone.innerHTML.replace(/\\s+/g, ' ').slice(0, 8000);

    // Also collect ALL unique class names and data-* attributes visible in the DOM
    // This is gold for selector synthesis — the LLM can see the real class names
    const classSet = new Set();
    const dataAttrSet = new Set();
    document.querySelectorAll('[class]').forEach(el => {
        el.className.split(' ').filter(c => c.length > 2 && c.length < 50).forEach(c => classSet.add(c));
    });
    document.querySelectorAll('*').forEach(el => {
        Array.from(el.attributes).forEach(attr => {
            if (attr.name.startsWith('data-') && attr.name.length < 40) {
                dataAttrSet.add(attr.name);
            }
        });
    });

    return {
        html: html,
        classes: Array.from(classSet).slice(0, 200),
        data_attrs: Array.from(dataAttrSet).slice(0, 100)
    };
}
"""

# ---------------------------------------------------------------------------
# LLM system prompt for selector synthesis
# ---------------------------------------------------------------------------
_SYNTHESIS_SYSTEM_PROMPT = """You are an expert CSS selector engineer.
Your task: given a partial DOM HTML preview and a list of field names to extract, \
output the best CSS selectors for each field.

RULES:
1. Output ONLY a valid JSON object. No markdown, no explanation.
2. Each key is a field name from the input. Each value is a JSON array of 1-3 CSS selectors, \
   ordered from most-specific to least-specific. Use the first selector that matches.
3. Prefer: data-* attributes > semantic class names > tag + attribute combos > generic tags.
4. NEVER use element IDs that look auto-generated (e.g., #root, #app, #__next).
5. If a field cannot be mapped to any selector in the given DOM, set its value to an empty array [].
6. Selectors must be valid CSS — no XPath, no pseudo-classes that take arguments (e.g., :nth-child(n) is OK, :contains() is not).
7. For list/repeated data, target the REPEATING CONTAINER CHILD, not the parent.

OUTPUT FORMAT (strict JSON, no other text):
{
  "field_name": ["selector1", "selector2"],
  "another_field": ["selector3"],
  "missing_field": []
}"""


def _build_synthesis_prompt(
    schema_fields: List[str],
    dom_preview: str,
    class_hints: List[str],
    data_attr_hints: List[str],
) -> str:
    class_str = ", ".join(f".{c}" for c in class_hints[:60])
    data_str = ", ".join(f"[{a}]" for a in data_attr_hints[:40])
    return (
        f"## Fields to extract\n{json.dumps(schema_fields)}\n\n"
        f"## DOM Preview (partial)\n```html\n{dom_preview[:DOM_PREVIEW_MAX_CHARS]}\n```\n\n"
        f"## Class names visible in full DOM\n{class_str[:800]}\n\n"
        f"## Data attributes visible in full DOM\n{data_str[:400]}\n\n"
        "Output ONLY the JSON selector map."
    )


# ---------------------------------------------------------------------------
# Validation — test each synthesized selector against the live DOM
# ---------------------------------------------------------------------------

async def _validate_selectors(
    page: Page,
    selector_map: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """
    For each field, run each synthesized selector against the live page.
    Remove selectors that match 0 elements or throw a CSS parse error.
    Keep the order intact (most-specific first).

    Returns a cleaned selector_map with only working selectors.
    """
    validated: Dict[str, List[str]] = {}

    for field_name, selectors in selector_map.items():
        good_selectors: List[str] = []
        for sel in selectors:
            if not sel or len(sel) > 200:
                continue
            try:
                count: int = await page.evaluate(
                    f"""() => document.querySelectorAll({json.dumps(sel)}).length"""
                )
                if count > 0:
                    good_selectors.append(sel)
                    logger.debug(
                        f"[SelectorSynth] ✓ '{field_name}' → '{sel}' ({count} matches)"
                    )
                else:
                    logger.debug(
                        f"[SelectorSynth] ✗ '{field_name}' → '{sel}' (0 matches — removed)"
                    )
            except Exception as exc:
                logger.debug(
                    f"[SelectorSynth] ✗ '{field_name}' → '{sel}' (invalid CSS: {exc})"
                )

        validated[field_name] = good_selectors[:MAX_SELECTORS_PER_FIELD]

    return validated


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

async def synthesize_selectors(
    page: Page,
    extraction_schema: Optional[Any],
    *,
    job_id: str = "unknown",
    llm_client=None,
) -> Dict[str, List[str]]:
    """
    Synthesize CSS selectors for each field in extraction_schema.

    Args:
        page:              Live Playwright page (should already be on target URL)
        extraction_schema: User-provided schema — dict or list[dict]
        job_id:            For logging
        llm_client:        Optional pre-initialized SafeLLMClient instance.
                           If None, a new instance is created.

    Returns:
        Dict mapping field_name → list of validated CSS selectors.
        Empty dict if schema is None/empty, or if synthesis fails.

    This function NEVER raises. All failures return an empty dict, which
    causes the extraction to fall back to the heuristic selector library.
    """
    # -----------------------------------------------------------------------
    # Guard: nothing to synthesize
    # -----------------------------------------------------------------------
    if not extraction_schema:
        return {}

    # Extract field names from schema (supports both dict and list[dict])
    schema_fields: List[str] = _extract_field_names(extraction_schema)
    if len(schema_fields) < MIN_FIELDS_FOR_SYNTHESIS:
        logger.debug(f"[{job_id}] SelectorSynth: schema has <{MIN_FIELDS_FOR_SYNTHESIS} fields, skipping.")
        return {}

    logger.info(f"[{job_id}] SelectorSynth: synthesizing selectors for {len(schema_fields)} fields: {schema_fields}")

    # -----------------------------------------------------------------------
    # Step 1: Extract DOM preview from live page
    # -----------------------------------------------------------------------
    dom_preview = ""
    class_hints: List[str] = []
    data_attr_hints: List[str] = []

    try:
        dom_data: dict = await page.evaluate(_DOM_PREVIEW_JS)
        dom_preview   = dom_data.get("html", "")[:DOM_PREVIEW_MAX_CHARS]
        class_hints   = dom_data.get("classes", [])
        data_attr_hints = dom_data.get("data_attrs", [])
        logger.debug(
            f"[{job_id}] SelectorSynth: DOM preview {len(dom_preview)} chars, "
            f"{len(class_hints)} classes, {len(data_attr_hints)} data-attrs"
        )
    except Exception as exc:
        logger.warning(f"[{job_id}] SelectorSynth: DOM preview extraction failed: {exc}. Aborting synthesis.")
        return {}

    if not dom_preview:
        logger.warning(f"[{job_id}] SelectorSynth: empty DOM preview, aborting.")
        return {}

    # -----------------------------------------------------------------------
    # Step 2: Call LLM
    # -----------------------------------------------------------------------
    try:
        if llm_client is None:
            from core.llm.safe_client import SafeLLMClient
            llm_client = SafeLLMClient(use_extraction_model=True)

        user_prompt = _build_synthesis_prompt(
            schema_fields=schema_fields,
            dom_preview=dom_preview,
            class_hints=class_hints,
            data_attr_hints=data_attr_hints,
        )

        raw_response = await llm_client.call(
            system_prompt=_SYNTHESIS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,  # Deterministic — selector synthesis is not creative
        )
    except Exception as exc:
        logger.warning(f"[{job_id}] SelectorSynth: LLM call failed: {exc}. Returning empty map.")
        return {}

    # -----------------------------------------------------------------------
    # Step 3: Parse LLM response
    # -----------------------------------------------------------------------
    selector_map: Dict[str, List[str]] = {}
    try:
        # Strip markdown fences if the model wrapped output
        cleaned = _strip_json_fences(raw_response)
        parsed = json.loads(cleaned)

        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")

        # Normalise: ensure values are lists of strings
        for field, selectors in parsed.items():
            if isinstance(selectors, list):
                selector_map[field] = [s for s in selectors if isinstance(s, str) and s.strip()]
            elif isinstance(selectors, str) and selectors:
                selector_map[field] = [selectors]
            else:
                selector_map[field] = []

        logger.info(
            f"[{job_id}] SelectorSynth: LLM returned {len(selector_map)} field entries"
        )
    except Exception as parse_exc:
        logger.warning(
            f"[{job_id}] SelectorSynth: LLM response parse failed: {parse_exc}. "
            f"Raw: {raw_response[:200]!r}"
        )
        return {}

    # -----------------------------------------------------------------------
    # Step 4: Validate synthesized selectors against live DOM
    # -----------------------------------------------------------------------
    try:
        validated_map = await _validate_selectors(page, selector_map)
    except Exception as val_exc:
        logger.warning(f"[{job_id}] SelectorSynth: validation step failed: {val_exc}")
        # Return unvalidated map rather than nothing — better than empty
        validated_map = selector_map

    # Summary log
    hits = sum(1 for v in validated_map.values() if v)
    total = len(schema_fields)
    logger.info(
        f"[{job_id}] SelectorSynth complete: {hits}/{total} fields have working selectors. "
        f"Map: { {k: v[:1] for k, v in validated_map.items()} }"
    )

    return validated_map


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _extract_field_names(schema: Any) -> List[str]:
    """Extract the list of field names from a schema (dict or list[dict])."""
    if isinstance(schema, dict):
        return list(schema.keys())
    if isinstance(schema, list) and schema and isinstance(schema[0], dict):
        return list(schema[0].keys())
    return []


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences that some models wrap JSON in."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Find the outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text
