# core/planning/harvester.py
"""
Site Harvester v2.1 — Aggressive Semantic Pruning & JIT Epoch Harvesting.

This module implements the "8k Pruning Mandate" to ensure high-fidelity planning 
within strict token constraints. It uses a browser-resident JavaScript engine to 
flatten the DOM and pierce Shadow Roots in a single read-only pass.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from playwright.async_api import Page

logger = logging.getLogger("harvester")

# =============================================================================
# HARVEST_JS — The Browser-Resident Pruning Engine
# =============================================================================

HARVEST_JS = """
() => {
    function buildSemanticMap(node) {
        // --- Phase 1: Blacklist & Visibility ---
        if (node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName.toUpperCase();
            if (['SCRIPT', 'STYLE', 'META', 'NOSCRIPT'].includes(tag)) return null;

            // Performance Guard: Check visibility once
            const style = window.getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden' || node.getAttribute('aria-hidden') === 'true') {
                return null;
            }

            // SVG Handling
            if (tag === 'SVG') {
                return {
                    type: 'ICON',
                    label: node.getAttribute('aria-label') || node.getAttribute('title') || 'unlabeled icon'
                };
            }

            // iFrame Handling (CORS Safe)
            if (tag === 'IFRAME') {
                return {
                    type: 'FRAME',
                    id: node.id || '',
                    src: node.src || ''
                };
            }
        }

        // --- Phase 2: Text Nodes ---
        if (node.nodeType === Node.TEXT_NODE) {
            const text = node.textContent.trim();
            return text ? text.substring(0, 150) : null;
        }

        if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) {
            return null;
        }

        // --- Phase 3: Shadow DOM & Children ---
        let children = [];
        const targetNodes = node.shadowRoot ? node.shadowRoot.childNodes : node.childNodes;

        for (const childNode of targetNodes) {
            const mapped = buildSemanticMap(childNode);
            if (mapped !== null) {
                if (Array.isArray(mapped)) {
                    children.push(...mapped);
                } else {
                    children.push(mapped);
                }
            }
        }

        if (children.length === 0) return null;

        // --- Phase 4: Single-Child Collapse ---
        const tag = node.tagName ? node.tagName.toUpperCase() : '';
        const isWrapper = tag === 'DIV' || tag === 'SPAN';
        const hasSemanticAttr = node.id || node.className || node.role || 
                               Array.from(node.attributes || []).some(a => a.name.startsWith('aria-'));

        if (isWrapper && !hasSemanticAttr && children.length === 1) {
            return children[0];
        }

        // Return element with its properties
        return {
            tag: tag,
            role: node.getAttribute ? node.getAttribute('role') : '',
            id: node.id || '',
            text: typeof children[0] === 'string' && children.length === 1 ? children[0] : undefined,
            children: typeof children[0] === 'string' && children.length === 1 ? undefined : children
        };
    }

    return buildSemanticMap(document.body);
}
"""

# =============================================================================
# PYTHON WRAPPER
# =============================================================================

async def harvest_context(page: Page) -> Dict[str, Any]:
    """
    Executes the aggressive pruning HARVEST_JS script on the live page.
    Returns a flattened, semantic JSON map of the DOM.
    """
    start_time = time.time()
    logger.info(f"[Harvester] Starting semantic harvest on {page.url}")

    try:
        # Inject and execute the JS pruning engine
        semantic_map = await page.evaluate(HARVEST_JS)
        
        duration = (time.time() - start_time) * 1000
        logger.info(f"[Harvester] Semantic harvest complete in {duration:.2f}ms")
        
        return {
            "url": page.url,
            "title": await page.title(),
            "timestamp": time.time(),
            "dom_map": semantic_map,
            "metrics": {
                "duration_ms": duration
            }
        }

    except Exception as e:
        logger.error(f"[Harvester] Critical failure during JS evaluation: {str(e)}")
        return {
            "url": page.url,
            "error": str(e),
            "dom_map": None
        }
