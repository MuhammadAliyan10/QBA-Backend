"""
Recipe Converter — Transforms React Flow graph → executable step array.

The frontend editor stores workflows as {nodes[], edges[]} in React Flow format.
The Python browser_automation_activity expects a flat steps[{action, params}] array.
This module bridges the two formats.

Algorithm:
  1. Build adjacency list from edges
  2. Find trigger/start node
  3. Topological sort (BFS from trigger, respecting edge order)
  4. Map each node's type → activity action name
  5. Map each node's data.config → step params
  6. Return ordered list of {action, params, node_id}
"""

import logging
from typing import Any
from collections import deque

logger = logging.getLogger("recipe_converter")

# =============================================================================
# NODE TYPE → ACTION MAPPING
# =============================================================================
# Maps React Flow node types (frontend) to activity action names (Python worker).
# Only include types that have browser-executable actions.

NODE_TYPE_TO_ACTION: dict[str, str] = {
    # Triggers (mark start of execution, treated as GOTO if they have a URL)
    "TRIGGER": "GOTO",
    "SCHEDULE_TRIGGER": "GOTO",
    "WEBHOOK_TRIGGER": "GOTO",

    # Navigation
    "GOTO": "GOTO",
    "NAVIGATE": "GOTO",
    "NEW_TAB": "NEW_TAB",
    "RELOAD": "RELOAD",
    "GO_BACK": "GO_BACK",
    "GO_FORWARD": "GO_FORWARD",
    "CLOSE_TAB": "CLOSE_TAB",
    "SWITCH_TAB": "SWITCH_TAB",

    # Mouse Actions
    "CLICK": "CLICK",
    "DOUBLE_CLICK": "DOUBLE_CLICK",
    "RIGHT_CLICK": "RIGHT_CLICK",
    "HOVER": "HOVER",
    "DRAG_DROP": "DRAG_AND_DROP",
    "FOCUS": "FOCUS",

    # Input Actions
    "TYPE": "TYPE",
    "FILL": "TYPE",
    "CLEAR": "CLEAR",
    "CHECK": "CHECK",
    "SELECT": "SELECT",
    "SUBMIT": "SUBMIT",
    "PRESS_KEY": "PRESS_KEY",
    "UPLOAD": "UPLOAD_FILE",

    # Data Extraction
    "SCRAPE": "EXTRACT",
    "EXTRACT": "EXTRACT",
    "GET_TEXT": "EXTRACT",
    "GET_ATTRIBUTE": "EXTRACT",
    "SCREENSHOT": "SCREENSHOT",

    # Waiting & Scrolling
    "WAIT": "WAIT_FOR",
    "SCROLL": "SCROLL",

    # Downloads
    "DOWNLOAD": "DOWNLOAD",

    # Advanced Browser
    "EVALUATE": "EVALUATE",
    "COOKIES": "COOKIES",
    "SET_VIEWPORT": "SET_VIEWPORT",
    "PDF": "PDF",
    "IFRAME": "IFRAME",
    "DIALOG": "DIALOG",

    # Network
    "HTTP_REQUEST": "HTTP_REQUEST",

    # Logic & Data
    "FORMAT_DATA": "DATA_TRANSFORM",
    "TRANSFORM": "DATA_TRANSFORM",

    # AI Actions
    "LLM": "LLM",
    "VISION": "VISION",
    "AI_CLASSIFY": "AI_CLASSIFY",
    "AI_SUMMARIZE": "AI_SUMMARIZE",
    "AI_TRANSLATE": "AI_TRANSLATE",

    # Schema V2.0 ActionType Alignment (Strings from Enum)
    "FIND_AND_CLICK": "CLICK",
    "FIND_AND_TYPE": "TYPE",
    "EXTRACT_TEXT": "EXTRACT",
    "EXTRACT_TABLE": "EXTRACT",
    "WAIT_FOR_SELECTOR": "WAIT_FOR",
    "WAIT_FOR_HIDDEN": "WAIT_FOR",
    "SELECT_OPTION": "SELECT",
    "CHECK_CHECKBOX": "CHECK",

    # Two-Phase Cognitive Orchestration
    "UNIVERSAL_AGENT": "UNIVERSAL_AGENT",

    # Security
    "STEALTH_VAULT": "LOAD_VAULT",
}

# Node types that are structural (not executable actions)
SKIP_NODE_TYPES = {
    "NOTE", "DEBUG", "CONTEXT",
    "MERGE", "FILTER", "SET", "CODE", "FUNCTION",
    "HTTP_RESPONSE",
}

# Node types that affect control flow
CONTROL_FLOW_TYPES = {
    "CONDITION", "IF", "LOOP", "SWITCH", "ERROR_HANDLER",
}


def convert_graph_to_steps(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert a React Flow graph (nodes + edges) into an ordered step array.

    Args:
        nodes: React Flow nodes [{id, type, data: {label, config, ...}, position}]
        edges: React Flow edges [{id, source, target, ...}]

    Returns:
        Ordered list of [{action, params, node_id, label}]
    """
    if not nodes:
        logger.warning("[Converter] Empty node list — nothing to execute")
        return []

    # Build lookup maps
    node_map: dict[str, dict] = {n["id"]: n for n in nodes}

    # Build adjacency list (source → [target, ...])
    adjacency: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}

    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        if source and target and source in node_map and target in node_map:
            adjacency[source].append(target)
            in_degree[target] = in_degree.get(target, 0) + 1

    # Find start node: trigger node first, or node with in_degree 0
    trigger_types = {"TRIGGER", "SCHEDULE_TRIGGER", "WEBHOOK_TRIGGER"}
    start_node_id = None

    for node in nodes:
        node_type = node.get("type", "")
        if node_type in trigger_types:
            start_node_id = node["id"]
            break

    # Fallback: first node with no incoming edges
    if not start_node_id:
        for node_id, degree in in_degree.items():
            if degree == 0:
                start_node_id = node_id
                break

    if not start_node_id:
        start_node_id = nodes[0]["id"]
        logger.warning("[Converter] No trigger or root node found, starting from first node")

    # BFS topological traversal from start node
    ordered_ids: list[str] = []
    visited: set[str] = set()
    queue: deque[str] = deque([start_node_id])

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        ordered_ids.append(current)

        # Add children in order
        for child_id in adjacency.get(current, []):
            if child_id not in visited:
                queue.append(child_id)

    # Include any unvisited nodes (disconnected components)
    for node in nodes:
        if node["id"] not in visited:
            ordered_ids.append(node["id"])

    # Convert each node to a step
    steps: list[dict[str, Any]] = []

    for node_id in ordered_ids:
        node = node_map.get(node_id)
        if not node:
            continue

        node_type = node.get("type", "")

        # Skip non-executable nodes
        if node_type in SKIP_NODE_TYPES:
            logger.debug(f"[Converter] Skipping non-executable node: {node_type} ({node_id})")
            continue

        # Handle control flow nodes (future: implement branching logic)
        if node_type in CONTROL_FLOW_TYPES:
            logger.info(f"[Converter] Control flow node '{node_type}' — executing inline for now")
            # For now, we can extract any sub-actions from config
            # Full branching support would require a more complex executor

        # V2 Schema Compatibility: 'action' nodes contain sub-actions
        if node_type and node_type.upper() == "ACTION" and "actions" in node:
            for act in node.get("actions", []):
                act_type = str(act.get("type", "")).upper()
                mapped_action = NODE_TYPE_TO_ACTION.get(act_type) or act_type

                params = {k: v for k, v in act.items() if v is not None}
                step = {
                    "action": mapped_action,
                    "params": params,
                    "node_id": node_id,
                    "label": node.get("name") or node.get("data", {}).get("label") or mapped_action,
                }
                steps.append(step)
            continue

        # Original V1 Schema Mapping
        # Map node type to action
        action = NODE_TYPE_TO_ACTION.get(node_type)
        if not action:
            # Maybe the node_type is just snake_case (e.g. from LLM)
            action = NODE_TYPE_TO_ACTION.get(node_type.upper())
            if not action:
                logger.warning(f"[Converter] Unknown node type '{node_type}' — skipping")
                continue

        # Extract config from node data
        node_data = node.get("data", {})
        config = node_data.get("config", {})
        if not config and "inputs" in node_data:
            config = node_data.get("inputs", {})
        label = node_data.get("label", node_type)

        # Build step params from config
        params = _build_step_params(action, config, node_data)

        step = {
            "action": action,
            "params": params,
            "node_id": node_id,      # For NATS event correlation with frontend
            "label": label,
        }

        steps.append(step)

    logger.info(f"[Converter] Converted {len(nodes)} nodes → {len(steps)} executable steps")
    return steps


def _build_step_params(
    action: str,
    config: dict[str, Any],
    node_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Build step parameters from a node's config object.

    The frontend stores config as {url, selector, value, text, ...} etc.
    The activity expects {url, intent, text, ...} etc.
    This maps between the two formats.
    """
    params: dict[str, Any] = {}

    # Pass through all config values as a baseline
    params.update(config)

    # Action-specific mapping
    if action == "GOTO":
        # Ensure 'url' key exists - Triggers use 'targetUrl', Navigate nodes use 'url'
        params["url"] = config.get("url", config.get("targetUrl", config.get("value", "")))

    elif action in ("CLICK", "DOUBLE_CLICK", "RIGHT_CLICK", "HOVER", "FOCUS"):
        # Map 'selector' or 'target' to 'intent' for SmartFinder
        params["intent"] = (
            config.get("intent")
            or config.get("selector")
            or config.get("target")
            or config.get("description", "")
        )

    elif action == "TYPE":
        params["intent"] = (
            config.get("intent")
            or config.get("selector")
            or config.get("target", "")
        )
        params["text"] = config.get("text", config.get("value", ""))

    elif action == "WAIT_FOR":
        if "selector" in config:
            params["selector"] = config["selector"]
        elif "timeout" in config or "duration" in config:
            ms = config.get("timeout", config.get("duration", 1000))
            params["timeout_ms"] = int(ms)

    elif action == "SCROLL":
        if "target" in config or "selector" in config:
            params["intent"] = config.get("target", config.get("selector", ""))
        elif "amount" in config or "delta_y" in config:
            params["delta_y"] = config.get("delta_y", config.get("amount", 300))

    elif action == "EXTRACT":
        params["intent"] = (
            config.get("intent")
            or config.get("selector")
            or config.get("target", "")
        )
        if "attribute" in config:
            params["attribute"] = config["attribute"]

    elif action == "PRESS_KEY":
        params["key"] = config.get("key", config.get("value", "Enter"))

    elif action == "UPLOAD_FILE":
        params["intent"] = config.get("selector", config.get("target", ""))
        params["file_path"] = config.get("file_path", config.get("filePath", ""))

    elif action == "DRAG_AND_DROP":
        params["source"] = config.get("source", "")
        params["target"] = config.get("target", "")

    elif action == "SELECT":
        params["intent"] = config.get("selector", config.get("target", ""))
        params["value"] = config.get("value", config.get("option", ""))

    elif action == "SCREENSHOT":
        params["full_page"] = config.get("fullPage", False)

    elif action == "UNIVERSAL_AGENT":
        params["navigation_objective"] = config.get("navigation_objective", "")
        raw_schema = config.get("extraction_schema", "")
        if isinstance(raw_schema, str) and raw_schema.strip():
            import json
            try:
                params["extraction_schema"] = json.loads(raw_schema)
            except json.JSONDecodeError as e:
                raise ValueError(f"Malformed extraction_schema JSON in UNIVERSAL_AGENT node: {e}")
        elif isinstance(raw_schema, dict):
            params["extraction_schema"] = raw_schema

    elif action == "LOAD_VAULT":
        params["vault_name"] = config.get("vault_name", "")

    return params
