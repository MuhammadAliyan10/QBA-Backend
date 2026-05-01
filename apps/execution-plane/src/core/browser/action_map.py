import os
import logging
from typing import List, Dict, Optional
from playwright.async_api import Page

logger = logging.getLogger("actionMapBuilder")

class ActionMapBuilder:
    """
    Python wrapper to inject the minimal `extractor.js` payload
    into a Playwright instance to condense the entire DOM into
    a highly focused list of interactive Action Map nodes.
    """

    def __init__(self, page: Page):
        self.page = page
        self._script_cache: Optional[str] = None

    async def generate_action_map(self) -> list[Dict]:
        """
        Injects extractor.js, evaluates it across the visible DOM,
        and returns the compressed Action Map.
        """
        script = self._get_extractor_script()

        try:
            # Playwright evaluates the anonymous function and returns the JSON output
            action_map = await self.page.evaluate(script)
            logger.info(f"[ActionMapBuilder] Extracted {len(action_map)} interactive nodes in the viewport")
            return action_map
        except Exception as e:
            logger.error(f"[ActionMapBuilder] Failed to generate Action Map: {e}")
            return []

    def _get_extractor_script(self) -> str:
        """Loads extractor.js from disk (cached)."""
        if self._script_cache:
            return self._script_cache

        try:
            # Resolve relative path
            current_dir = os.path.dirname(os.path.abspath(__file__))
            extractor_path = os.path.join(current_dir, "extractor.js")

            with open(extractor_path, "r", encoding="utf-8") as f:
                self._script_cache = f.read()

            return self._script_cache
        except Exception as e:
            logger.error(f"[ActionMapBuilder] Could not find extractor.js: {e}")
            # Fallback to a super basic one just in case file is totally missing in prod build
            return '() => { return [{"error": "extractor.js missing"}]; }'
