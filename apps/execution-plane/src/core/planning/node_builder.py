"""
nodeBuilder.py — Workflow Node Construction Engine

PURPOSE:
  Converts a matched DOMElement + Intent into a fully-structured, execution-ready
  WorkflowNode that can be:
    1. Stored in the database exactly as-is
    2. Rendered in the React Flow canvas
    3. Replayed by the execution engine without any additional lookups

  This module ensures that every node leaving this system has:
    - A 5-tier cascading selector chain (most stable → least stable)
    - A structural fingerprint for SmartFinder self-healing
    - All required configuration fields pre-populated
    - Correct SCROLL information so the executor never re-scans the DOM

DESIGN PHILOSOPHY:
  Nodes are permanent artifacts. A user may store and re-run them months later.
  The selector chain is our insurance policy: if selector[0] breaks (CSS class changed),
  selector[1] will work. If that breaks, selector[2] will work. If ALL break,
  SmartFinder uses the fingerprint to find the element algorithmically.

  We NEVER store `data-quanta-id` as a permanent selector. `q-1` is a session ID
  that vanishes when the browser context closes. We compute and store REAL selectors
  before writing the node.

SELECTOR TIER ORDER:
  Tier 1 — Developer-provided stable attributes:
    data-testid, data-cy, data-qa (these don't change between deploys)
  Tier 2 — Stable semantic identifiers:
    #id (only if not auto-generated), name attribute, type+name combo
  Tier 3 — ARIA / accessible selectors:
    [aria-label="..."], [role="..."][aria-label="..."], [placeholder="..."]
  Tier 4 — Text content (fragile to i18n, copywriting changes):
    button:has-text("Submit"), a:has-text("Next")
  Tier 5 — Structural path (most fragile — last resort):
    XPath or structural CSS path (tag > tag > nth-child)
"""

import logging
import re
from typing import List, Optional, Dict, Any

from core.browser.dom_harvester import DOMElement
from core.planning.intent_parser import Intent

logger = logging.getLogger("nodeBuilder")


# ─── NODE SCHEMA ──────────────────────────────────────────────────────────────

# Mapping from action name → Quanta node type (matches the frontend registry)
ACTION_TO_NODE_TYPE: dict[str, str] = {
    "click":    "CLICK",
    "type":     "TYPE",
    "scrape":   "SCRAPE",
    "navigate": "NAVIGATE",
    "wait":     "WAIT",
    "select":   "SELECT",
    "check":    "CHECK",
    "scroll":   "SCROLL",
    "hover":    "HOVER",
    "log":      "LOG",
    "format":    "FORMAT_DATA",
    "transform": "TRANSFORM",
    "llm":       "LLM",
    "http":      "HTTP_REQUEST",
    "vision":    "VISION",
    "extract":   "EXTRACT",
}

# Node type → display category (used by frontend for grouping and color)
NODE_TYPE_CATEGORY: dict[str, str] = {
    "CLICK":    "browser",
    "TYPE":     "browser",
    "SCRAPE":   "browser",
    "NAVIGATE": "browser",
    "WAIT":     "logic",
    "SELECT":   "browser",
    "CHECK":    "browser",
    "SCROLL":   "browser",
    "HOVER":    "browser",
    "NOTE":     "utility",
    "TRIGGER":  "trigger",
    "MANUAL_FIX": "utility",
    "LOG":      "logic",
    "FORMAT_DATA":  "logic",
    "TRANSFORM":    "logic",
    "LLM":          "ai",
    "VISION":       "ai",
    "EXTRACT":      "ai",
    "HTTP_REQUEST": "network",
}

# X layout constants (nodes spaced evenly on canvas)
NODE_X_START   = 150
NODE_X_SPACING = 300
NODE_Y          = 300


# ─── NODE BUILDER ─────────────────────────────────────────────────────────────

class NodeBuilder:
    """
    Converts a matched element + intent pair into a fully-structured WorkflowNode.

    Usage:
        builder = NodeBuilder()
        node = builder.build(intent, matchResult.element, position)
    """

    def build(
        self,
        intent: Intent,
        element: Optional[DOMElement],
        stepIndex: int,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """
        Builds a complete workflow node.

        Args:
            intent:     The parsed Intent for this step.
            element:    The matched DOMElement (None for navigate/wait/scroll).
            stepIndex:  Used to compute position on the canvas.
            confidence: The ElementMatcher confidence (stored in node for inspection).

        Returns:
            A plain dict matching the frontend WorkflowNode schema.
        """
        nodeId   = f"node-{stepIndex}"
        nodeType = ACTION_TO_NODE_TYPE.get(intent.action, "CLICK")
        position = self._computePosition(stepIndex)

        # ── Build the config depending on node type ────────────────────────
        config = self._buildConfig(intent, element, nodeType)

        # ── Build the label ────────────────────────────────────────────────
        label = self._buildLabel(intent)

        # ── Build fingerprint (for SmartFinder Layer 1 self-healing) ───────
        fingerprint = self._buildFingerprint(element)

        return {
            "id":       nodeId,
            "type":     nodeType,
            "position": position,
            "data": {
                "label":      label,
                "nodeType":   nodeType,
                "category":   NODE_TYPE_CATEGORY.get(nodeType, "browser"),
                "config":     config,
                "confidence": round(confidence, 2),
                "verified":   confidence >= 0.75,
                "qualifier":  intent.qualifier,
                "fingerprint":fingerprint,
                # Handle connections — IDs MUST match frontend BaseNode/StudioNode
                "inputs":  [{"id": "input",  "label": "Input", "dataType": "trigger"}],
                "outputs": [{"id": "output", "label": "Output",    "dataType": "trigger"}],
            },
        }

    def buildTriggerNode(self, url: str, triggerType: str = "MANUAL", cron: str = None) -> dict[str, Any]:
        """
        Creates the mandatory TRIGGER node that starts every workflow.
        This is always node index 0, positioned at the far left.
        """
        node_type = "SCHEDULE_TRIGGER" if triggerType == "SCHEDULE" else "TRIGGER"
        if url and not url.startswith(("http://", "https://", "file://", "data:")):
            url = "https://" + url

        config = {
            "triggerType": triggerType,
            "targetUrl":   url,
        }
        if cron:
            config["cronExpression"] = cron

        return {
            "id":   "trigger-1",
            "type": node_type,
            "position": {"x": 0, "y": NODE_Y},
            "data": {
                "label":    "Start Workflow" if triggerType == "MANUAL" else f"Scheduled: {cron}",
                "nodeType": node_type,
                "category": "trigger",
                "config": config,
                "verified": True,
                "confidence": 1.0,
                "fingerprint": {},
                "inputs":  [],
                "outputs": [{"id": "output", "label": "Output", "dataType": "trigger"}],
            },
        }

    def buildManualFixNode(
        self,
        stepIndex: int,
        action: str,
        intent: str,
        errorMessage: str,
    ) -> dict[str, Any]:
        """
        Creates a MANUAL_FIX placeholder node for steps where matching failed.
        The user sees this in the canvas and can override the selector manually.
        """
        position = self._computePosition(stepIndex)
        return {
            "id":   f"node-{stepIndex}",
            "type": "MANUAL_FIX",
            "position": position,
            "data": {
                "label":    f"⚠ Fix Needed: {intent[:30]}",
                "nodeType": "MANUAL_FIX",
                "category": "utility",
                "config": {
                    "action":   action,
                    "intent":   intent,
                    "error":    errorMessage,
                    "selector": "",   # User must fill this in
                },
                "verified": False,
                "confidence": 0.0,
                "fingerprint": {},
                "inputs":  [{"id": "input",  "label": "Input", "dataType": "trigger"}],
                "outputs": [{"id": "output", "label": "Output",    "dataType": "trigger"}],
            },
        }

    def buildEdge(self, sourceId: str, targetId: str) -> dict[str, Any]:
        """Creates a directed edge between two nodes."""
        return {
            "id":      f"e-{sourceId}-{targetId}",
            "source":  sourceId,
            "target":  targetId,
            "type":    "default",
            "animated": True,
        }

    # ── PRIVATE ────────────────────────────────────────────────────────────

    def _buildConfig(
        self,
        intent: Intent,
        element: Optional[DOMElement],
        nodeType: str,
    ) -> dict[str, Any]:
        """
        Builds the node's runtime configuration.
        Everything the execution engine needs to replay this step is stored here.
        """
        config: dict[str, Any] = {
            "intent": intent.targetDescription,
        }

        # ── Navigation: URL only, no element needed ────────────────────────
        if nodeType == "NAVIGATE":
            url = intent.value or intent.targetDescription
            if url and not url.startswith(("http://", "https://", "file://", "data:")):
                # Handle bare domains like amazon.com or bbc.com
                url = "https://" + url
            config["url"] = url
            return config

        # ── Wait: duration only, no element needed ─────────────────────────
        if nodeType == "WAIT":
            config["duration"] = int(intent.value or "2000")
            return config

        # ── Scroll: direction + optional pixels ───────────────────────────
        if nodeType == "SCROLL":
            config["direction"] = "down" if "up" not in intent.rawSentence.lower() else "up"
            config["pixels"]    = 600   # Sensible default — one viewport height
            return config

        # ── Log: message content (supports variables) ──────────────────────
        if nodeType == "LOG":
            config["message"] = intent.targetDescription
            return config

        # ── Format Data: input + format ────────────────────────────────────
        if nodeType == "FORMAT_DATA":
            config["inputData"] = "{{ previousNode.data }}"  # Smart default
            # Extract format from target
            target_lower = intent.targetDescription.lower()
            if "csv" in target_lower: config["format"] = "csv"
            elif "json" in target_lower: config["format"] = "json"
            elif "table" in target_lower or "html" in target_lower: config["format"] = "html_table"
            else: config["format"] = "json"
            config["includeHeader"] = True
            return config

        # ── Transform: manual JS logic ─────────────────────────────────────
        if nodeType == "TRANSFORM":
            config["language"]   = "javascript"
            config["expression"] = intent.value or f"return data;"
            return config

        # ── LLM: analysis node ─────────────────────────────────────────────
        if nodeType == "LLM":
            config["provider"]     = "openai"
            config["model"]        = "gpt-4o"
            config["prompt"]       = intent.targetDescription
            config["systemPrompt"] = "You are a helpful data analyst assistant. Analyze the incoming data according to the user's request."
            # Chain from previous node by default if it was a scrape
            config["input"]        = "{{ previousNode.data }}"
            return config

        # ── HTTP Request: dynamic network node ────────────────────────────
        if nodeType == "HTTP_REQUEST":
            config["url"]     = intent.value or intent.targetDescription or "https://hook.us1.make.com/..."
            config["method"]  = "POST"
            config["headers"] = "{\n  \"Content-Type\": \"application/json\"\n}"
            config["body"]    = "{\n  \"data\": \"{{ previousNode.data }}\"\n}"
            return config

        # ── Vision: analyze screenshot ─────────────────────────────────────
        if nodeType == "VISION":
            config["provider"]    = "openai"
            config["model"]       = "gpt-4o"
            config["prompt"]      = intent.targetDescription
            config["imageSource"] = "screenshot"
            return config

        # ── AI Extract: structured extraction ──────────────────────────────
        if nodeType == "EXTRACT":
            config["provider"] = "openai"
            config["input"]    = "{{ previousNode.data }}"
            config["prompt"]   = intent.targetDescription
            config["schema"]   = "{\n  \"result\": \"string\"\n}"
            return config

        # ── All element-based nodes: need selector chain + position info ───
        if element:
            selectorChain = self._buildSelectorChain(element)
            config.update({
                "selector":       selectorChain[0] if selectorChain else "",
                "selectorChain":  selectorChain,         # Full fallback list for executor
                "scrollY":        element.scrollY,       # Absolute Y — executor scrolls here first
                "scrollX":        element.scrollX,
                "inIframe":       element.inIframe,
                "iframeIndex":    element.iframeIndex,
                "inShadowDom":    element.inShadowDom,
                "elementTag":     element.tag,
                "elementText":    (element.text or "")[:60],
            })

            # TYPE: include the text to type
            if nodeType == "TYPE":
                config["value"] = intent.value or ""

            # SCRAPE: handle multiple and limit from qualifier
            if nodeType == "SCRAPE":
                qual = intent.qualifier or ""
                if qual == "all" or qual.startswith("limit:"):
                    config["multiple"] = True
                    if qual.startswith("limit:"):
                        config["limit"] = int(qual.split(":")[1])
                else:
                    config["multiple"] = False

            # SELECT: include the option value/text to select
            if nodeType == "SELECT":
                config["optionValue"] = intent.value or intent.targetDescription

            # CHECK: store the expected checked state
            if nodeType == "CHECK":
                config["checked"] = "uncheck" not in intent.rawSentence.lower()

        config["qualifier"] = intent.qualifier
        return config

    def _buildSelectorChain(self, el: DOMElement) -> list[str]:
        """
        Generates an ordered selector chain from most stable to least stable.
        The execution engine tries each selector in sequence until one works.

        Tier 1 — Developer-written stable attributes (survive deploys)
        Tier 2 — Stable semantic identifiers (survive minor refactors)
        Tier 3 — ARIA / accessible selectors (survive non-copy changes)
        Tier 4 — Text-content selectors (fragile to copy changes)
        Tier 5 — Name / type structural: last resort
        """
        chain: list[str] = []

        # ── Tier 1: Developer-provided stable attributes ───────────────────
        if el.dataTestId:
            chain.append(f"[data-testid='{self._esc(el.dataTestId)}']")

        # ── Tier 2: Stable semantic identifiers ───────────────────────────
        if el.id and not self._isAutoId(el.id):
            chain.append(f"#{self._esc(el.id)}")

        if el.name:
            chain.append(f"{el.tag}[name='{self._esc(el.name)}']")

        # type + name combo for form inputs (very stable)
        if el.type and el.name:
            chain.append(f"input[type='{el.type}'][name='{self._esc(el.name)}']")

        # ── Tier 3: ARIA / Accessible selectors ─────────────────────────
        if el.ariaLabel:
            chain.append(f"[aria-label='{self._esc(el.ariaLabel)}']")
        if el.role:
            if el.ariaLabel:
                chain.append(f"[role='{el.role}'][aria-label='{self._esc(el.ariaLabel)}']")
            else:
                chain.append(f"[role='{el.role}']")
        if el.placeholder:
            chain.append(f"{el.tag}[placeholder='{self._esc(el.placeholder)}']")
        if el.type and el.type in ("email", "password", "search", "tel", "url", "number"):
            chain.append(f"input[type='{el.type}']")

        # ── Tier 4: Text-content selectors (fragile but human-readable) ───
        if el.text and len(el.text.strip()) > 0 and len(el.text) <= 50:
            # Strip invisible whitespace and escape quotes
            safeText = el.text.strip()[:40].replace("'", "\\'")
            chain.append(f"{el.tag}:has-text('{safeText}')")

        if el.href and len(el.href) < 100:
            # Use relative path only (avoids domain-specific breakage)
            hrefPath = re.sub(r"^https?://[^/]+", "", el.href)
            if hrefPath:
                chain.append(f"a[href='{self._esc(hrefPath)}']")
            else:
                chain.append(f"a[href='{self._esc(el.href)}']")

        # ── Tier 5: Structural / type-only fallbacks ──────────────────────
        if el.type and not any(f"type='{el.type}'" in s for s in chain):
            chain.append(f"{el.tag}[type='{el.type}']")

        # ── Guarantee at minimum one selector exists ───────────────────────
        if not chain:
            chain.append(el.tag)   # Bare tag — matches first element of that type

        return chain

    def _buildFingerprint(self, el: Optional[DOMElement]) -> dict[str, Any]:
        """
        Builds a structural fingerprint for SmartFinder Layer 1 self-healing.
        Stored in the node so that if ALL selectors fail, SmartFinder can
        re-find the element by its structural + semantic signature.
        """
        if not el:
            return {}

        # Compute a short SimHash-compatible string
        signatureStr = "|".join(filter(None, [
            el.tag,
            el.type or "",
            el.role or "",
            (el.ariaLabel or el.placeholder or el.text or "")[:30],
        ])).lower()

        return {
            "signatureStr":  signatureStr,
            "tag":           el.tag,
            "type":          el.type,
            "role":          el.role,
            "ariaLabel":     el.ariaLabel,
            "textPrefix":    (el.text or "")[:25],
            "zone":          el.zone,
        }

    def _buildLabel(self, intent: Intent) -> str:
        """Creates a human-readable node label (max 38 chars)."""
        target = intent.targetDescription
        label  = f"{intent.action.capitalize()} {target}"

        if intent.value and intent.action == "type":
            label = f"Type '{intent.value[:20]}' in {target}"
        elif intent.action == "navigate":
            label = f"Go to {intent.value or target}"
        elif intent.action == "scrape":
            label = f"Scrape {target}"

        return label[:38] + "..." if len(label) > 41 else label

    @staticmethod
    def _computePosition(stepIndex: int) -> dict[str, int]:
        """Calculates the canvas position for a node based on its step index."""
        return {
            "x": NODE_X_START + stepIndex * NODE_X_SPACING,
            "y": NODE_Y,
        }

    @staticmethod
    def _esc(value: str) -> str:
        """Escapes single quotes in attribute selector values."""
        return value.replace("'", "\\'") if value else ""

    @staticmethod
    def _isAutoId(elementId: str) -> bool:
        """
        Returns True for auto-generated IDs that are not stable across renders.
        React, Radix, HeadlessUI, and similar frameworks generate IDs like
        ':r0:', 'radix-:r12:', 'headlessui-listbox-1744'.
        """
        unstablePatterns = [
            r"^:r\d+:$",
            r"^radix-",
            r"^headlessui-",
            r"^react-",
            r"^\d+$",
            r"^[a-f0-9]{8}-[a-f0-9]{4}",   # UUID
        ]
        for pattern in unstablePatterns:
            if re.match(pattern, elementId, re.IGNORECASE):
                return True
        return False
