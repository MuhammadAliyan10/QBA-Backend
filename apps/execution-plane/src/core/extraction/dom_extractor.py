# src/core/extraction/dom_extractor.py
"""
Schema-Driven DOM Extractor — Deterministic Phase 2 replacement.

Strategy:
  1. Inject a JavaScript schema-walker into the live Playwright page.
  2. The JS walker traverses the DOM using structural patterns derived from the
     user's extraction schema (field names → heuristic selectors).
  3. For single-item schemas, returns a flat dict.
  4. For list schemas, returns a list of dicts, one per detected repeating unit.
  5. If DOM extraction returns empty/null for a required field, fall back to the
     LLM extraction path (safe_client.call) for that field only.

This replaces the previous approach of:
  page.content() → markdownify → 2000-char truncated blob → LLM guess
"""

import json
import logging
import re
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger("dom_extractor")

# ---------------------------------------------------------------------------
# Heuristic field → CSS selector pattern library
# ---------------------------------------------------------------------------
_FIELD_SELECTORS: dict[str, list[str]] = {
    # Generic identity patterns
    "title":       ["h1", "h2", ".title", "[data-title]", ".product-title", ".job-title", "title"],
    "name":        ["h1", "h2", ".name", "[data-name]", ".product-name", ".item-name"],
    "price":       [".price", "[data-price]", ".cost", ".amount", "[itemprop='price']", ".product-price"],
    "description": [".description", "[data-description]", "p", ".summary", "[itemprop='description']"],
    "url":         ["a[href]", "[data-url]", "link[rel='canonical']"],
    "image":       ["img[src]", "[data-image]", "[itemprop='image']", ".product-image img"],
    "rating":      [".rating", "[data-rating]", ".stars", "[itemprop='ratingValue']", ".review-score"],
    "author":      [".author", "[data-author]", "[itemprop='author']", ".byline"],
    "date":        ["time", ".date", "[data-date]", "[itemprop='datePublished']", ".published"],
    "category":    [".category", "[data-category]", "[itemprop='category']", ".breadcrumb li:last-child"],
    "location":    [".location", "[data-location]", "[itemprop='addressLocality']", ".address"],
    "company":     [".company", "[data-company]", ".employer", "[itemprop='hiringOrganization']"],
    "salary":      [".salary", "[data-salary]", ".compensation", ".pay"],
    "availability": [".availability", "[data-availability]", ".stock", ".in-stock"],
    "sku":         [".sku", "[data-sku]", "[itemprop='sku']", "#sku"],
    "brand":       [".brand", "[data-brand]", "[itemprop='brand']", ".manufacturer"],
}

# Common list container selectors — tried in order, first match wins
_LIST_CONTAINERS: list[str] = [
    # Structured data list patterns
    "[data-testid*='result']",
    "[data-testid*='item']",
    "[data-testid*='product']",
    "[data-testid*='card']",
    ".product-card",
    ".job-card",
    ".result-item",
    ".list-item",
    "article",
    ".card",
    # Generic repeating pattern — detect via tag repetition
    "li",
    "tr:not(thead tr)",
]

_JS_DOM_EXTRACTOR = """
(function(schema, fieldSelectors, listContainers) {

    // ----------------------------------------------------------------
    // Utility: safely get text from a matched element
    // ----------------------------------------------------------------
    function getText(el) {
        if (!el) return null;
        // For price/rating, prefer attribute data then text
        const dataVal = el.getAttribute('data-price') || el.getAttribute('data-rating');
        if (dataVal) return dataVal.trim();
        // For images return src
        if (el.tagName === 'IMG') return el.getAttribute('src') || el.getAttribute('data-src');
        // For anchors return href for url fields
        if (el.tagName === 'A') return el.getAttribute('href');
        return (el.innerText || el.textContent || '').trim().slice(0, 500) || null;
    }

    // ----------------------------------------------------------------
    // Utility: find first matching element from a selector list within scope
    // ----------------------------------------------------------------
    function findFirst(selectors, scope) {
        for (const sel of selectors) {
            try {
                const el = scope.querySelector(sel);
                if (el) return el;
            } catch (e) {}
        }
        return null;
    }

    // ----------------------------------------------------------------
    // Detect whether schema is a list (array) schema or object schema
    // ----------------------------------------------------------------
    function isListSchema(schema) {
        return Array.isArray(schema) && schema.length > 0 && typeof schema[0] === 'object';
    }

    const schemaFields = isListSchema(schema)
        ? Object.keys(schema[0])
        : Object.keys(schema || {});

    // ----------------------------------------------------------------
    // LIST EXTRACTION MODE: detect repeating units in the DOM
    // ----------------------------------------------------------------
    function extractList(container) {
        const results = [];
        const units = container.querySelectorAll(':scope > *');
        if (units.length === 0) return results;

        for (const unit of units) {
            const item = {};
            for (const field of schemaFields) {
                const selList = fieldSelectors[field] || ['.' + field, '[data-' + field + ']'];
                const el = findFirst(selList, unit);
                item[field] = getText(el);
            }
            // Only include items that have at least one non-null field
            if (Object.values(item).some(v => v !== null)) {
                results.push(item);
            }
        }
        return results;
    }

    // ----------------------------------------------------------------
    // SINGLE ITEM EXTRACTION MODE
    // ----------------------------------------------------------------
    function extractSingle() {
        const item = {};
        for (const field of schemaFields) {
            const selList = fieldSelectors[field] || ['.' + field, '[data-' + field + ']'];
            const el = findFirst(selList, document.body);
            item[field] = getText(el);
        }
        return item;
    }

    // ----------------------------------------------------------------
    // MAIN: decide list vs single, find best container, extract
    // ----------------------------------------------------------------
    if (!isListSchema(schema)) {
        return { mode: 'single', data: extractSingle() };
    }

    // List schema: find the best repeating container
    for (const containerSel of listContainers) {
        let candidates;
        try {
            candidates = Array.from(document.querySelectorAll(containerSel));
        } catch (e) { continue; }

        if (candidates.length < 2) continue;  // must have at least 2 items to be a list

        // Group by parent — same parent = same list
        const parentMap = new Map();
        for (const el of candidates) {
            const parent = el.parentElement;
            if (!parent) continue;
            const key = parent;
            if (!parentMap.has(key)) parentMap.set(key, []);
            parentMap.get(key).push(el);
        }

        let bestParent = null;
        let bestCount = 0;
        for (const [parent, children] of parentMap) {
            if (children.length > bestCount) {
                bestCount = children.length;
                bestParent = parent;
            }
        }

        if (bestParent && bestCount >= 2) {
            const items = extractList(bestParent);
            if (items.length > 0) {
                return { mode: 'list', container: containerSel, count: items.length, data: items };
            }
        }
    }

    // Fallback: treat whole body as single and return single item (signals to Python to use LLM)
    return { mode: 'single_fallback', data: extractSingle() };

})(schema, fieldSelectors, listContainers);
"""


async def extract_with_dom(
    page: Page,
    extraction_schema: dict | list | None,
    *,
    llm_fallback_fn=None,
) -> dict | list:
    """
    Primary Phase 2 extraction entrypoint.

    Args:
        page:              Live Playwright page after navigation is complete.
        extraction_schema: The user-provided JSON schema.
                           - dict → single-item extraction
                           - list with one dict element → multi-item list extraction
                           - None → returns raw page text summary
        llm_fallback_fn:  Optional async callable(field_name, page_text) → str.
                          Called for individual fields that the DOM walker returns null.

    Returns:
        dict or list matching the schema structure.
    """
    if extraction_schema is None:
        logger.info("[DOMExtractor] No schema provided — returning raw page title")
        title = await page.title()
        return {"page_title": title, "url": page.url}

    schema = extraction_schema
    is_list_mode = isinstance(schema, list) and len(schema) > 0

    try:
        result: dict = await page.evaluate(
            _JS_DOM_EXTRACTOR,
            [schema, _FIELD_SELECTORS, _LIST_CONTAINERS],
        )
    except Exception as exc:
        logger.warning(f"[DOMExtractor] JS eval failed: {exc}. Falling back to LLM extraction.")
        result = {"mode": "js_error", "data": None}

    mode: str = result.get("mode", "unknown")
    data: Any = result.get("data")

    logger.info(
        f"[DOMExtractor] mode={mode} | "
        f"count={result.get('count', 'N/A')} | "
        f"container={result.get('container', 'N/A')}"
    )

    # -----------------------------------------------------------------------
    # List result validation
    # -----------------------------------------------------------------------
    if is_list_mode and mode == "list" and isinstance(data, list) and len(data) > 0:
        data = _coerce_list(data, schema[0])
        await _fill_null_fields_in_list(data, page, llm_fallback_fn)
        return data

    # -----------------------------------------------------------------------
    # Single item result
    # -----------------------------------------------------------------------
    if not is_list_mode and isinstance(data, dict):
        data = _coerce_dict(data, schema)
        await _fill_null_fields(data, page, llm_fallback_fn)
        return data

    # -----------------------------------------------------------------------
    # Degraded path: DOM walker couldn't find the structure — use LLM
    # -----------------------------------------------------------------------
    logger.warning(
        f"[DOMExtractor] DOM extraction returned mode='{mode}' with no usable data. "
        "Delegating full extraction to LLM fallback."
    )
    if llm_fallback_fn is not None:
        page_text = await _get_page_text(page)
        return await llm_fallback_fn(schema, page_text)

    # No fallback available — return best-effort partial result
    return data if data is not None else {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_dict(data: dict, schema: dict) -> dict:
    """Ensure all schema keys are present in data, adding None for missing ones."""
    coerced = {}
    for key in schema.keys():
        coerced[key] = data.get(key)
    return coerced


def _coerce_list(data: list, schema_item: dict) -> list:
    """Ensure each item in the list has all schema keys."""
    return [_coerce_dict(item, schema_item) for item in data]


async def _fill_null_fields(
    data: dict,
    page: Page,
    llm_fallback_fn,
) -> None:
    """
    For any field that the DOM walker returned null, attempt LLM fill
    using a targeted page-text snippet. Mutates data in-place.
    """
    if llm_fallback_fn is None:
        return
    null_fields = [k for k, v in data.items() if v is None]
    if not null_fields:
        return
    logger.info(f"[DOMExtractor] LLM filling {len(null_fields)} null fields: {null_fields}")
    page_text = await _get_page_text(page)
    for field in null_fields:
        try:
            data[field] = await llm_fallback_fn({field: None}, page_text)
            if isinstance(data[field], dict):
                data[field] = data[field].get(field)
        except Exception as exc:
            logger.warning(f"[DOMExtractor] LLM fill failed for field '{field}': {exc}")


async def _fill_null_fields_in_list(
    data: list,
    page: Page,
    llm_fallback_fn,
) -> None:
    """
    For list results, only attempt LLM fill on the first 3 items
    (performance guard). Full list null-fill is too expensive.
    """
    if llm_fallback_fn is None or not data:
        return
    sample_size = min(3, len(data))
    for i in range(sample_size):
        await _fill_null_fields(data[i], page, llm_fallback_fn)


async def _get_page_text(page: Page, max_chars: int = 4000) -> str:
    """Extract visible text from the page, truncated to max_chars."""
    try:
        text: str = await page.evaluate(
            """() => {
                const els = document.body.querySelectorAll('p, h1, h2, h3, li, td, th, span.price, .title');
                return Array.from(els)
                    .map(el => el.innerText || el.textContent || '')
                    .filter(t => t.trim().length > 0)
                    .join('\\n');
            }"""
        )
        return text[:max_chars]
    except Exception:
        return ""


def _normalize_field_name(field: str) -> str:
    """snake_case → space-separated word, for selector heuristic matching."""
    return re.sub(r"[_\-]", " ", field).lower()
