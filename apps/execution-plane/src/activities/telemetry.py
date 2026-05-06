import logging
from playwright.async_api import Page

logger = logging.getLogger("activity")

async def capture_failure_screenshot(page: Page, job_id: str, error: Exception) -> bytes:
    """
    Captures screenshot on failure for debugging.

    Returns empty bytes if capture fails (never throws).
    """
    try:
        screenshot = await page.screenshot(
            type='jpeg',
            quality=60,
            full_page=False  # Current viewport only for speed
        )
        logger.info(f"[{job_id}] Failure screenshot captured ({len(screenshot)} bytes)")
        return screenshot
    except Exception as e:
        logger.warning(f"Failed to capture failure screenshot: {e}")
        return b""


