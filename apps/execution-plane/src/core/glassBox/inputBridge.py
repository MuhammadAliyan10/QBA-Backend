"""InputBridge - Remote Control via Playwright"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from playwright.async_api import Page

logger = logging.getLogger("glassBox.input")


@dataclass
class InputConfig:
    """Input handling configuration."""
    canvas_width: int = 1280
    canvas_height: int = 720
    max_events_per_second: int = 60
    min_event_interval_ms: int = 16
    type_delay_ms: int = 50


class InputBridge:
    """Translates frontend input events to Playwright actions."""

    def __init__(self, page: Page, config: InputConfig = None):
        self.page = page
        self.config = config or InputConfig()
        self._viewport: Optional[Dict] = None
        self._last_event_time = 0.0

    async def _get_viewport(self) -> Dict:
        if not self._viewport:
            viewport = self.page.viewport_size
            self._viewport = viewport if viewport else {"width": 1280, "height": 720}
        return self._viewport

    def _scale_coordinates(self, x: float, y: float, vw: int, vh: int) -> tuple[float, float]:
        scale_x = vw / self.config.canvas_width
        scale_y = vh / self.config.canvas_height
        return (x * scale_x, y * scale_y)

    async def handle_event(self, event: dict[str, Any]) -> bool:
        event_type = event.get("type", "")

        try:
            if event_type in ["click", "mousedown", "mouseup", "dblclick"]:
                return await self._handle_mouse_click(event)
            elif event_type == "mousemove":
                return await self._handle_mouse_move(event)
            elif event_type == "wheel":
                return await self._handle_wheel(event)
            elif event_type in ["keydown", "keyup", "keypress"]:
                return await self._handle_key(event)
            elif event_type == "type":
                return await self._handle_type(event)
            else:
                logger.warning(f"[Input] Unknown event: {event_type}")
                return False
        except Exception as e:
            logger.error(f"[Input] Failed: {e}")
            return False

    async def _handle_mouse_click(self, event: Dict) -> bool:
        viewport = await self._get_viewport()
        x, y = self._scale_coordinates(
            event.get("x", 0), event.get("y", 0),
            viewport["width"], viewport["height"]
        )

        button = event.get("button", "left")
        event_type = event.get("type")

        if event_type == "mousedown":
            await self.page.mouse.down(button=button)
        elif event_type == "mouseup":
            await self.page.mouse.up(button=button)
        else:
            click_count = 2 if event_type == "dblclick" else 1
            await self.page.mouse.move(x, y)
            await self.page.mouse.click(x, y, button=button, click_count=click_count)

        return True

    async def _handle_mouse_move(self, event: Dict) -> bool:
        viewport = await self._get_viewport()
        x, y = self._scale_coordinates(
            event.get("x", 0), event.get("y", 0),
            viewport["width"], viewport["height"]
        )
        await self.page.mouse.move(x, y)
        return True

    async def _handle_wheel(self, event: Dict) -> bool:
        viewport = await self._get_viewport()
        x, y = self._scale_coordinates(
            event.get("x", 0), event.get("y", 0),
            viewport["width"], viewport["height"]
        )
        await self.page.mouse.move(x, y)
        await self.page.mouse.wheel(event.get("deltaX", 0), event.get("deltaY", 0))
        return True

    async def _handle_key(self, event: Dict) -> bool:
        key = event.get("key", "")
        if not key:
            return False

        event_type = event.get("type")
        if event_type == "keydown":
            await self.page.keyboard.down(key)
        elif event_type == "keyup":
            await self.page.keyboard.up(key)
        else:
            await self.page.keyboard.press(key)
        return True

    async def _handle_type(self, event: Dict) -> bool:
        text = event.get("text", "")
        if not text:
            return False
        await self.page.keyboard.type(text, delay=self.config.type_delay_ms)
        return True

    def update_canvas_size(self, width: int, height: int):
        self.config.canvas_width = width
        self.config.canvas_height = height

    async def update_viewport_cache(self):
        self._viewport = None
        await self._get_viewport()
