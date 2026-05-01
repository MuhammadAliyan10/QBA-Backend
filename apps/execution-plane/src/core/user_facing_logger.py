"""
User-Facing Logger - The Voice of the Glass Box

Translates technical events into professional, calm, and non-technical messages
for the end-user. Ensures the "Glass Box" experience is transparent but not overwhelming.

Features:
- Jargon Removal: "Selector" -> "Element", "HTTP 200" -> "Success"
- Panic Prevention: Friendly error messages instead of tracebacks
- Silence Prevention: Heartbeat/Progress logs for long operations
"""

import logging
import random
import asyncio
from typing import Optional, Dict, Any
from core.nervous_system import NervousSystem

logger = logging.getLogger("user_logger")

class UserFriendlyLogger:
    """
    Wraps the NervousSystem to provide a polished UX for logs.
    """

    # =========================================================================
    # THE TRANSLATION DICTIONARY (Internal Event -> User Message)
    # =========================================================================
    TRANSLATION_MAP = {
        # --- SUCCESS EVENTS ---
        "FOUND_ELEMENT": [
            "I've identified the {element}.",
            "Found the {element} on the page.",
            "Locating {element}... Done."
        ],
        "CLICKED_ELEMENT": [
            "Clicking the {element}.",
            "Interacting with the {element}.",
            "Moving forward by clicking {element}."
        ],
        "TYPED_TEXT": [
            "Typing information into {element}.",
            "Filling out the {element}.",
            "Entering data..."
        ],
        "NAVIGATING": [
            "Navigating to {url}...",
            "Heading over to {url}.",
            "Loading the next page..."
        ],
        "DOWNLOAD_START": [
            "Starting download for {filename}...",
            "Found a file. Downloading {filename} now."
        ],
        "DOWNLOAD_COMPLETE": [
            "Download complete: {filename}.",
            "File saved successfully."
        ],

        # --- WAIT/PROGRESS EVENTS ---
        "WAITING_NETWORK": [
            "The page is taking a moment to load.",
            "Waiting for the website to settle...",
            "Just a moment, letting the content load."
        ],
        "WAITING_CAPTCHA": [
            "I see a security check. Pausing for a moment.",
            "Please help me with this CAPTCHA.",
            "Security challenge detected. Waiting for your input."
        ],
        "PROCESSING_RAG": [
            "Reading the page content...",
            "Analyzing the page structure...",
            "Thinking about the next step..."
        ],

        # --- ERROR EVENTS ---
        "ELEMENT_NOT_FOUND": [
            "I couldn't find the {element}. I'll try looking again.",
            "The {element} seems to be missing. Re-scanning...",
            "Hmm, I can't see the {element} right now."
        ],
        "TIMEOUT": [
            "This is taking longer than expected.",
            "The website is being slow. I'm still waiting.",
            "Connection is a bit slow..."
        ],
        "LOGIN_FAILED": [
            "I couldn't log in. Please check your credentials.",
            "Login didn't work. The username or password might be incorrect.",
            "Access denied. Please verify your login details."
        ],
        "GENERIC_ERROR": [
            "I ran into a small issue. Retrying...",
            "Something unexpected happened. Attempting to recover.",
            "Adjusting my approach..."
        ]
    }

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._last_log_time = 0

    async def info(self, event_key: str, **kwargs):
        """
        Log a user-friendly info message.

        Args:
            event_key: Key from TRANSLATION_MAP (e.g., "FOUND_ELEMENT")
            **kwargs: Variables to inject (e.g., element="Submit Button")
        """
        message = self._translate(event_key, **kwargs)
        await NervousSystem.publish_update(
            job_id=self.job_id,
            status="RUNNING",
            message=message,
            node_id="worker"
        )

    async def error(self, event_key: str, error_details: Optional[str] = None, **kwargs):
        """
        Log a user-friendly error message. never dumps tracebacks.
        """
        message = self._translate(event_key, **kwargs)

        # We log the raw error to the backend logger for devs, but NOT to the user
        if error_details:
            logger.error(f"[Job {self.job_id}] Internal Error: {error_details}")

        await NervousSystem.publish_update(
            job_id=self.job_id,
            status="FAILED" if event_key == "FATAL" else "RUNNING", # Most errors are non-fatal warnings to the user
            message=message,
            node_id="worker"
        )

    async def progress(self, message: str):
        """
        Log a raw progress message (for things that don't need translation).
        """
        await NervousSystem.publish_update(
            job_id=self.job_id,
            status="RUNNING",
            message=message,
            node_id="worker"
        )

    def _translate(self, key: str, **kwargs) -> str:
        """
        Translates a technical key into a human string.
        """
        templates = self.TRANSLATION_MAP.get(key, [f"Processing... ({key})"])
        template = random.choice(templates)

        try:
            return template.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Missing format key {e} for message {key}")
            return template # Return unformatted template as fallback

    # --- HEARTBEAT MECHANISM ---
    async def heartbeat_loop(self, stop_event: asyncio.Event, interval: int = 5):
        """
        Runs in background to send "I'm still working" logs during long waits.
        """
        messages = [
            "Still working...",
            "Processing data...",
            "This is a large page, almost done...",
            "Downloading content..."
        ]

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                # Timeout reached, send heartbeat
                msg = random.choice(messages)
                await self.progress(msg)
