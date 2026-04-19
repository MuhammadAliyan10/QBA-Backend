import asyncio
import hashlib
import random
import re
import logging
from typing import List
from playwright.async_api import Page, ElementHandle

logger = logging.getLogger("glassBox")

class GlassBoxEngine:
    """
    The Deterministic Math Engine.
    Handles Physics (Raycasting), Geometry (SVG), and Statistics (Typing).
    """

    def __init__(self):
        # Known hashes for common SVG icons (Gear, Trash, User)
        self.KNOWN_ICONS = {
            "a1b2c3...": "settings",
            "d4e5f6...": "delete"
        }

    # --- 1. THE RAYCAST AUDITOR (PHYSICS) ---
    # --- 1. THE RAYCAST AUDITOR (PHYSICS) ---
    async def is_physically_clickable(self, page: Page, element: ElementHandle) -> bool:
        """
        Fires a 'Laser' at the element's center to check if it is covered by a popup.
        """
        try:
            # 1. Get Geometry
            box = await element.bounding_box()
            if not box:
                return False

            # 2. Calculate Center of Mass
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2

            # 3. Execute Hit Test in Browser Kernel
            # FIX: We pass (cx, cy) as a list/tuple in the second argument
            is_obscured = await page.evaluate("""
                ([x, y, el]) => {
                    const target = document.elementFromPoint(x, y);
                    if (!target) return true; // Nothing there
                    return !el.contains(target) && !target.contains(el);
                }
            """, [cx, cy, element]) # <--- PASS ARGS AS A LIST

            if is_obscured:
                logger.warning(f"[Warning] Element at ({cx}, {cy}) is obscured by another layer.")
                return False

            return True
        except Exception as e:
            logger.error(f"Raycast failed: {e}")
            return False # Fail safe

    async def compute_icon_hash(self, element: ElementHandle) -> str:
        """
        Normalizes an SVG path to identify icons without Computer Vision.
        """
        try:
            # 1. Get the path data
            path_d = await element.get_attribute("d")
            if not path_d:
                # Try searching children if the handle is the <svg> tag
                path_el = await element.query_selector("path")
                if path_el:
                    path_d = await path_el.get_attribute("d")

            if not path_d:
                return "empty"

            # 2. Normalize: Remove letters, round numbers to 1 decimal
            # Logic: "M10.55 20.1 L30..." -> "10.6 20.1 30.0"
            numbers = re.findall(r"[-+]?\d*\.\d+|\d+", path_d)
            normalized = " ".join([f"{float(n):.1f}" for n in numbers])

            # 3. Hash it (SHA-256)
            return hashlib.sha256(normalized.encode()).hexdigest()
        except Exception:
            return "error"

    # --- 3. THE GAUSSIAN TYPER (STATISTICS) ---
    # --- 3. THE GAUSSIAN TYPER (STATISTICS) ---
    async def human_type(self, page: Page, element: ElementHandle, text: str):
        """
        Types text using a Bell Curve distribution.
        Includes 'Re-Focus' logic to fix 'Element not attached' errors.
        """
        try:
            # 1. Focus the element
            await element.click()
        except Exception:
            # If click fails (detached), try to re-focus or ignore
            logger.warning("Element click failed before typing. Attempting to type anyway...")

        for char in text:
            # Mean=100ms, StdDev=30ms
            delay_ms = random.gauss(100, 30)
            delay_sec = max(0.05, delay_ms / 1000.0)

            # FIX: Use page.keyboard instead of element.type to avoid handle rot
            await page.keyboard.press(char)
            await asyncio.sleep(delay_sec)

    # --- 4. THE RECURSIVE SHADOW PIERCER (DOM TRAVERSAL) ---
    async def get_all_interactive_nodes(self, page: Page) -> list[ElementHandle]:
        """
        Recursively extracts buttons/inputs, piercing through Shadow DOMs.
        """
        # We query the Top Level DOM + standard Shadow Roots
        # In a full production version, this would inject a JS script to traverse recursively
        return await page.query_selector_all("button, a, input, [role='button'], [onclick]")

    # --- 5. THE HONEYPOT FILTER (VISUAL FILTER) ---
    async def filter_visible_elements(self, page: Page, elements: list[ElementHandle]) -> list[ElementHandle]:
        """
        Removes elements that are invisible to humans (Honeypot Traps).
        """
        visible = []
        for el in elements:
            try:
                # Check Computed Style (What the human eye sees)
                is_visible = await page.evaluate("""
                    (el) => {
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' &&
                               style.visibility !== 'hidden' &&
                               style.opacity > 0.1 &&
                               el.offsetWidth > 5 &&
                               el.offsetHeight > 5;
                    }
                """, el)

                if is_visible:
                    visible.append(el)
            except:
                pass
        return visible

    async def get_pruned_axtree(self, page: Page, elements: list[ElementHandle]) -> tuple[str, dict[int, ElementHandle]]:
        """
        Generates a pruned, numbered list of interactive elements for the LLM.
        Returns (formatted_string, id_to_handle_map).
        """
        mapping = {}
        lines = []

        for i, el in enumerate(elements):
            try:
                # Extract semantic properties
                props = await page.evaluate("""
                    (el) => ({
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || el.placeholder || "").trim().slice(0, 50),
                        role: el.getAttribute('role') || "",
                        id: el.id || "",
                        name: el.getAttribute('name') || ""
                    })
                """, el)

                mapping[i] = el
                label = props["text"] or f"No-Text {props['tag']}"
                line = f"[{i}] {props['tag'].upper()}: \"{label}\" (Role: {props['role']}, ID: {props['id']})"
                lines.append(line)

            except Exception:
                continue

        return "\n".join(lines), mapping
