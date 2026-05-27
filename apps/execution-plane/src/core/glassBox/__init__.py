"""
glassBox/ - Real-Time Browser Visualization Module

The Glass Box provides live streaming of headless browser automation
with remote control capabilities.

Components:
- browserStreamer.py: CDP screencast → NATS JetStream
- inputBridge.py: Frontend events → Playwright actions

Usage:
    from core.glassBox import BrowserStreamer, InputBridge

    # Attach streaming to page
    async with BrowserStreamer(page, "workflow_123") as streamer:
        # Your automation runs here
        await page.click("button")

    # Handle remote input
    bridge = InputBridge(page)
    await bridge.handle_event({"type": "click", "x": 100, "y": 50})
"""

from core.glassBox.browserStreamer import (
    BrowserStreamer,
    StreamConfig,
    StreamerState,
    NATSPublisher,
    create_streamer,
)

from core.glassBox.inputBridge import (
    InputBridge,
    InputConfig,
)

__all__ = [
    # Streaming
    "BrowserStreamer",
    "StreamConfig",
    "StreamerState",
    "NATSPublisher",
    "create_streamer",
    # Input
    "InputBridge",
    "InputConfig",
]
