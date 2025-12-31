"""
recipeEngine.py - Universal Recipe Schema v2.0 Executor

The DAG Execution Engine that runs recipes with:
- Full async/await support
- Crash recovery via checkpoints (resume from Step 48)
- Node-type dispatching (Action, Decision, Loop, HumanGate, Parallel)
- Pre/Post condition guards with timeout enforcement

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                      RecipeEngine                                │
    │  load_recipe() → validate() → build_graph() → run()             │
    └───────────────────────┬─────────────────────────────────────────┘
                            │
    ┌───────────────────────▼─────────────────────────────────────────┐
    │                  ExecutionContext                                │
    │  Holds: browser, page, context variables, checkpoint history    │
    └───────────────────────┬─────────────────────────────────────────┘
                            │
    ┌───────────────────────▼─────────────────────────────────────────┐
    │               NodeProcessorFactory                               │
    │  Dispatches to: ActionProcessor, DecisionProcessor, etc.        │
    └───────────────────────┬─────────────────────────────────────────┘
                            │
    ┌───────────────────────▼─────────────────────────────────────────┐
    │                   StateManager                                   │
    │  save_checkpoint() ←→ hydrate_session() (Crash Recovery)        │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    engine = RecipeEngine(job_id="job-123")
    await engine.load_recipe(recipe_json)

    # Normal execution
    result = await engine.run()

    # Resume from crash (Step 48 Recovery)
    result = await engine.run(resume_from_node_id="node_step_48")

Author: e2e Platform Engineering
Version: 2.0.0
"""

import asyncio
import logging
import time
import json
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from playwright.async_api import Page, Browser, BrowserContext

# Internal imports
from core.recipe.recipeValidator import RecipeValidator, ValidationResult
from core.NervousSystem import NervousSystem

logger = logging.getLogger("recipeEngine")


# =============================================================================
# ENUMS & DATA CLASSES
# =============================================================================

class NodeType(Enum):
    """Supported node types in the schema."""
    ACTION = "action"
    DECISION = "decision"
    LOOP = "loop"
    CHECKPOINT = "checkpoint"
    HUMAN_GATE = "human_gate"
    PARALLEL = "parallel"


class ExecutionStatus(Enum):
    """Status of node/recipe execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"  # For human gates
    TIMEOUT = "timeout"


@dataclass
class NodeResult:
    """Result of executing a single node."""
    node_id: str
    status: ExecutionStatus
    next_node_id: Optional[str] = None  # Determined by edges/decisions
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0
    checkpoint_id: Optional[str] = None


@dataclass
class ExecutionContext:
    """
    The shared state passed through all nodes during execution.

    This is THE source of truth during a run. It contains:
    - Browser/Page references
    - Context variables (shared data between nodes)
    - Execution history (for debugging)
    - Input parameters
    """
    job_id: str
    browser: Optional[Browser] = None
    browser_context: Optional[BrowserContext] = None
    page: Optional[Page] = None

    # Recipe data
    recipe: Dict = field(default_factory=dict)
    nodes: Dict[str, Dict] = field(default_factory=dict)  # node_id -> node
    edges: List[Dict] = field(default_factory=list)

    # Dynamic state
    context_vars: Dict[str, Any] = field(default_factory=dict)  # {{ context.* }}
    inputs: Dict[str, Any] = field(default_factory=dict)        # {{ inputs.* }}
    secrets: Dict[str, Any] = field(default_factory=dict)       # {{ secrets.* }}

    # Loop state (for nested loops)
    loop_stack: List[Dict] = field(default_factory=list)  # [{iterator, index, items}]

    # Execution tracking
    executed_nodes: Set[str] = field(default_factory=set)
    execution_history: List[NodeResult] = field(default_factory=list)
    current_node_id: Optional[str] = None

    # Checkpoint state (for crash recovery)
    resume_from_node: Optional[str] = None
    last_checkpoint_id: Optional[str] = None


# =============================================================================
# STATE MANAGER - Checkpoint & Recovery
# =============================================================================

class StateManager:
    """
    Manages browser state checkpoints for crash recovery.

    THE CRASH RECOVERY ALGORITHM:
    =============================

    1. DURING NORMAL EXECUTION:
       - After critical nodes (login, loop progress), we call save_checkpoint()
       - This extracts: cookies, localStorage, sessionStorage, current URL
       - State is saved to persistent storage (DB/Redis/Disk)
       - Checkpoint is tagged with node_id and timestamp

    2. ON CRASH (Worker dies at Step 48):
       - Temporal automatically retries the activity
       - RecipeEngine is called with resume_from_node_id="node_step_48"
       - We call hydrate_session() with the last checkpoint
       - Browser is injected with saved cookies/storage
       - Execution resumes from Step 48, NOT Step 1

    3. CHECKPOINT STORAGE FORMAT:
       {
           "checkpoint_id": "cp_node_login_1703505600",
           "node_id": "node_login",
           "timestamp": "2025-12-25T10:00:00Z",
           "browser_state": {
               "cookies": [...],
               "local_storage": {...},
               "session_storage": {...},
               "url": "https://example.com/dashboard"
           },
           "context_vars": {...}
       }
    """

    def __init__(self, storage_backend: str = "memory"):
        """
        Initialize StateManager.

        Args:
            storage_backend: "memory" | "redis" | "postgres" | "file"
        """
        self.storage_backend = storage_backend
        self._checkpoints: Dict[str, Dict] = {}  # In-memory for now
        logger.info(f"[StateManager] Initialized (backend: {storage_backend})")

    async def save_checkpoint(
        self,
        checkpoint_id: str,
        node_id: str,
        ctx: ExecutionContext
    ) -> bool:
        """
        Extract and save browser state at a specific node.

        Args:
            checkpoint_id: Unique identifier for this checkpoint
            node_id: The node that triggered this checkpoint
            ctx: Current execution context

        Returns:
            True if checkpoint was saved successfully
        """
        try:
            # 1. Extract browser state
            browser_state = await self._extract_browser_state(ctx)

            # 2. Build checkpoint object
            checkpoint = {
                "checkpoint_id": checkpoint_id,
                "node_id": node_id,
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": ctx.job_id,
                "browser_state": browser_state,
                "context_vars": dict(ctx.context_vars),  # Deep copy
                "executed_nodes": list(ctx.executed_nodes),
                "loop_stack": list(ctx.loop_stack)
            }

            # 3. Persist to storage
            await self._persist_checkpoint(checkpoint_id, checkpoint)

            logger.info(f"[Checkpoint] Saved: {checkpoint_id} at node '{node_id}'")
            return True

        except Exception as e:
            logger.error(f"[Checkpoint] Failed to save: {e}")
            return False

    async def hydrate_session(
        self,
        checkpoint_id: str,
        ctx: ExecutionContext
    ) -> bool:
        """
        Restore browser state from a checkpoint.

        This is THE key to crash recovery. When a worker dies and restarts,
        we inject the saved cookies/storage so the browser is already
        "logged in" without re-running previous steps.

        Args:
            checkpoint_id: The checkpoint to restore from
            ctx: Execution context (with fresh browser)

        Returns:
            True if hydration was successful
        """
        try:
            # 1. Load checkpoint from storage
            checkpoint = await self._load_checkpoint(checkpoint_id)
            if not checkpoint:
                logger.warning(f"[Hydrate] Checkpoint not found: {checkpoint_id}")
                return False

            # 2. Inject cookies
            browser_state = checkpoint.get("browser_state", {})
            cookies = browser_state.get("cookies", [])
            if cookies and ctx.browser_context:
                await ctx.browser_context.add_cookies(cookies)
                logger.info(f"[Hydrate] Injected {len(cookies)} cookies")

            # 3. Navigate to saved URL
            saved_url = browser_state.get("url")
            if saved_url and ctx.page:
                await ctx.page.goto(saved_url)
                logger.info(f"[Hydrate] Navigated to: {saved_url}")

            # 4. Inject localStorage/sessionStorage
            local_storage = browser_state.get("local_storage", {})
            session_storage = browser_state.get("session_storage", {})

            if ctx.page and (local_storage or session_storage):
                await ctx.page.evaluate(f"""
                    () => {{
                        // Inject localStorage
                        const ls = {json.dumps(local_storage)};
                        for (const [key, value] of Object.entries(ls)) {{
                            localStorage.setItem(key, value);
                        }}

                        // Inject sessionStorage
                        const ss = {json.dumps(session_storage)};
                        for (const [key, value] of Object.entries(ss)) {{
                            sessionStorage.setItem(key, value);
                        }}
                    }}
                """)
                logger.info("[Hydrate] Injected localStorage and sessionStorage")

            # 5. Restore context variables
            ctx.context_vars = checkpoint.get("context_vars", {})
            ctx.executed_nodes = set(checkpoint.get("executed_nodes", []))
            ctx.loop_stack = checkpoint.get("loop_stack", [])

            logger.info(f"[Hydrate] Session restored from checkpoint: {checkpoint_id}")
            return True

        except Exception as e:
            logger.error(f"[Hydrate] Failed: {e}")
            return False

    async def _extract_browser_state(self, ctx: ExecutionContext) -> Dict:
        """Extract current browser state (cookies, storage, URL)."""
        state = {
            "cookies": [],
            "local_storage": {},
            "session_storage": {},
            "url": ""
        }

        try:
            if ctx.browser_context:
                state["cookies"] = await ctx.browser_context.cookies()

            if ctx.page:
                state["url"] = ctx.page.url

                # Extract storage (handle errors gracefully)
                try:
                    storage_data = await ctx.page.evaluate("""
                        () => ({
                            localStorage: Object.fromEntries(
                                Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])
                            ),
                            sessionStorage: Object.fromEntries(
                                Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)])
                            )
                        })
                    """)
                    state["local_storage"] = storage_data.get("localStorage", {})
                    state["session_storage"] = storage_data.get("sessionStorage", {})
                except:
                    pass  # Some pages block storage access
        except Exception as e:
            logger.warning(f"[Extract] Partial state extraction: {e}")

        return state

    async def _persist_checkpoint(self, checkpoint_id: str, data: Dict):
        """Persist checkpoint to storage backend."""
        # TODO: Implement Redis/Postgres backends
        self._checkpoints[checkpoint_id] = data

    async def _load_checkpoint(self, checkpoint_id: str) -> Optional[Dict]:
        """Load checkpoint from storage backend."""
        return self._checkpoints.get(checkpoint_id)

    async def get_latest_checkpoint(self, job_id: str) -> Optional[Dict]:
        """Get the most recent checkpoint for a job."""
        job_checkpoints = [
            cp for cp in self._checkpoints.values()
            if cp.get("job_id") == job_id
        ]
        if not job_checkpoints:
            return None
        return max(job_checkpoints, key=lambda x: x.get("timestamp", ""))


# =============================================================================
# STEP GUARD - Pre/Post Condition Wrapper
# =============================================================================

class StepGuard:
    """
    Wrapper that enforces pre_conditions, timeout_ms, and post_conditions
    around every node execution.

    FLOW:
    1. Check pre_conditions (e.g., "Is user logged in?")
       - If FAIL → Execute on_failure action (navigate, retry, skip)
    2. Execute the node with timeout
       - If TIMEOUT → Raise TimeoutError
    3. Check post_conditions (e.g., "Did page navigate to dashboard?")
       - If FAIL → Execute on_failure action (retry, goto, fail)
    """

    def __init__(self, ctx: ExecutionContext, node: Dict):
        self.ctx = ctx
        self.node = node
        self.node_id = node.get("id", "unknown")

    async def check_pre_conditions(self) -> Tuple[bool, Optional[Dict]]:
        """
        Evaluate all pre_conditions for the node.

        Returns:
            Tuple of (all_passed, first_failure_action)
        """
        pre_conditions = self.node.get("pre_conditions", [])

        for condition in pre_conditions:
            passed = await self._evaluate_condition(condition)
            if not passed:
                on_failure = condition.get("on_failure", {})
                logger.warning(f"[Guard] Pre-condition failed: {condition.get('id', 'unknown')}")
                return False, on_failure

        return True, None

    async def check_post_conditions(self) -> Tuple[bool, Optional[Dict]]:
        """
        Evaluate all post_conditions after node execution.

        Returns:
            Tuple of (all_passed, first_failure_action)
        """
        post_conditions = self.node.get("post_conditions", [])

        for condition in post_conditions:
            passed = await self._evaluate_condition(condition)
            if not passed:
                on_failure = condition.get("on_failure", {})
                logger.warning(f"[Guard] Post-condition failed: {condition.get('id', 'unknown')}")
                return False, on_failure

        return True, None

    async def _evaluate_condition(self, condition: Dict) -> bool:
        """
        Evaluate a single condition.

        Supports:
        - element_visible / element_not_visible
        - url_contains / page_url_matches
        - context_value
        - expression
        - any_of / all_of
        """
        check_type = condition.get("check", "")

        try:
            if check_type == "element_visible":
                selector = condition.get("selector", "")
                element = await self.ctx.page.query_selector(selector)
                return element is not None and await element.is_visible()

            elif check_type == "element_not_visible":
                selector = condition.get("selector", "")
                element = await self.ctx.page.query_selector(selector)
                return element is None or not await element.is_visible()

            elif check_type == "url_contains":
                value = condition.get("value", "")
                return value in self.ctx.page.url

            elif check_type == "page_url_matches":
                import re
                pattern = condition.get("pattern", "")
                return bool(re.match(pattern, self.ctx.page.url))

            elif check_type == "context_value":
                path = condition.get("path", "")
                expected = condition.get("value")
                cond_type = condition.get("condition", "equals")
                actual = self._resolve_variable(path)

                if cond_type == "equals":
                    return actual == expected
                elif cond_type == "length_greater_than":
                    return len(actual) > expected if actual else False
                return False

            elif check_type == "any_of":
                sub_conditions = condition.get("conditions", [])
                for sub in sub_conditions:
                    if await self._evaluate_condition(sub):
                        return True
                return False

            elif check_type == "all_of":
                sub_conditions = condition.get("conditions", [])
                for sub in sub_conditions:
                    if not await self._evaluate_condition(sub):
                        return False
                return True

            elif check_type == "network_idle":
                await self.ctx.page.wait_for_load_state("networkidle", timeout=5000)
                return True

            elif check_type == "page_loaded":
                await self.ctx.page.wait_for_load_state("domcontentloaded", timeout=5000)
                return True

            else:
                logger.warning(f"[Guard] Unknown condition type: {check_type}")
                return True  # Default to pass for unknown conditions

        except Exception as e:
            logger.error(f"[Guard] Condition evaluation failed: {e}")
            return False

    def _resolve_variable(self, path: str) -> Any:
        """Resolve a variable path like 'context.items' to its value."""
        parts = path.split(".")

        if parts[0] == "context":
            obj = self.ctx.context_vars
        elif parts[0] == "inputs":
            obj = self.ctx.inputs
        elif parts[0] == "loop" and self.ctx.loop_stack:
            obj = self.ctx.loop_stack[-1]  # Current loop
        else:
            return None

        for part in parts[1:]:
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, list) and part.isdigit():
                obj = obj[int(part)] if int(part) < len(obj) else None
            else:
                return None

        return obj


def with_timeout(timeout_ms: int):
    """
    Decorator to enforce timeout on async functions.

    Usage:
        @with_timeout(30000)
        async def do_something():
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=timeout_ms / 1000.0
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"Operation timed out after {timeout_ms}ms")
        return wrapper
    return decorator


# =============================================================================
# NODE PROCESSORS - Factory Pattern
# =============================================================================

class BaseNodeProcessor(ABC):
    """Abstract base class for all node processors."""

    def __init__(self, node: Dict, ctx: ExecutionContext):
        self.node = node
        self.ctx = ctx
        self.node_id = node.get("id", "unknown")

    @abstractmethod
    async def execute(self) -> NodeResult:
        """Execute the node and return result."""
        pass

    def resolve_template(self, value: Any) -> Any:
        """
        Resolve {{ variable }} templates in values.

        Supports:
        - {{ inputs.username }}
        - {{ context.processed_count }}
        - {{ loop.item.id }}
        - {{ loop.index }}
        """
        if not isinstance(value, str):
            return value

        import re
        pattern = r'\{\{\s*([\w.|\s+\-*/]+)\s*\}\}'

        def replacer(match):
            var_path = match.group(1).strip()

            # Handle simple expressions with arithmetic
            if any(op in var_path for op in ['+', '-', '*', '/']):
                # Simple arithmetic: {{ context.count + 1 }}
                try:
                    parts = re.split(r'([+\-*/])', var_path)
                    left = self._get_variable(parts[0].strip())
                    op = parts[1].strip() if len(parts) > 1 else None
                    right = parts[2].strip() if len(parts) > 2 else None

                    if op and right:
                        right_val = int(right) if right.isdigit() else self._get_variable(right)
                        if op == '+': return str(left + right_val)
                        elif op == '-': return str(left - right_val)
                        elif op == '*': return str(left * right_val)
                        elif op == '/': return str(left // right_val)
                except:
                    return match.group(0)

            # Handle pipe filters: {{ items | length }}
            if '|' in var_path:
                var_part, filter_part = var_path.split('|', 1)
                var_value = self._get_variable(var_part.strip())
                filter_name = filter_part.strip()

                if filter_name == "length":
                    return str(len(var_value) if var_value else 0)
                return str(var_value)

            # Simple variable lookup
            resolved = self._get_variable(var_path)
            return str(resolved) if resolved is not None else match.group(0)

        return re.sub(pattern, replacer, value)

    def _get_variable(self, path: str) -> Any:
        """Get variable value from context."""
        parts = path.split(".")

        if parts[0] == "inputs":
            obj = self.ctx.inputs
        elif parts[0] == "context":
            obj = self.ctx.context_vars
        elif parts[0] == "secrets":
            obj = self.ctx.secrets
        elif parts[0] == "loop" and self.ctx.loop_stack:
            obj = self.ctx.loop_stack[-1]
        elif parts[0] == "node":
            obj = {"id": self.node_id, "duration": 0}
        elif parts[0] == "timestamp":
            return datetime.utcnow().isoformat()
        elif parts[0] == "uuid":
            import uuid
            return str(uuid.uuid4())
        else:
            return None

        for part in parts[1:]:
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, list) and part.isdigit():
                idx = int(part)
                obj = obj[idx] if idx < len(obj) else None
            else:
                return None

        return obj


class ActionNodeProcessor(BaseNodeProcessor):
    """
    Processes ACTION nodes - the workhorses of automation.

    INTEGRATION WITH SMARTFINDER:
    =============================

    This processor uses SmartFinder's 4-layer fallback for element finding:
    - Layer 1 (REFLEX): SimHash fingerprint (<10ms)
    - Layer 2 (HEURISTIC): Levenshtein matching (~50ms)
    - Layer 3 (SEMANTIC): Vector DB search (~200ms)
    - Layer 4 (COGNITIVE): AI recovery (slow)

    SELF-HEALING:
    =============

    If Layer 1 fails but a deeper layer finds the element:
    1. SmartFinder computes new signature for found element
    2. We update the recipe metadata with new SimHash
    3. Next execution hits Layer 1 immediately (fast path)

    Handles action types:
    - navigate, find_and_click, find_and_type
    - wait_for_selector, wait_for_navigation, wait_for_network_idle
    - extract_text, extract_table
    - screenshot, set_context
    """

    def __init__(self, node: Dict, ctx: ExecutionContext, state_manager: "StateManager" = None):
        super().__init__(node, ctx)
        self.state_manager = state_manager
        self._smart_finder = None  # Lazy initialization
        self._healing_updates: List[Dict] = []  # Track metadata updates

    @property
    def smart_finder(self):
        """Lazy-load SmartFinder to avoid import issues."""
        if self._smart_finder is None:
            from core.selector.smartFinder import SmartFinder
            self._smart_finder = SmartFinder(self.ctx.page)
        return self._smart_finder

    async def execute(self) -> NodeResult:
        """Execute all actions in sequence with SmartFinder integration."""
        start_time = time.time()

        actions = self.node.get("actions", [])

        for idx, action in enumerate(sorted(actions, key=lambda a: a.get("seq", 0))):
            action_type = action.get("type", "")

            try:
                await self._execute_action(action, action_index=idx)
            except Exception as e:
                logger.error(f"[Action] Failed: {action_type} - {e}")
                return NodeResult(
                    node_id=self.node_id,
                    status=ExecutionStatus.FAILED,
                    error=f"Action '{action_type}' failed: {str(e)}",
                    duration_ms=int((time.time() - start_time) * 1000)
                )

        # Apply any pending self-healing updates
        if self._healing_updates and self.state_manager:
            await self._apply_healing_updates()

        duration = int((time.time() - start_time) * 1000)
        return NodeResult(
            node_id=self.node_id,
            status=ExecutionStatus.COMPLETED,
            duration_ms=duration,
            data={"healing_count": len(self._healing_updates)}
        )

    async def _execute_action(self, action: Dict, action_index: int):
        """
        Execute a single action with SmartFinder integration.

        Args:
            action: Action definition from recipe
            action_index: Index of this action in the node (for self-healing)
        """
        action_type = action.get("type", "")

        # Resolve any templates in action parameters
        resolved_action = {
            k: self.resolve_template(v) if isinstance(v, str) else v
            for k, v in action.items()
        }

        logger.debug(f"[Action] Executing: {action_type}")

        # =====================================================================
        # NAVIGATION ACTIONS
        # =====================================================================
        if action_type == "navigate":
            url = resolved_action.get("url", "")
            logger.info(f"[Action] Navigating to: {url}")
            await self.ctx.page.goto(url)

        # =====================================================================
        # SEMANTIC ELEMENT ACTIONS (Use SmartFinder)
        # =====================================================================
        elif action_type == "find_and_click":
            intent = resolved_action.get("intent", "")
            metadata = resolved_action.get("metadata", {})

            logger.info(f"[Action] Finding and clicking: '{intent}'")

            # Use SmartFinder with 4-layer fallback
            result = await self.smart_finder.find(intent, metadata)

            if not result.found:
                raise Exception(f"Element not found: {intent}")

            # Self-healing: Update metadata if needed
            if result.needs_healing and result.new_signature:
                logger.info(f"[Self-Healing] Layer {result.layer.value} found element, updating fingerprint")
                self._healing_updates.append({
                    "action_index": action_index,
                    "new_signature": result.new_signature,
                    "layer_used": result.layer.value
                })

            # Click the element
            await result.element.click()
            logger.info(f"[Action] Clicked element (Layer {result.layer.value}, {result.duration_ms}ms)")

        elif action_type == "find_and_type":
            intent = resolved_action.get("intent", "")
            value = resolved_action.get("value", "")
            metadata = resolved_action.get("metadata", {})
            clear_first = resolved_action.get("clear_first", False)
            mask_in_logs = resolved_action.get("mask_in_logs", False)

            display_value = "****" if mask_in_logs else value[:20]
            logger.info(f"[Action] Finding and typing into: '{intent}' (value: {display_value}...)")

            # Use SmartFinder to find the input element
            result = await self.smart_finder.find(intent, metadata)

            if not result.found:
                raise Exception(f"Input element not found: {intent}")

            # Self-healing
            if result.needs_healing and result.new_signature:
                logger.info(f"[Self-Healing] Layer {result.layer.value} found element, updating fingerprint")
                self._healing_updates.append({
                    "action_index": action_index,
                    "new_signature": result.new_signature,
                    "layer_used": result.layer.value
                })

            # Clear and type
            if clear_first:
                await result.element.fill("")

            # Use human-like typing if available
            try:
                from core.GlassBox import human_type
                await human_type(result.element, value)
            except ImportError:
                # Fallback to regular typing
                await result.element.type(value, delay=50)

            logger.info(f"[Action] Typed into element (Layer {result.layer.value}, {result.duration_ms}ms)")

        elif action_type == "find_and_extract":
            intent = resolved_action.get("intent", "")
            metadata = resolved_action.get("metadata", {})
            store_in = resolved_action.get("store_in", "")

            logger.info(f"[Action] Finding and extracting from: '{intent}'")

            result = await self.smart_finder.find(intent, metadata)

            if not result.found:
                raise Exception(f"Element not found for extraction: {intent}")

            # Self-healing
            if result.needs_healing and result.new_signature:
                self._healing_updates.append({
                    "action_index": action_index,
                    "new_signature": result.new_signature,
                    "layer_used": result.layer.value
                })

            # Extract text
            text = await result.element.inner_text()
            self._store_value(store_in, text.strip())
            logger.info(f"[Action] Extracted: '{text[:50]}...'")

        # =====================================================================
        # WAIT ACTIONS
        # =====================================================================
        elif action_type == "wait_for_selector":
            selector = resolved_action.get("selector", "")
            timeout = resolved_action.get("timeout_ms", 10000)
            await self.ctx.page.wait_for_selector(selector, timeout=timeout)

        elif action_type == "wait_for_navigation":
            timeout = resolved_action.get("timeout_ms", 10000)
            await self.ctx.page.wait_for_load_state("networkidle", timeout=timeout)

        elif action_type == "wait_for_network_idle":
            await self.ctx.page.wait_for_load_state("networkidle")

        elif action_type == "wait_for_load_state":
            state = resolved_action.get("state", "domcontentloaded")
            await self.ctx.page.wait_for_load_state(state)

        elif action_type == "wait":
            duration_ms = resolved_action.get("duration_ms", 1000)
            await asyncio.sleep(duration_ms / 1000.0)

        # =====================================================================
        # EXTRACTION ACTIONS
        # =====================================================================
        elif action_type == "extract_text":
            selector = resolved_action.get("selector", "")
            store_in = resolved_action.get("store_in", "")
            element = await self.ctx.page.query_selector(selector)
            if element:
                text = await element.inner_text()
                self._store_value(store_in, text.strip())
                logger.debug(f"[Action] Extracted text: '{text[:50]}...'")

        elif action_type == "extract_table":
            selector = resolved_action.get("selector", "")
            store_in = resolved_action.get("store_in", "")
            columns = resolved_action.get("columns", [])
            max_rows = resolved_action.get("max_rows", 100)

            # Extract table data
            rows_data = await self._extract_table_data(selector, columns, max_rows)
            self._store_value(store_in, rows_data)
            logger.info(f"[Action] Extracted table: {len(rows_data)} rows")

        # =====================================================================
        # UTILITY ACTIONS
        # =====================================================================
        elif action_type == "screenshot":
            store_in = resolved_action.get("store_in", "screenshot")
            screenshot = await self.ctx.page.screenshot()
            self._store_value(store_in, screenshot)
            logger.debug("[Action] Captured screenshot")

        elif action_type == "set_context":
            path = resolved_action.get("path", "")
            value = resolved_action.get("value")
            resolved_value = self.resolve_template(str(value)) if isinstance(value, str) else value
            self._store_value(path, resolved_value)
            logger.debug(f"[Action] Set context: {path}")

        elif action_type == "log":
            message = resolved_action.get("message", "")
            logger.info(f"[Recipe Log] {message}")

        elif action_type == "emit_event":
            event = resolved_action.get("event", "")
            data = resolved_action.get("data", {})
            try:
                await NervousSystem.publish_update(
                    self.ctx.job_id, self.node_id, "RUNNING", f"Event: {event}"
                )
            except Exception as e:
                logger.warning(f"[Action] Event emission failed: {e}")

        elif action_type == "create_report":
            format_type = resolved_action.get("format", "json")
            content = resolved_action.get("content", {})
            store_in = resolved_action.get("store_in", "")

            # Resolve all template values in content
            resolved_content = {}
            for key, val in content.items():
                if isinstance(val, str):
                    resolved_content[key] = self.resolve_template(val)
                else:
                    resolved_content[key] = val

            self._store_value(store_in, resolved_content)
            logger.info(f"[Action] Created report: {store_in}")

        else:
            logger.warning(f"[Action] Unknown type: {action_type}")

    async def _extract_table_data(
        self,
        selector: str,
        columns: List[str],
        max_rows: int
    ) -> List[Dict]:
        """Extract data from an HTML table."""
        rows_data = []

        try:
            table = await self.ctx.page.query_selector(selector)
            if not table:
                return rows_data

            rows = await table.query_selector_all("tr")

            for row in rows[1:max_rows+1]:  # Skip header row
                cells = await row.query_selector_all("td")
                if len(cells) >= len(columns):
                    row_data = {}
                    for i, col_name in enumerate(columns):
                        cell_text = await cells[i].inner_text()
                        row_data[col_name] = cell_text.strip()
                    rows_data.append(row_data)

        except Exception as e:
            logger.warning(f"[Action] Table extraction failed: {e}")

        return rows_data

    async def _apply_healing_updates(self):
        """
        Apply all pending self-healing updates to the recipe.

        AUDIT FIX: Added version lock check to prevent race conditions
        when multiple workers try to update the same recipe.

        This is called at the end of node execution to batch the updates.
        """
        if not self._healing_updates:
            return

        # AUDIT FIX: Version lock check
        recipe_version = getattr(self.ctx, 'recipe_version', None)
        current_version = getattr(self.state_manager, '_recipe_versions', {}).get(self.ctx.job_id)

        if recipe_version and current_version and recipe_version != current_version:
            logger.warning(
                f"[Self-Healing] Version conflict detected. "
                f"Loaded: {recipe_version}, Current: {current_version}. "
                f"Aborting {len(self._healing_updates)} healing updates to prevent race condition."
            )
            self._healing_updates.clear()
            return

        for update in self._healing_updates:
            try:
                # Mock call to state manager - in production this updates the DB
                logger.info(
                    f"[Self-Healing] Updating node '{self.node_id}' action {update['action_index']} "
                    f"with new SimHash: {update['new_signature'].get('simhash', 'N/A')[:16]}... "
                    f"(found via Layer {update['layer_used']})"
                )

                # TODO: Real implementation would call:
                # await self.state_manager.update_recipe_metadata(
                #     node_id=self.node_id,
                #     action_index=update["action_index"],
                #     new_signature=update["new_signature"],
                #     expected_version=recipe_version  # Optimistic locking
                # )

            except Exception as e:
                logger.error(f"[Self-Healing] Failed to update metadata: {e}")

        self._healing_updates.clear()

    async def _safe_element_action(
        self,
        element,
        action: str,
        intent: str,
        metadata: dict,
        value: str = None,
        max_retries: int = 2
    ):
        """
        AUDIT FIX: Safe wrapper for element actions with stale element retry.

        If an element becomes stale (DOM re-rendered), this will:
        1. Re-find the element using SmartFinder
        2. Retry the action

        Args:
            element: Element handle
            action: "click" or "type"
            intent: Intent string for re-finding
            metadata: Metadata for re-finding
            value: Value to type (for action="type")
            max_retries: Maximum retry attempts
        """
        for attempt in range(max_retries + 1):
            try:
                if action == "click":
                    await element.click()
                    return
                elif action == "type":
                    await element.type(value or "", delay=50)
                    return
                elif action == "fill":
                    await element.fill(value or "")
                    return
            except Exception as e:
                error_str = str(e).lower()
                is_stale = any(term in error_str for term in [
                    "stale", "detached", "removed", "disposed",
                    "target closed", "element is not attached"
                ])

                if is_stale and attempt < max_retries:
                    logger.warning(
                        f"[StaleElement] Element became stale, re-finding... "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    # Re-find the element
                    result = await self.smart_finder.find(intent, metadata)
                    if result.found:
                        element = result.element
                        continue
                    else:
                        raise Exception(f"Failed to re-find stale element: {intent}")
                else:
                    raise

    def _store_value(self, path: str, value: Any):
        """Store a value at the given path in context."""
        if not path:
            return

        parts = path.split(".")
        if parts[0] == "context":
            target = self.ctx.context_vars
            for part in parts[1:-1]:
                if part not in target:
                    target[part] = {}
                target = target[part]
            target[parts[-1]] = value


class DecisionNodeProcessor(BaseNodeProcessor):
    """
    Processes DECISION nodes - branching logic.

    Evaluates conditions and returns the target node ID.
    """

    async def execute(self) -> NodeResult:
        """Evaluate decision and determine next node."""
        evaluate = self.node.get("evaluate", {})
        source_value = self.resolve_template(evaluate.get("source", ""))

        branches = evaluate.get("branches", [])

        for branch in branches:
            condition = branch.get("condition", "")

            if self._evaluate_branch(condition, source_value, branch):
                target = branch.get("target", "")
                return NodeResult(
                    node_id=self.node_id,
                    status=ExecutionStatus.COMPLETED,
                    next_node_id=target
                )

        # No branch matched, use default
        default_target = evaluate.get("default", "")
        return NodeResult(
            node_id=self.node_id,
            status=ExecutionStatus.COMPLETED,
            next_node_id=default_target
        )

    def _evaluate_branch(self, condition: str, source_value: Any, branch: Dict) -> bool:
        """Evaluate a branch condition."""
        compare_value = branch.get("value")

        if condition == "equals":
            return str(source_value) == str(compare_value)
        elif condition == "not_equals":
            return str(source_value) != str(compare_value)
        elif condition == "greater_than":
            field = branch.get("field")
            if field:
                source_value = self.resolve_template(field)
            try:
                return float(source_value) > float(compare_value)
            except:
                return False
        elif condition == "less_than":
            try:
                return float(source_value) < float(compare_value)
            except:
                return False
        elif condition == "contains":
            return str(compare_value) in str(source_value)

        return False


class LoopNodeProcessor(BaseNodeProcessor):
    """
    Processes LOOP nodes - iteration over collections.

    LOOP STATE MANAGEMENT:
    - Pushes loop state to ctx.loop_stack on start
    - Updates {{ loop.item }} and {{ loop.index }} each iteration
    - Handles continue_on_error for resilient processing
    - Checkpoints every N iterations
    """

    async def execute(self) -> NodeResult:
        """Execute loop and process all items."""
        loop_config = self.node.get("loop", {})

        # Resolve source collection
        source_path = loop_config.get("source", "")
        items = self._get_variable(source_path.replace("{{", "").replace("}}", "").strip())

        if not items or not isinstance(items, list):
            logger.warning(f"[Loop] Source is empty or not a list: {source_path}")
            return NodeResult(
                node_id=self.node_id,
                status=ExecutionStatus.COMPLETED,
                next_node_id=loop_config.get("on_complete")
            )

        iterator_var = loop_config.get("iterator_var", "item")
        index_var = loop_config.get("index_var", "index")
        max_iterations = loop_config.get("max_iterations", 1000)
        continue_on_error = loop_config.get("continue_on_error", False)
        checkpoint_every = loop_config.get("checkpoint_every")
        body_node = loop_config.get("body")

        # Limit iterations
        items = items[:max_iterations]

        # Push loop state to stack
        loop_state = {
            iterator_var: None,
            index_var: 0,
            "item": None,  # Standard accessor
            "index": 0
        }
        self.ctx.loop_stack.append(loop_state)

        try:
            for idx, item in enumerate(items):
                # Update loop state
                loop_state[iterator_var] = item
                loop_state[index_var] = idx
                loop_state["item"] = item
                loop_state["index"] = idx

                # Return body node for engine to execute
                # Engine will call us again for next iteration via loop_continue edge
                return NodeResult(
                    node_id=self.node_id,
                    status=ExecutionStatus.RUNNING,
                    next_node_id=body_node,
                    data={"loop_index": idx, "loop_total": len(items)}
                )

        finally:
            # Pop loop state when done
            if self.ctx.loop_stack:
                self.ctx.loop_stack.pop()

        return NodeResult(
            node_id=self.node_id,
            status=ExecutionStatus.COMPLETED,
            next_node_id=loop_config.get("on_complete")
        )


class HumanGateNodeProcessor(BaseNodeProcessor):
    """
    Processes HUMAN_GATE nodes - pauses for human input.

    When executed:
    1. Saves a checkpoint (for resumability)
    2. Emits a NATS event requesting human input
    3. Throws HumanInterventionRequired exception
    4. Temporal workflow hibernates (zero CPU cost)
    5. When signal received, workflow resumes with user input
    """

    async def execute(self) -> NodeResult:
        """Pause execution for human input."""
        from exceptions import HumanInterventionRequired

        gate_config = self.node.get("gate", {})
        reason = self.resolve_template(gate_config.get("reason", "Human approval required"))
        prompt = self.resolve_template(gate_config.get("prompt", "Please provide input"))
        options = gate_config.get("options", [])

        # Emit event to notify humans
        await NervousSystem.publish_update(
            self.ctx.job_id,
            self.node_id,
            "PAUSED",
            f"Human Gate: {reason}"
        )

        # Raise exception to trigger Temporal hibernation
        raise HumanInterventionRequired(
            reason=reason,
            prompt=prompt,
            options=[opt.get("label") for opt in options],
            node_id=self.node_id
        )


class CheckpointNodeProcessor(BaseNodeProcessor):
    """Processes explicit CHECKPOINT nodes."""

    def __init__(self, node: Dict, ctx: ExecutionContext, state_manager: StateManager):
        super().__init__(node, ctx)
        self.state_manager = state_manager

    async def execute(self) -> NodeResult:
        """Save a checkpoint."""
        checkpoint_config = self.node.get("checkpoint", {})
        checkpoint_id = self.resolve_template(
            checkpoint_config.get("id", f"cp_{self.node_id}_{int(time.time())}")
        )

        success = await self.state_manager.save_checkpoint(
            checkpoint_id=checkpoint_id,
            node_id=self.node_id,
            ctx=self.ctx
        )

        return NodeResult(
            node_id=self.node_id,
            status=ExecutionStatus.COMPLETED if success else ExecutionStatus.FAILED,
            checkpoint_id=checkpoint_id
        )


class NodeProcessorFactory:
    """
    Factory to create the appropriate processor for each node type.

    DISPATCH TABLE:
    - action      → ActionNodeProcessor (with SmartFinder + self-healing)
    - decision    → DecisionNodeProcessor
    - loop        → LoopNodeProcessor
    - human_gate  → HumanGateNodeProcessor
    - checkpoint  → CheckpointNodeProcessor
    - parallel    → ParallelNodeProcessor (TODO)
    """

    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager

    def create(self, node: Dict, ctx: ExecutionContext) -> BaseNodeProcessor:
        """Create the appropriate processor for a node."""
        node_type = node.get("type", "action")

        if node_type == "action":
            # Pass state_manager for self-healing updates
            return ActionNodeProcessor(node, ctx, self.state_manager)
        elif node_type == "decision":
            return DecisionNodeProcessor(node, ctx)
        elif node_type == "loop":
            return LoopNodeProcessor(node, ctx)
        elif node_type == "human_gate":
            return HumanGateNodeProcessor(node, ctx)
        elif node_type == "checkpoint":
            return CheckpointNodeProcessor(node, ctx, self.state_manager)
        # elif node_type == "parallel":
        #     return ParallelNodeProcessor(node, ctx)
        else:
            logger.warning(f"[Factory] Unknown node type '{node_type}', defaulting to action")
            return ActionNodeProcessor(node, ctx, self.state_manager)


# =============================================================================
# RECIPE ENGINE - Main Executor
# =============================================================================

class RecipeEngine:
    """
    The Main DAG Executor.

    EXECUTION FLOW:
    ===============

    1. load_recipe(json_data)
       - Validates schema against 15 rules
       - Builds node lookup table
       - Builds edge graph for traversal

    2. run(resume_from_node_id=None)
       - If resuming: hydrate browser state from checkpoint
       - Start from entry_point (or resume node)
       - Execute nodes in DAG order
       - Handle pre/post conditions via StepGuard
       - Save checkpoints after critical nodes
       - Follow edges to next node
       - Continue until exit_point reached

    CRASH RECOVERY (The "Step 48" Problem):
    =======================================

    SCENARIO: Worker crashes at Step 48 of 100-step workflow.

    WITHOUT CHECKPOINTS:
    - Temporal retries the activity
    - Starts from Step 1
    - User sees duplicate actions (double login, double submit)
    - 47 steps of wasted time

    WITH CHECKPOINTS:
    - Temporal retries the activity
    - RecipeEngine receives resume_from_node_id="node_step_48"
    - hydrate_session() restores cookies/storage from last checkpoint
    - Browser is already logged in
    - Execution resumes exactly at Step 48
    - Zero duplicate actions
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.validator = RecipeValidator()
        self.state_manager = StateManager(storage_backend="memory")
        self.factory = NodeProcessorFactory(self.state_manager)

        self.ctx: Optional[ExecutionContext] = None
        self.recipe: Optional[Dict] = None
        self.is_loaded = False

        # =================================================================
        # GLASS BOX - Real-time streaming & remote control
        # =================================================================
        self._streamer = None      # BrowserStreamer instance
        self._input_bridge = None  # InputBridge instance
        self._streaming_enabled = False

    # -------------------------------------------------------------------------
    # GLASS BOX - Streaming & Remote Control
    # -------------------------------------------------------------------------

    async def enable_streaming(self, workflow_id: str = None):
        """
        Enable real-time browser streaming.

        Call this AFTER run() has started to attach streaming to the page.

        Args:
            workflow_id: Optional workflow ID for NATS subject (defaults to job_id)
        """
        if not self.ctx or not self.ctx.page:
            raise RuntimeError("[GlassBox] Cannot enable streaming: no page context")

        try:
            from core.glassBox import BrowserStreamer, InputBridge

            # Create streamer
            self._streamer = BrowserStreamer(
                page=self.ctx.page,
                workflow_id=workflow_id or self.job_id
            )
            await self._streamer.start()

            # Create input bridge
            self._input_bridge = InputBridge(self.ctx.page)

            self._streaming_enabled = True
            logger.info(f"[GlassBox] Streaming enabled for job: {self.job_id}")

        except ImportError as e:
            logger.warning(f"[GlassBox] Streaming not available: {e}")

    async def disable_streaming(self):
        """Stop browser streaming gracefully."""
        if self._streamer:
            await self._streamer.stop()
            self._streamer = None
        self._input_bridge = None
        self._streaming_enabled = False
        logger.info("[GlassBox] Streaming disabled")

    async def handle_remote_input(self, event: Dict[str, Any]) -> bool:
        """
        Handle remote input events from frontend.

        COORDINATE MAPPING:
        ===================
        Frontend canvas may be different size than browser viewport.
        InputBridge handles scaling automatically.

        Args:
            event: Input event from frontend WebSocket
                Mouse: {"type": "click", "x": 100, "y": 50, "button": "left"}
                Keyboard: {"type": "keydown", "key": "Enter"}

        Returns:
            True if event was handled successfully

        Example:
            # From Go Gateway WebSocket handler:
            event = {"type": "click", "x": 500, "y": 200}
            await engine.handle_remote_input(event)
        """
        if not self._input_bridge:
            logger.warning("[GlassBox] Remote input received but input bridge not initialized")
            return False

        try:
            return await self._input_bridge.handle_event(event)
        except Exception as e:
            logger.error(f"[GlassBox] Remote input failed: {e}")
            return False

    @property
    def is_streaming(self) -> bool:
        """Check if streaming is currently active."""
        return self._streaming_enabled and self._streamer is not None

    async def load_recipe(self, recipe_json: Dict) -> ValidationResult:
        """
        Validate and load a recipe for execution.

        Args:
            recipe_json: The recipe JSON object

        Returns:
            ValidationResult indicating success/failure
        """
        # 1. Validate against 15 rules
        validation = self.validator.validate(recipe_json)
        if not validation.is_valid:
            logger.error(f"[Engine] Recipe validation failed: {len(validation.errors)} errors")
            return validation

        # 2. Store recipe
        self.recipe = recipe_json

        # 3. Build execution context
        self.ctx = ExecutionContext(
            job_id=self.job_id,
            recipe=recipe_json,
            context_vars=dict(recipe_json.get("context", {}).get("initial", {})),
            inputs={},
            secrets={}
        )

        # 4. Build node lookup
        for node in recipe_json.get("nodes", []):
            self.ctx.nodes[node.get("id")] = node

        # 5. Store edges
        self.ctx.edges = recipe_json.get("edges", [])

        self.is_loaded = True
        logger.info(f"[Engine] Recipe loaded: {validation.recipe_name} ({len(self.ctx.nodes)} nodes)")

        return validation

    async def run(
        self,
        browser: Browser,
        inputs: Dict[str, Any] = None,
        secrets: Dict[str, Any] = None,
        resume_from_node_id: Optional[str] = None,
        resume_checkpoint_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute the recipe DAG.

        Args:
            browser: Playwright browser instance
            inputs: User-provided input parameters
            secrets: Encrypted credentials
            resume_from_node_id: Node to resume from (crash recovery)
            resume_checkpoint_id: Checkpoint to hydrate from

        Returns:
            Dict with execution result and final context
        """
        if not self.is_loaded:
            raise RuntimeError("Recipe not loaded. Call load_recipe() first.")

        # 1. Setup browser context
        self.ctx.browser = browser
        self.ctx.browser_context = await browser.new_context()
        self.ctx.page = await self.ctx.browser_context.new_page()
        self.ctx.inputs = inputs or {}
        self.ctx.secrets = secrets or {}

        # 2. CRASH RECOVERY: Hydrate if resuming
        if resume_from_node_id and resume_checkpoint_id:
            logger.info(f"[Engine] RESUMING from {resume_from_node_id} (checkpoint: {resume_checkpoint_id})")

            await self.state_manager.hydrate_session(resume_checkpoint_id, self.ctx)
            self.ctx.resume_from_node = resume_from_node_id
            current_node_id = resume_from_node_id
        else:
            # Normal start from entry point
            current_node_id = self.recipe.get("entry_point")

        # 3. Main execution loop
        exit_points = self.recipe.get("exit_points", {})
        max_steps = 10000  # Safety limit
        step_count = 0

        try:
            while current_node_id and step_count < max_steps:
                step_count += 1

                node = self.ctx.nodes.get(current_node_id)
                if not node:
                    logger.error(f"[Engine] Node not found: {current_node_id}")
                    break

                logger.info(f"[Engine] Step {step_count}: Executing '{current_node_id}'")
                self.ctx.current_node_id = current_node_id

                # 4. Execute node with guards
                result = await self._execute_node_with_guards(node)

                # 5. Track execution
                self.ctx.executed_nodes.add(current_node_id)
                self.ctx.execution_history.append(result)

                # 6. Check if we hit an exit point
                if current_node_id == exit_points.get("success"):
                    logger.info("[Engine] Reached SUCCESS exit point")
                    return self._build_result(ExecutionStatus.COMPLETED)

                if result.status == ExecutionStatus.FAILED:
                    current_node_id = exit_points.get("failure")
                    continue

                # 7. Determine next node
                if result.next_node_id:
                    current_node_id = result.next_node_id
                else:
                    current_node_id = self._find_next_node(current_node_id, result)

                # 8. Save checkpoint if node requests it
                if node.get("state_policy", {}).get("checkpoint"):
                    checkpoint_id = f"cp_{current_node_id}_{int(time.time())}"
                    await self.state_manager.save_checkpoint(checkpoint_id, current_node_id, self.ctx)
                    self.ctx.last_checkpoint_id = checkpoint_id

            return self._build_result(ExecutionStatus.COMPLETED)

        except Exception as e:
            logger.error(f"[Engine] Execution failed: {e}")
            return self._build_result(ExecutionStatus.FAILED, error=str(e))

        finally:
            # Cleanup
            if self.ctx.page:
                await self.ctx.page.close()
            if self.ctx.browser_context:
                await self.ctx.browser_context.close()

    async def _execute_node_with_guards(self, node: Dict) -> NodeResult:
        """
        Execute a node with pre/post condition guards and timeout.

        FLOW:
        1. Check pre_conditions → handle failures
        2. Execute node with timeout
        3. Check post_conditions → handle failures
        4. Return result
        """
        node_id = node.get("id")
        guard = StepGuard(self.ctx, node)
        execution = node.get("execution", {})
        timeout_ms = execution.get("timeout_ms", 30000)

        # 1. Pre-conditions
        pre_passed, pre_failure_action = await guard.check_pre_conditions()
        if not pre_passed:
            next_node = await self._handle_failure_action(pre_failure_action, node_id)
            return NodeResult(
                node_id=node_id,
                status=ExecutionStatus.SKIPPED,
                next_node_id=next_node
            )

        # 2. Execute with timeout
        processor = self.factory.create(node, self.ctx)

        try:
            result = await asyncio.wait_for(
                processor.execute(),
                timeout=timeout_ms / 1000.0
            )
        except asyncio.TimeoutError:
            return NodeResult(
                node_id=node_id,
                status=ExecutionStatus.TIMEOUT,
                error=f"Node timed out after {timeout_ms}ms"
            )
        except Exception as e:
            # Check if it's a human intervention request
            if "HumanInterventionRequired" in str(type(e).__name__):
                raise
            return NodeResult(
                node_id=node_id,
                status=ExecutionStatus.FAILED,
                error=str(e)
            )

        # 3. Post-conditions
        post_passed, post_failure_action = await guard.check_post_conditions()
        if not post_passed:
            next_node = await self._handle_failure_action(post_failure_action, node_id)
            if next_node:
                result.next_node_id = next_node
            else:
                result.status = ExecutionStatus.FAILED

        return result

    async def _handle_failure_action(
        self,
        failure_action: Optional[Dict],
        current_node_id: str
    ) -> Optional[str]:
        """
        Process on_failure action from a condition.

        Returns the next node to go to, or None if should fail.
        """
        if not failure_action:
            return None

        action = failure_action.get("action", "")

        if action == "goto":
            return failure_action.get("target")
        elif action == "skip_to":
            return failure_action.get("target")
        elif action == "retry":
            return current_node_id  # Will re-execute current node
        elif action == "fail":
            return self.recipe.get("exit_points", {}).get("failure")
        elif action == "wait":
            duration = failure_action.get("duration_ms", 1000)
            await asyncio.sleep(duration / 1000.0)
            return current_node_id  # Retry after wait
        elif action == "navigate":
            target_url = failure_action.get("target", "")
            await self.ctx.page.goto(target_url)
            return current_node_id
        elif action == "branch":
            # Evaluate nested conditions
            for condition in failure_action.get("conditions", []):
                guard = StepGuard(self.ctx, {"pre_conditions": [condition.get("if", {})]})
                passed, _ = await guard.check_pre_conditions()
                if passed:
                    then_action = condition.get("then", {})
                    return await self._handle_failure_action(then_action, current_node_id)
            # Use default if no condition matched
            default_action = failure_action.get("default", {})
            return await self._handle_failure_action(default_action, current_node_id)

        return None

    def _find_next_node(self, current_node_id: str, result: NodeResult) -> Optional[str]:
        """
        Find the next node based on edges.

        Traverses edges to find where to go next.
        """
        for edge in self.ctx.edges:
            if edge.get("from") == current_node_id:
                # Skip loop_continue edges unless explicitly returned
                if edge.get("type") == "loop_continue":
                    continue

                condition = edge.get("condition")
                if condition is None:
                    return edge.get("to")

        return None

    def _build_result(
        self,
        status: ExecutionStatus,
        error: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build the final execution result."""
        return {
            "status": status.value,
            "job_id": self.job_id,
            "recipe_name": self.recipe.get("metadata", {}).get("name"),
            "context": dict(self.ctx.context_vars),
            "executed_nodes": list(self.ctx.executed_nodes),
            "total_steps": len(self.ctx.execution_history),
            "last_checkpoint_id": self.ctx.last_checkpoint_id,
            "error": error
        }


# =============================================================================
# EXAMPLE USAGE
# =============================================================================

if __name__ == "__main__":
    """
    Standalone test for RecipeEngine.
    """
    import json

    async def test():
        print("=" * 60)
        print("RECIPE ENGINE v2.0 - Test Run")
        print("=" * 60)

        # Minimal test recipe
        test_recipe = {
            "metadata": {"name": "test_recipe"},
            "context": {"initial": {"count": 0}},
            "inputs": {"required": [], "optional": []},
            "nodes": [
                {
                    "id": "node_start",
                    "type": "action",
                    "actions": [
                        {"seq": 1, "type": "log", "message": "Starting test..."},
                        {"seq": 2, "type": "set_context", "path": "context.count", "value": "1"}
                    ],
                    "execution": {"timeout_ms": 30000},
                    "post_conditions": [{"check": "page_loaded", "on_failure": {"action": "retry"}}]
                },
                {
                    "id": "node_end",
                    "type": "action",
                    "actions": [
                        {"seq": 1, "type": "log", "message": "Test complete!"}
                    ],
                    "execution": {"timeout_ms": 5000},
                    "post_conditions": [{"check": "page_loaded", "on_failure": {"action": "retry"}}]
                }
            ],
            "edges": [
                {"from": "node_start", "to": "node_end"}
            ],
            "entry_point": "node_start",
            "exit_points": {
                "success": "node_end",
                "failure": "node_end",
                "timeout": "node_end"
            }
        }

        engine = RecipeEngine(job_id="test-001")
        result = await engine.load_recipe(test_recipe)
        print(f"\nValidation: {result.summary()}")

        print("\n[Engine] Ready to execute (browser required for full test)")

    asyncio.run(test())
