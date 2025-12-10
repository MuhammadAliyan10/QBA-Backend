"""
NetworkSniffer - Protocol Reverse Engineering Module (Level 5)

Captures HTTP requests and responses for API replay automation.
Intercepts network traffic, extracts authentication credentials and payloads,
and validates them based on response status codes.

Key Features:
- Response-driven validation (only captures on 2xx success)
- Case-insensitive header matching
- Robust payload parsing (JSON, form-data, raw)
- Support for all HTTP methods
- Rate limit detection
"""

import logging
import json
from typing import Dict, Optional, Any
from playwright.async_api import Page, Request, Response

logger = logging.getLogger("networkSniffer")


class NetworkSniffer:
    """
    The Traffic Analyst Module (Level 5).
    Captures verified API Credentials and Payloads for high-speed replay.
    """

    def __init__(self, target_domain: Optional[str] = None):
        """
        Initialize the NetworkSniffer.

        Args:
            target_domain: Optional domain filter (e.g., "api.example.com").
                          If None, captures from all domains.
        """
        self.target_domain = target_domain
        self.verified_session: Optional[Dict] = None
        self.rate_limited = False

    async def start_sniffing(self, page: Page):
        """
        Attaches listeners to the browser network stack.

        Args:
            page: Playwright Page object to monitor
        """
        logger.info(f"[Network] Network Sniffer Active. Auditing traffic for: {self.target_domain or'ALL'}")
        # Capture only on RESPONSE to ensure credentials actually work
        page.on("response", self._handle_response)

    async def _handle_response(self, response: Response):
        """
        Handle response events from the browser.

        Only processes successful responses (2xx) to ensure captured
        credentials are valid.

        Args:
            response: Playwright Response object
        """
        try:
            request = response.request
            url = response.url
            status = response.status

            # Filter: Domain Scope
            if self.target_domain and self.target_domain not in url:
                return

            # Filter: Resource Type (Data only)
            if request.resource_type not in ["xhr", "fetch"]:
                return

            # --- VALIDATION LOGIC ---
            # Only capture if server returns Success (2xx)
            if 200 <= status < 300:
                await self._capture_verified_request(request)

            # Check for Rate Limits
            elif status == 429:
                self.rate_limited = True
                logger.warning(f"🛑 Sniffer detected RATE LIMIT (429) on {url}")

        except Exception as e:
            logger.debug(f"Sniffer error: {e}")

    async def _capture_verified_request(self, request: Request):
        """
        Extracts DNA from a successful request.

        Implements case-insensitive header matching and robust payload parsing.

        Args:
            request: Playwright Request object from a successful response
        """
        raw_headers = request.headers
        method = request.method
        url = request.url
        post_data = None

        # 1. Header Extraction (Case-Insensitive)
        # We normalize everything to lowercase for checking, but keep original values
        captured_headers = {}
        target_keys = ["authorization", "x-api-key", "x-csrf-token", "x-xsrf-token", "cookie", "user-agent", "content-type"]

        for key, value in raw_headers.items():
            if key.lower() in target_keys:
                captured_headers[key] = value

        # 2. Payload Extraction (POST/PUT/PATCH)
        if method in ["POST", "PUT", "PATCH", "DELETE"]:
            try:
                # Playwright provides raw post data
                raw_payload = request.post_data
                if raw_payload:
                    try:
                        # Try parsing JSON first
                        post_data = json.loads(raw_payload)
                        logger.info(f"Captured JSON payload: {list(post_data.keys()) if isinstance(post_data, dict) else '<array>'}")
                    except json.JSONDecodeError:
                        # Fallback: Store raw string (for Form Data or GraphQL)
                        post_data = raw_payload
                        logger.debug(f"Captured raw payload (non-JSON): {len(raw_payload)} bytes")
            except Exception:
                pass  # Binary data or empty

        # 3. Store the Golden Ticket
        # We prioritize requests that have Auth headers
        has_auth = any(k.lower() == "authorization" for k in captured_headers)
        has_cookie = any(k.lower() == "cookie" for k in captured_headers)

        if has_auth or has_cookie:
            self.verified_session = {
                "url": url,
                "method": method,
                "headers": captured_headers,
                "payload": post_data
            }
            logger.info(f"🔓 Sniffer captured verified credentials for {method} {url}")

    def get_session_context(self) -> Optional[Dict]:
        """
        Returns the verified session data.

        Returns:
            Dictionary containing:
                - url: The API endpoint
                - method: HTTP method
                - headers: Authentication headers (case-preserved)
                - payload: Request body (JSON dict, raw string, or None)

            Returns None if no session was captured.
        """
        return self.verified_session


# Example usage (for testing)
if __name__ == "__main__":
    import asyncio
    from playwright.async_api import async_playwright

    async def demo():
        """Demo: Capture API request from a real site"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()

            # Initialize sniffer
            sniffer = NetworkSniffer(target_domain="api.github.com")
            await sniffer.start_sniffing(page)

            # Navigate to a page that makes API calls
            await page.goto("https://github.com/microsoft/playwright")
            await page.wait_for_timeout(5000)

            # Retrieve captured session
            session = sniffer.get_session_context()

            if session:
                print("\n" + "="*50)
                print("🔓 CAPTURED SESSION:")
                print("="*50)
                print(f"URL: {session['url']}")
                print(f"Method: {session['method']}")
                print(f"Headers: {list(session['headers'].keys())}")
                print(f"Payload: {session['payload']}")
                print("="*50)
            else:
                print("\nNo session captured")

            await browser.close()

    asyncio.run(demo())
