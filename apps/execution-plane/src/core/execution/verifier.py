"""Lightweight post-plan checks — no LLM; safe for SaaS defaults."""

from __future__ import annotations

from typing import Any


async def verify_selector_visible(
    page: Any,
    selector: str,
    *,
    timeout_ms: int = 3000,
) -> bool:
    """
    Returns True if the selector resolves to at least one visible node.
    Used for deterministic verification before/after critical interactions.
    """
    if not selector:
        return False
    try:
        loc = page.locator(selector).first
        return await loc.is_visible(timeout=timeout_ms)
    except Exception:
        return False
