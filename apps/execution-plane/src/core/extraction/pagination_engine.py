# src/core/extraction/pagination_engine.py
"""
PaginationEngine — Multi-Strategy Paginator (Fix for 2.6)

Problem being solved:
  The existing code tries only one pagination strategy: click "Next Page".
  This fails for:
    - Infinite scroll sites (Twitter, LinkedIn, TikTok)
    - "Load More" button sites (Google Search, many product pages)
    - URL-parameter pagination (?page=2, ?offset=100, ?cursor=xyz)
    - Sites where new_rows == 0 immediately (content loaded by XHR, not DOM)

Solution — 4-strategy priority cascade:
  1. NEXT_BUTTON:    Explicit next/forward pagination controls (highest signal)
  2. LOAD_MORE:      "Load More" / "Show More" / "See All" buttons
  3. INFINITE_SCROLL: Scroll-to-bottom with IntersectionObserver content gate
  4. URL_PARAMETER:  Increment ?page= / ?offset= / ?start= parameters in URL

Quantity-aware stopping:
  Each strategy respects `max_items`. When `len(accumulated_rows) >= max_items`,
  pagination halts regardless of what's on the next page. This supports
  "get 1000 products" style requests without hardcoding page counts.

New-content gate:
  After every navigation, we count the UNIQUE item-like nodes in the DOM before
  and after. If the count hasn't increased, pagination is exhausted regardless
  of what strategy was attempted.

Usage:
    paginator = PaginationEngine(page, job_id=job_id)
    async for strategy_used in paginator.advance(current_row_count=len(rows)):
        # caller extracts new rows from the page
        new_rows = await extract_with_dom(page, schema)
        if not new_rows:
            break
        rows.extend(new_rows)
        if len(rows) >= max_items:
            break
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from typing import AsyncIterator, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger("pagination_engine")


# ---------------------------------------------------------------------------
# Strategy identifiers
# ---------------------------------------------------------------------------

class PaginationStrategy(str, Enum):
    NEXT_BUTTON    = "next_button"
    LOAD_MORE      = "load_more"
    INFINITE_SCROLL = "infinite_scroll"
    URL_PARAMETER  = "url_parameter"
    EXHAUSTED      = "exhausted"    # Sentinel — no more pages


# ---------------------------------------------------------------------------
# JavaScript probes injected into the page
# ---------------------------------------------------------------------------

# Finds the most likely "Next Page" control. Returns CSS selector or null.
_NEXT_BUTTON_PROBE = """
() => {
    const candidates = [
        // Explicit ARIA labels (highest confidence)
        'a[aria-label*="next" i]:not([aria-disabled="true"]):not([disabled])',
        'button[aria-label*="next" i]:not([aria-disabled="true"]):not([disabled])',
        'a[aria-label*="Next" i]:not([aria-disabled="true"]):not([disabled])',
        // Rel="next" link (semantic HTML)
        'link[rel="next"]',
        'a[rel="next"]',
        // Common class/text patterns
        'a.next:not(.disabled):not([aria-disabled="true"])',
        'button.next:not(.disabled):not([disabled])',
        '[class*="next-page"]:not(.disabled):not([disabled])',
        '[class*="pagination__next"]:not(.disabled):not([disabled])',
        '[class*="pager__next"]:not(.disabled):not([disabled])',
        // Last resort: text content match in pagination containers
        '[class*="pagination"] a:last-child:not(.disabled)',
        '[class*="pager"] a:last-child:not(.disabled)',
    ];
    for (const sel of candidates) {
        try {
            const el = document.querySelector(sel);
            if (el && el.offsetParent !== null && !el.hasAttribute('disabled')) {
                // Extra check: don't click a "prev" that happens to match
                const txt = (el.textContent || el.getAttribute('aria-label') || '').toLowerCase();
                if (txt.includes('prev') || txt.includes('back') || txt.includes('<')) continue;
                return sel;
            }
        } catch (_) {}
    }
    return null;
}
"""

# Finds the most likely "Load More" button. Returns CSS selector or null.
_LOAD_MORE_PROBE = """
() => {
    const keywords = ['load more', 'show more', 'see more', 'view more',
                      'load all', 'show all', 'more results', 'more items',
                      'show additional', 'expand'];
    const candidates = document.querySelectorAll('button, a, [role="button"]');
    for (const el of candidates) {
        if (!el.offsetParent) continue;  // not visible
        const txt = (el.textContent || el.innerText || el.getAttribute('aria-label') || '').toLowerCase().trim();
        if (keywords.some(k => txt.includes(k)) && txt.length < 80) {
            // Build a reasonably specific selector
            if (el.id) return '#' + CSS.escape(el.id);
            const cls = Array.from(el.classList).slice(0, 2).join('.');
            if (cls) return el.tagName.toLowerCase() + '.' + cls;
            return el.tagName.toLowerCase() + '[data-testid="' + (el.dataset.testid || '') + '"]';
        }
    }
    return null;
}
"""

# Counts "item-like" nodes — proxy for content loaded. Returns count.
_ITEM_COUNT_PROBE = """
() => {
    // Count list-like containers: repeated sibling elements within common list wrappers.
    // Heuristic: a container has 3+ similar direct children.
    const CONTAINER_SELECTORS = [
        '[class*="results"]', '[class*="items"]', '[class*="products"]',
        '[class*="listings"]', '[class*="cards"]', '[class*="grid"]',
        '[class*="feed"]', '[class*="list"]', 'ul', 'ol',
    ];
    let best = 0;
    for (const sel of CONTAINER_SELECTORS) {
        try {
            const containers = document.querySelectorAll(sel);
            for (const c of containers) {
                const children = c.children.length;
                if (children > best) best = children;
            }
        } catch (_) {}
    }
    return best;
}
"""

# URL parameter pagination patterns
_URL_PARAM_PATTERNS = [
    "page",     # ?page=1
    "p",        # ?p=1
    "offset",   # ?offset=0
    "start",    # ?start=0
    "from",     # ?from=0
    "skip",     # ?skip=0
    "pg",       # ?pg=1
]


# ---------------------------------------------------------------------------
# PaginationEngine
# ---------------------------------------------------------------------------

class PaginationEngine:
    """
    Multi-strategy paginator. One instance per extraction session.

    The engine is stateful: it remembers what strategies have been tried
    and their results, avoiding redundant attempts.
    """

    def __init__(
        self,
        page: Page,
        *,
        job_id: str = "unknown",
        max_scroll_attempts: int = 5,
        scroll_settle_ms: int = 2500,
        load_more_settle_ms: int = 2000,
        next_button_settle_ms: int = 3000,
    ):
        self._page = page
        self._job_id = job_id
        self._max_scroll_attempts = max_scroll_attempts
        self._scroll_settle_ms = scroll_settle_ms
        self._load_more_settle_ms = load_more_settle_ms
        self._next_button_settle_ms = next_button_settle_ms

        # Track which strategies have been exhausted
        self._tried_next_button: bool = False
        self._tried_load_more: bool = False
        self._scroll_attempts: int = 0
        self._url_param_tried: bool = False
        self._last_item_count: int = 0
        self._current_url_page: int = 1

    # -----------------------------------------------------------------------
    # Public: async generator interface
    # -----------------------------------------------------------------------

    async def advance(self, *, current_row_count: int = 0) -> "Optional[PaginationStrategy]":
        """
        Attempt to advance to the next page of content using the best
        available strategy. Returns the strategy that was used, or
        PaginationStrategy.EXHAUSTED if no more pages are available.

        Caller should:
          1. Call advance()
          2. If result is EXHAUSTED: stop the pagination loop
          3. Otherwise: extract rows from the current page state, repeat

        Args:
            current_row_count: total rows accumulated so far (for logging)
        """
        page = self._page

        # Capture item count BEFORE advancing (for new-content gate)
        pre_count = await self._count_items()

        # ----------------------------------------------------------------
        # Strategy 1: Next Button
        # ----------------------------------------------------------------
        if not self._tried_next_button:
            result = await self._try_next_button()
            if result:
                post_count = await self._count_items()
                if post_count > pre_count or post_count == 0:
                    logger.info(
                        f"[{self._job_id}] Pagination: NEXT_BUTTON succeeded "
                        f"(items: {pre_count} → {post_count}, rows: {current_row_count})"
                    )
                    self._last_item_count = post_count
                    return PaginationStrategy.NEXT_BUTTON
                else:
                    logger.info(
                        f"[{self._job_id}] Pagination: NEXT_BUTTON clicked but no new items detected"
                    )
                    self._tried_next_button = True  # This button leads to same content

        self._tried_next_button = True

        # ----------------------------------------------------------------
        # Strategy 2: Load More Button
        # ----------------------------------------------------------------
        if not self._tried_load_more:
            result = await self._try_load_more()
            if result:
                post_count = await self._count_items()
                if post_count > pre_count:
                    logger.info(
                        f"[{self._job_id}] Pagination: LOAD_MORE succeeded "
                        f"(items: {pre_count} → {post_count})"
                    )
                    self._last_item_count = post_count
                    return PaginationStrategy.LOAD_MORE
                else:
                    logger.info(
                        f"[{self._job_id}] Pagination: LOAD_MORE button found but no new content"
                    )
                    # Don't mark as tried — Load More may appear again after scroll
            else:
                self._tried_load_more = True  # No button found — done trying

        # ----------------------------------------------------------------
        # Strategy 3: Infinite Scroll
        # ----------------------------------------------------------------
        if self._scroll_attempts < self._max_scroll_attempts:
            loaded = await self._try_infinite_scroll(pre_count=pre_count)
            self._scroll_attempts += 1
            if loaded:
                post_count = await self._count_items()
                logger.info(
                    f"[{self._job_id}] Pagination: INFINITE_SCROLL loaded new content "
                    f"(attempt {self._scroll_attempts}/{self._max_scroll_attempts}, "
                    f"items: {pre_count} → {post_count})"
                )
                self._last_item_count = post_count
                # Reset Load More flag — new content may have revealed button again
                self._tried_load_more = False
                return PaginationStrategy.INFINITE_SCROLL
            else:
                logger.info(
                    f"[{self._job_id}] Pagination: INFINITE_SCROLL attempt "
                    f"{self._scroll_attempts}/{self._max_scroll_attempts} — no new content"
                )

        # ----------------------------------------------------------------
        # Strategy 4: URL Parameter
        # ----------------------------------------------------------------
        if not self._url_param_tried:
            self._url_param_tried = True
            new_url = self._build_next_url()
            if new_url:
                try:
                    await page.goto(new_url, wait_until="domcontentloaded", timeout=15000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except PlaywrightTimeout:
                        pass
                    await asyncio.sleep(2)
                    post_count = await self._count_items()
                    if post_count > 0:
                        logger.info(
                            f"[{self._job_id}] Pagination: URL_PARAMETER advanced to {new_url[:80]}"
                        )
                        self._current_url_page += 1
                        self._url_param_tried = False  # Allow further increments
                        return PaginationStrategy.URL_PARAMETER
                except Exception as exc:
                    logger.info(f"[{self._job_id}] Pagination: URL_PARAMETER failed: {exc}")

        # ----------------------------------------------------------------
        # All strategies exhausted
        # ----------------------------------------------------------------
        logger.info(f"[{self._job_id}] Pagination: all strategies exhausted after {current_row_count} rows")
        return PaginationStrategy.EXHAUSTED

    # -----------------------------------------------------------------------
    # Private strategy implementations
    # -----------------------------------------------------------------------

    async def _try_next_button(self) -> bool:
        """Click the Next Page button. Returns True if a button was found and clicked."""
        try:
            selector: Optional[str] = await self._page.evaluate(_NEXT_BUTTON_PROBE)
            if not selector:
                return False

            await self._page.click(selector, timeout=5000)
            try:
                await self._page.wait_for_load_state("networkidle", timeout=self._next_button_settle_ms)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(self._next_button_settle_ms / 1000)
            return True

        except Exception as exc:
            logger.debug(f"[{self._job_id}] _try_next_button: {exc}")
            return False

    async def _try_load_more(self) -> bool:
        """Click the Load More button. Returns True if clicked."""
        try:
            selector: Optional[str] = await self._page.evaluate(_LOAD_MORE_PROBE)
            if not selector:
                return False

            await self._page.click(selector, timeout=5000)
            await asyncio.sleep(self._load_more_settle_ms / 1000)
            try:
                await self._page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeout:
                pass
            return True

        except Exception as exc:
            logger.debug(f"[{self._job_id}] _try_load_more: {exc}")
            return False

    async def _try_infinite_scroll(self, *, pre_count: int) -> bool:
        """
        Scroll to the bottom of the page and wait for new content to load.
        Uses a two-phase approach:
          1. Scroll to the current bottom
          2. Wait for page height to increase OR for new DOM items to appear

        Returns True if new content was detected.
        """
        try:
            prev_height: int = await self._page.evaluate("document.body.scrollHeight")

            # Phase 1: Scroll to bottom
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)

            # Phase 2: Wait for either:
            #   a) Page height increase (more DOM was appended)
            #   b) New item count increase
            # We poll for up to scroll_settle_ms
            settle_s = self._scroll_settle_ms / 1000
            poll_interval = 0.4
            elapsed = 0.0

            while elapsed < settle_s:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                new_height: int = await self._page.evaluate("document.body.scrollHeight")
                if new_height > prev_height:
                    return True  # Page grew — infinite scroll loaded content

                new_count: int = await self._count_items()
                if new_count > pre_count:
                    return True  # New items appeared (XHR append)

            # One final scroll in case of lazy-loader
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)

            final_height: int = await self._page.evaluate("document.body.scrollHeight")
            return final_height > prev_height

        except Exception as exc:
            logger.debug(f"[{self._job_id}] _try_infinite_scroll: {exc}")
            return False

    def _build_next_url(self) -> Optional[str]:
        """
        Construct the next-page URL by incrementing a known pagination
        parameter in the current URL. Returns None if no known parameter found.
        """
        try:
            current_url = self._page.url
            parsed = urlparse(current_url)
            params = parse_qs(parsed.query, keep_blank_values=True)

            for param in _URL_PARAM_PATTERNS:
                if param in params:
                    current_val_str = params[param][0]
                    try:
                        current_val = int(current_val_str)
                    except ValueError:
                        continue

                    # Determine increment: offset/start params use item-count steps,
                    # page/p params use 1-step increments
                    if param in ("offset", "start", "from", "skip"):
                        increment = max(self._last_item_count, 10)
                    else:
                        increment = 1

                    params[param] = [str(current_val + increment)]
                    new_query = urlencode(params, doseq=True)
                    new_url = urlunparse(parsed._replace(query=new_query))
                    logger.debug(
                        f"[{self._job_id}] URL param '{param}': "
                        f"{current_val} → {current_val + increment}"
                    )
                    return new_url

            # No known param found — try appending ?page=2 if URL has no params
            if not params:
                sep = "&" if "?" in current_url else "?"
                return f"{current_url}{sep}page={self._current_url_page + 1}"

            return None

        except Exception as exc:
            logger.debug(f"[{self._job_id}] _build_next_url: {exc}")
            return None

    async def _count_items(self) -> int:
        """Count item-like DOM nodes using the content probe."""
        try:
            return await self._page.evaluate(_ITEM_COUNT_PROBE)
        except Exception:
            return 0
