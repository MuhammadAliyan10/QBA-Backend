# src/core/recipe/operatorRealizer.py
"""
operatorRealizer.py - Semantic Intent Realization Layer

Maps high-level Intent Ops (SET_FILTER, OPEN_RESULT, etc.) to multiple
realization strategies (URL mutation, DOM interaction, Search).
"""

import logging
import asyncio
from typing import Any, Dict, List, Optional
from playwright.async_api import Page, ElementHandle

from core.recipe.recipe_schema import ActionType, Action
from core.selector.smart_finder import SmartFinder, FindResult, FinderLayer

logger = logging.getLogger("operatorRealizer")

class OperatorRealizer:
    """Realizes high-level intent ops into concrete browser actions."""

    def __init__(self, page: Page, finder: SmartFinder):
        self.page = page
        self.finder = finder

        # Phase 15: Register Replay Infrastructure
        from core.selector.selectorRegistry import SelectorRegistry
        self.registry = SelectorRegistry()

    async def realize(self, action: Action) -> FindResult:
        """
        Main entry point for realization.
        Enforces Phase 15 Replay & Execution sequence:
        1. Profile SPA Configuration
        2. Attempt cached selector bundle based on rank
        3. Live Resolve (SmartFinder) fallback
        4. Self-Heal registry
        """
        from urllib.parse import urlparse
        from core.browser.state_signature import StateSignatureGenerator
        from core.selector.selectorRegistry import SelectorBundle

        domain = urlparse(self.page.url).netloc
        op_type = action.type
        arguments = getattr(action, "data", {}) or {}

        logger.info(f"[Realizer] Realizing Intent Op: {op_type} ({action.intent}={action.value})")

        # 1. Profile Page State (A11y Hash)
        state_signature = await StateSignatureGenerator.generate(self.page)

        # 2. Key Generation & Cache Match
        cache_key = self.registry.generate_key(domain, state_signature, op_type.value, arguments)
        cached_bundles = self.registry.get_bundles(cache_key)

        if cached_bundles:
            logger.info(f"[ReplayEngine] Found {len(cached_bundles)} cached bundles for intent={action.intent}. Attempting strict playback.")
            for bundle in cached_bundles:
                try:
                    # Execute Locator Playback
                    if bundle.locator_type in ["css", "xpath"]:
                        # 500ms strict timeout to avoid hang on stale selectors
                        el = await self.page.wait_for_selector(bundle.locator_value, state="attached", timeout=500)
                        if el:
                            logger.info(f"[ReplayEngine] Cache HIT on rank {bundle.rank} ({bundle.locator_value})")
                            return FindResult(
                                element=el,
                                selector_id=bundle.selector_id,
                                locator_type=bundle.locator_type,
                                locator_value=bundle.locator_value,
                                confidence=bundle.confidence,
                                reason_codes=["CACHE_REPLAY_SUCCESS"]
                            )
                except Exception as e:
                    logger.debug(f"[ReplayEngine] Bundle locator {bundle.locator_value} failed validation: {e}")
                    # Demote bundle penalty on replay failure
                    self.registry.demote_bundle(cache_key, bundle.locator_value)

            logger.warning(f"[ReplayEngine] Cache EXHAUSTED for intent={action.intent}. Falling back to Live SmartFinder Resolve.")

        # 3. Live Resolve (Cache Miss or Exhaustion)
        if op_type == ActionType.SET_FILTER:
            res = await self._realize_set_filter(action)
        elif op_type == ActionType.OPEN_RESULT:
            res = await self._realize_open_result(action)
        elif op_type == ActionType.SEARCH:
            res = await self._realize_search(action)
        elif op_type == ActionType.APPLY_SORT:
            res = await self._realize_apply_sort(action)
        else:
            # Fallback for standard actions
            res = await self.finder.find(
                intent=action.intent,
                value=action.value,
            )

        # 4. Self-Heal Registry
        if res.found and res.locator_value and res.locator_type in ["css", "xpath"]:
            bundle = SelectorBundle(
                key=cache_key,
                intent_type=op_type.value,
                argument_schema=self.registry._normalize_arguments(arguments),
                selector_id=res.selector_id,
                locator_type=res.locator_type,
                locator_value=res.locator_value,
                rank=1,
                confidence=res.confidence,
                reason_codes=res.reason_codes,
                fingerprint=res.fingerprint or {}
            )
            self.registry.register_bundle(bundle, domain, state_signature)
            logger.info(f"[ReplayEngine] Registry self-healed. Upserted locator {res.locator_value} for intent={action.intent}")

        return res

    async def _realize_set_filter(self, action: Action) -> FindResult:
        """
        Strategies for SET_FILTER(key, value):
        1. URL Strategy: Try common query param patterns.
        2. DOM Strategy: Use SmartFinder to find and click the filter.
        """
        key = action.intent.lower() if action.intent else ""
        value = action.value.lower() if action.value else ""

        # Strategy 1: URL Mutation (Fastest, most reliable if matches)
        # Attempt common mappings: l=python, q=python, sort=newest, etc.
        url_strategies = [
            f"{key}={value}",
            f"q={value}",
            f"f={value}",
        ]

        # For now, let's just log URL strategy potential.
        # Real URL mutation requires schema awareness.
        logger.debug(f"[Realizer:SET_FILTER] Strategy 1: Checking URL patterns {url_strategies}")

        # Strategy 2: DOM Interaction
        logger.debug(f"[Realizer:SET_FILTER] Strategy 2: DOM Interaction via SmartFinder")
        intent_str = f"{key} {value}, {value}"
        result = await self.finder.find(
            intent=intent_str,
            action_type="find_and_click"
        )
        return result

    async def _realize_open_result(self, action: Action) -> FindResult:
        """
        Strategies for OPEN_RESULT(rank):
        1. DOM Position Strategy: Use SmartFinder with position hint.
        """
        try:
            rank = int(action.value) - 1 if action.value and action.value.isdigit() else 0
        except ValueError:
            rank = 0

        logger.debug(f"[Realizer:OPEN_RESULT] Strategy: DOM Position {rank}")
        # Use a broad intent like "result, link, item" to catch generic result items
        intent_str = action.intent or "result, link, entry, item"
        result = await self.finder.find(
            intent=intent_str,
            action_type="find_and_click",
            position=rank
        )
        return result

    async def _realize_search(self, action: Action) -> FindResult:
        """
        Strategies for SEARCH(query):
        1. URL Strategy: /search?q=...
        2. DOM Strategy: Find search box, type, and press enter.
        """
        query = action.value or ""
        logger.debug(f"[Realizer:SEARCH] Strategy: DOM Interaction for '{query}'")

        # 1. Find search box
        res = await self.finder.find(
            intent="search input, search box, search query",
            action_type="type_text"
        )
        url = self.page.url
        if "github.com/trending" in url:
            if action.type == ActionType.SET_FILTER and action.intent == "language":
                target_url = f"https://github.com/trending/{action.value.lower()}"
                logger.info(f"[Realizer:URL] Mutating Trending URL to: {target_url}")
                await self.page.goto(target_url)
                await self.page.wait_for_url(target_url)
                return FindResult(layer=FinderLayer.DOMAIN_MEMORY, confidence=1.0)

        if res.found:
            # Note: The actual execution of TYPE + ENTER is handled by ActionRunner/Engine.
            # Realizer just finds the element and prepares the result.
            return res

        return FindResult(layer=FinderLayer.NONE, error="Search box not found")

    async def _realize_apply_sort(self, action: Action) -> FindResult:
        """
        Strategies for APPLY_SORT(value):
        1. DOM Strategy: Find sort dropdown or button.
        """
        value = action.value or ""
        logger.debug(f"[Realizer:APPLY_SORT] Strategy: DOM Interaction for '{value}'")
        intent_str = f"sort {value}, {value}"
        return await self.finder.find(
            intent=intent_str,
            action_type="find_and_click"
        )
