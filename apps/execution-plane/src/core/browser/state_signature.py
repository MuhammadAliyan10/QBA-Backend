"""
stateSignature.py - SPA-Safe State Signature Generator

Calculates robust hashes characterizing a page's DOM state, filtering out
meaningless dynamic data (times, ads) and focusing on layout and interactive
surface. Addresses single-page app (SPA) DOM drift.
"""

import hashlib
import json
import logging
from typing import Dict, Any, List
from playwright.async_api import Page

logger = logging.getLogger("state_signature")

class StateSignatureGenerator:
    """
    Generates a deterministic hash representing the 'semantic structure' of a page.
    """

    @staticmethod
    async def generate(page: Page) -> str:
        """
        Creates a compound signature hash from:
        1. Base URL (stripped of volatile query params)
        2. Accessible Tree interactive roles structure
        3. Dialog/Modal footprints
        """
        try:
            raw_url = page.url
            # Strip highly volatile parameters like session IDs
            clean_url = raw_url.split('?')[0]

            # Fetch minimal Accessibility Tree for interactive footprints
            # We evaluate JS to fetch a highly pruned sequence of roles to prevent CDP overload
            signature_data = await page.evaluate("""() => {
                const getRoles = (root) => {
                    let seq = [];
                    // Capture modals distinctly
                    const dialogs = document.querySelectorAll('dialog, [role="dialog"], .modal, .Modal');
                    const hasModal = dialogs.length > 0;

                    // Simple traversal of major semantic containers
                    const containers = document.querySelectorAll('main, nav, form, table, [role="main"], [role="navigation"]');
                    for (let c of containers) {
                        seq.push(c.tagName + (c.getAttribute('role') ? '#' + c.getAttribute('role') : ''));
                    }

                    return {
                        title: document.title,
                        hasModal: hasModal,
                        containers: seq.sort()
                    };
                };
                return getRoles(document.body);
            }""")

            # Create a deterministic footprint dictionary
            footprint = {
                "base_url": clean_url,
                "title": signature_data.get("title", ""),
                "modal_active": signature_data.get("hasModal", False),
                "structural_sequence": signature_data.get("containers", [])
            }

            # MD5 hash for keying
            footprint_str = json.dumps(footprint, sort_keys=True)
            return hashlib.md5(footprint_str.encode('utf-8')).hexdigest()

        except Exception as e:
            logger.error(f"Failed to generate state signature: {e}")
            return "unknown_state_signature"

    @staticmethod
    def calculate_drift(sig_a: str, sig_b: str) -> bool:
        """
        Determines if two signatures are considered divergent enough to invalidate caches.
        For exact match hashes, an inequality indicates a mismatch.
        In advanced implementations, this would perform a bounded Levenshtein ratio on sequences.
        """
        return sig_a != sig_b
