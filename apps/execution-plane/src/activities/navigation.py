import os
import asyncio
import logging
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Page, ElementHandle
from playwright.async_api import TimeoutError as PlaywrightTimeout
from core.selector.smart_finder import SmartFinder

logger = logging.getLogger("activity")

NAVIGATION_TIMEOUT = int(os.getenv("NAVIGATION_TIMEOUT_MS", "30000"))
NETWORK_IDLE_TIMEOUT = int(os.getenv("NETWORK_IDLE_TIMEOUT_MS", "5000"))
CLICK_RETRY_ATTEMPTS = int(os.getenv("CLICK_RETRY_ATTEMPTS", "3"))
CLICK_RETRY_DELAY_MS = int(os.getenv("CLICK_RETRY_DELAY_MS", "500"))

@asynccontextmanager
async def safe_browser_context(playwright_instance, launch_args, storage_state=None):
    """
    Guarantees browser and context closure even on catastrophic crashes.
    Prevents zombie Chromium processes and memory leaks.
    """
    # TASK 8: Visual Demo Mode for FYP2 Video
    # We disable headless mode and add slow_mo to allow the AI manipulation 
    # to be visually recorded on the host machine.
    browser = await playwright_instance.chromium.launch(
        headless=False,
        slow_mo=800,
        args=["--start-maximized"]
    )
    context = await browser.new_context(storage_state=storage_state)
    page = await context.new_page()
    try:
        # We yield browser, context, and page to allow explicit closure in exception handlers
        yield browser, context, page
    finally:
        await context.close()
        await browser.close()


async def click_with_retry(
    finder: SmartFinder,
    page: Page,
    intent: str,
    job_id: str,
    max_attempts: int = CLICK_RETRY_ATTEMPTS
) -> None:
    """
    Robust click with automatic retry and re-find on failure.

    Handles:
    - Stale element references
    - Elements that become detached during animation
    - Transient overlays that disappear

    Args:
        finder: SmartFinder instance
        page: Playwright page
        intent: What to click (e.g., "submit button")
        job_id: For logging
        max_attempts: Maximum retry attempts

    Raises:
        Exception: If all attempts fail
    """
    last_error = None

    for attempt in range(max_attempts):
        try:
            # Re-find element on each attempt (handles stale references)
            result = await finder.find(intent, timeout=10000)
            if not result.found:
                raise Exception(f"Element not found: {intent}")

            element = result.element

            # Scroll into view to ensure visibility
            await element.scroll_into_view_if_needed()
            await asyncio.sleep(0.1)  # Brief pause for scroll animation

            # Attempt click
            await element.click(timeout=5000)

            logger.info(f"[{job_id}] Click successful: '{intent}' (attempt {attempt + 1})")
            return

        except Exception as e:
            last_error = e
            logger.warning(
                f"[{job_id}] Click failed (attempt {attempt + 1}/{max_attempts}): {e}"
            )

            if attempt < max_attempts - 1:
                # Wait before retry
                await asyncio.sleep(CLICK_RETRY_DELAY_MS / 1000)

                # Try to dismiss any popups/overlays that might be blocking
                await dismiss_overlays(page)

    # All attempts failed
    raise Exception(
        f"Click failed after {max_attempts} attempts on '{intent}': {last_error}"
    )


async def dismiss_overlays(page: Page) -> None:
    """
    Attempts to dismiss common UI overlays that block interactions.

    Handles:
    - Cookie consent banners
    - Newsletter popups
    - Modal dialogs with close buttons
    - Push notification prompts
    """
    DISMISS_SELECTORS = [
        # Cookie banners
        "[class*='cookie'] button[class*='accept']",
        "[class*='cookie'] button[class*='close']",
        "[id*='cookie'] button[class*='accept']",
        "#onetrust-accept-btn-handler",
        ".cc-btn.cc-dismiss",

        # Generic close buttons
        "[class*='modal'] [class*='close']",
        "[class*='popup'] [class*='close']",
        "[class*='overlay'] [class*='close']",
        "button[aria-label='Close']",
        "button[aria-label='Dismiss']",

        # Newsletter popups
        "[class*='newsletter'] [class*='close']",
        "[class*='subscribe'] [class*='close']",
    ]

    for selector in DISMISS_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element and await element.is_visible():
                await element.click(timeout=1000)
                logger.debug(f"[Overlay] Dismissed: {selector}")
                await asyncio.sleep(0.3)  # Wait for animation
                return  # One dismissal per call is enough
        except:
            continue  # Try next selector


async def safe_wait_for_network_idle(page: Page, timeout_ms: int = NETWORK_IDLE_TIMEOUT) -> None:
    """
    Waits for network idle state with proper exception handling.

    Only catches timeout - re-raises critical errors like page closed.
    """
    try:
        # TASK 7 FIX: networkidle is unreliable for hydration on heavy SPAs.
        # We wait for the initial load and then allow for GraphQL/XHR hydration.
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        
        # Semantic Latch: Allow the page to physically hydrate with actual feed data.
        await asyncio.sleep(2.0)
    except PlaywrightTimeout:
        logger.debug("Navigation load timeout (expected for some slow-loading sites)")
    except Exception as e:
        error_str = str(e).lower()
        if "closed" in error_str or "crashed" in error_str:
            raise  # Critical error - re-raise
        logger.warning(f"Unexpected network wait error: {e}")


def get_proxy_config(region="us"):
    """
    Constructs the Proxy dictionary for Playwright.
    Supports BrightData / Smartproxy / IPRoyal formats.

    TASK 5 FIX: Returns None if PROXY_SERVER is not configured.
    This allows running without a proxy provider to save costs.

    Returns:
        dict: Playwright proxy config, or None if proxy is not configured
    """
    server = os.getenv("PROXY_SERVER")  # e.g., "http://brd.superproxy.io:22225"

    # TASK 5 FIX: If no proxy server configured, return None (don't crash)
    if not server:
        logger.info("[Proxy] PROXY_SERVER not set - running without proxy")
        return None

    username = os.getenv("PROXY_USER", "")
    password = os.getenv("PROXY_PASSWORD", "")

    # If server is set but credentials are missing, log warning but still try
    # Some proxies (like SOCKS5) don't require auth
    if not username:
        logger.warning("[Proxy] PROXY_USER not set - attempting connection without auth")
        return {
            "server": server
        }

    # Full proxy config with region support
    return {
        "server": server,
        "username": f"{username}-country-{region}",  # Most providers use this format
        "password": password
    }


def is_proxy_available() -> bool:
    """
    Check if proxy is configured.

    Returns:
        bool: True if PROXY_SERVER environment variable is set
    """
    return bool(os.getenv("PROXY_SERVER"))


