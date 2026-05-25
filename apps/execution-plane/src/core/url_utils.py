# core/url_utils.py
"""
URL pre-resolution utility for Playwright compatibility.

Headless Chromium inside Docker can crash with
``net::ERR_SOCKET_NOT_CONNECTED`` when a bare domain triggers a TLS
redirect (e.g. https://amazon.com → https://www.amazon.com). By
following redirects with httpx first, we hand Playwright the *final*
URL so Chromium never has to handle the redirect itself.
"""

import logging

import httpx

logger = logging.getLogger("urlUtils")


async def resolve_final_url(url: str, timeout: float = 10.0) -> str:
    """Follow HTTP/TLS redirects and return the terminal URL.

    Falls back to the original URL if the HEAD request fails for any
    reason (timeouts, DNS errors, etc.) so the pipeline is never
    blocked by this safety layer.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            verify=False,  # some targets have mismatched certs on bare domains
        ) as client:
            resp = await client.head(url)
            final = str(resp.url)
            if final != url:
                logger.info(f"[URLResolve] {url} -> {final}")
            return final
    except Exception as exc:
        logger.warning(
            f"[URLResolve] Could not pre-resolve {url}: {exc} -- using original"
        )
        return url
