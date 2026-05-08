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
from typing import Dict, Optional, Any, List
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
        self.captured_responses = []

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
            if request.resource_type not in ["xhr", "fetch", "document"]:
                return

            # --- VALIDATION LOGIC ---
            # Only capture if server returns Success (2xx)
            if 200 <= status < 300:
                await self._capture_verified_request(request)

                # PHASE 3 FIX: Telemetry Firehose Filtering & GraphQL Hardening
                try:
                    content_type = response.headers.get("content-type", "")
                    lower_url = url.lower()
                    is_graphql = "/api/graphql" in lower_url or "graphql" in lower_url

                    if "application/json" in content_type.lower() or is_graphql:
                        try:
                            # Handle GraphQL payloads that might not strictly have application/json
                            try:
                                data = await response.json()
                            except Exception:
                                text_data = await response.text()
                                import json
                                data = json.loads(text_data)

                            if isinstance(data, (list, dict)):
                                data_str = str(data)
                                payload_size = len(data_str)

                                # Endpoint Heuristic (Drop obvious tracking endpoints)
                                if not is_graphql and any(k in lower_url for k in ['track', 'analytics', 'telemetry', 'beacon', 'metrics', 'log']):
                                    return

                                self.captured_responses.append({
                                    "url": url,
                                    "method": request.method,
                                    "data": data,
                                    "size": payload_size
                                })

                                # 3. Memory Cap (Keep top 20 largest payloads)
                                self.captured_responses.sort(key=lambda x: x["size"], reverse=True)
                                if len(self.captured_responses) > 20:
                                    self.captured_responses.pop()

                                logger.debug(f"[Network] Intercepted JSON Payload ({payload_size} bytes) from {url}")
                        except Exception:
                            pass # Body might be inaccessible or already consumed
                except Exception:
                    pass # Network error or response closed

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
        """Returns the verified auth session data."""
        return self.verified_session

    def get_captured_responses(self) -> List[Dict]:
        """Returns the list of intercepted JSON payloads."""
        return self.captured_responses


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
