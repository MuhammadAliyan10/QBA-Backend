"""
recipeSchema.py - Pydantic Models for Universal Recipe Schema v2.0

Type-safe data validation for recipe JSON structures.
Provides IDE autocompletion and runtime validation.

Usage:
    from core.recipeSchema import Recipe, ActionNode, LoopNode

    recipe = Recipe.model_validate(json_data)
    print(recipe.metadata.name)
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator


# =============================================================================
# ENUMS
# =============================================================================

class ResourceTier(str, Enum):
    STANDARD = "standard"
    HIGH_MEMORY = "high_memory"
    GPU = "gpu"


class BrowserType(str, Enum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


class NodeType(str, Enum):
    ACTION = "action"
    DECISION = "decision"
    LOOP = "loop"
    CHECKPOINT = "checkpoint"
    HUMAN_GATE = "human_gate"
    PARALLEL = "parallel"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "find_and_click"
    TYPE = "find_and_type"
    EXTRACT = "extract_text"
    EXTRACT_TABLE = "extract_table"
    WAIT = "wait_for_selector"
    WAIT_HIDDEN = "wait_for_hidden"
    SCREENSHOT = "screenshot"
    SCROLL = "scroll"
    HOVER = "hover"
    SELECT = "select_option"
    CHECK = "check_checkbox"



class BackoffStrategy(str, Enum):
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    CONSTANT = "constant"


class JoinStrategy(str, Enum):
    WAIT_ALL = "wait_all"
    WAIT_ANY = "wait_any"
    WAIT_N = "wait_n"


# =============================================================================
# CONFIGURATION MODELS
# =============================================================================

class ProxyConfig(BaseModel):
    """Proxy configuration for browser."""
    enabled: bool = False
    type: str = "datacenter"  # residential | datacenter
    region: str = "us"
    sticky_session: bool = True


class ViewportConfig(BaseModel):
    """Browser viewport settings."""
    width: int = 1920
    height: int = 1080


class GeolocationConfig(BaseModel):
    """Geolocation settings."""
    latitude: float
    longitude: float


class RecipeConfig(BaseModel):
    """Global recipe configuration."""
    resource_tier: ResourceTier = ResourceTier.STANDARD
    browser: BrowserType = BrowserType.CHROMIUM
    viewport: ViewportConfig = Field(default_factory=ViewportConfig)
    locale: str = "en-US"
    timezone: str = "America/New_York"
    geolocation: Optional[GeolocationConfig] = None
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    stealth_mode: bool = True
    record_video: bool = False
    screenshot_on_error: bool = True


# =============================================================================
# METADATA
# =============================================================================

class RecipeMetadata(BaseModel):
    """Recipe metadata."""
    id: str
    name: str
    description: str = ""
    author: str = "unknown"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    tags: list[str] = Field(default_factory=list)
    priority: str = "normal"  # low | normal | high | critical
    estimated_duration_ms: int = 60000
    max_cost_usd: float = 10.0


# =============================================================================
# INPUTS
# =============================================================================

class InputField(BaseModel):
    """Single input field definition."""
    name: str
    type: str = "string"  # string | integer | number | boolean | url
    encrypted: bool = False
    default: Optional[Any] = None


class RecipeInputs(BaseModel):
    """Recipe input definitions."""
    required: list[InputField] = Field(default_factory=list)
    optional: list[InputField] = Field(default_factory=list)


# =============================================================================
# CONDITIONS
# =============================================================================

class FailureAction(BaseModel):
    """Action to take on condition failure."""
    action: str  # goto | skip_to | retry | fail | wait | navigate | branch
    target: Optional[str] = None
    duration_ms: Optional[int] = None
    reason: Optional[str] = None
    extract_error: Optional[str] = None
    conditions: Optional[list[dict[str, Any]]] = None
    default: Optional[dict[str, Any]] = None


class Condition(BaseModel):
    """Pre or post condition."""
    id: Optional[str] = None
    check: str  # element_visible | url_contains | context_value | etc.
    selector: Optional[str] = None
    pattern: Optional[str] = None
    value: Optional[Any] = None
    path: Optional[str] = None
    condition: Optional[str] = None  # equals | length_greater_than | etc.
    conditions: Optional[list["Condition"]] = None  # For any_of / all_of
    on_failure: Optional[FailureAction] = None
    on_success: Optional[FailureAction] = None  # For post-conditions that trigger action on success


# =============================================================================
# ACTIONS
# =============================================================================

class WaitStrategy(BaseModel):
    """Wait strategy for actions."""
    type: str = "visible"  # visible | stable | attached
    stable_ms: int = 500
    max_wait_ms: int = 10000


class ResponseGuard(BaseModel):
    """API Awaiter: Intercepts specific network responses."""
    url_pattern: str
    status: int = 200
    timeout_ms: int = 15000


class MutationGuard(BaseModel):
    """Mutation Observer Guard: Waits for specific DOM changes."""
    selector: str
    type: str = "text"  # text | children | attribute | detached
    attribute_name: Optional[str] = None
    timeout_ms: int = 15000


class Action(BaseModel):
    """Single action within a node."""
    seq: int = 0
    type: ActionType
    intent: Optional[str] = None  # For semantic finding
    url: Optional[str] = None
    value: Optional[str] = None
    selector: Optional[str] = None
    timeout_ms: Optional[int] = None
    store_in: Optional[str] = None
    clear_first: bool = False
    mask_in_logs: bool = False
    wait_strategy: Optional[WaitStrategy] = None
    response_guard: Optional[ResponseGuard] = None
    mutation_guard: Optional[MutationGuard] = None
    path: Optional[str] = None  # For set_context
    message: Optional[str] = None  # For log
    event: Optional[str] = None  # For emit_event
    data: Optional[dict[str, Any]] = None
    state: Optional[str] = None  # For wait_for_load_state
    format: Optional[str] = None  # For create_report
    content: Optional[dict[str, Any]] = None
    columns: Optional[list[str]] = None
    max_rows: Optional[Union[int, str]] = None


# =============================================================================
# EXECUTION CONFIG
# =============================================================================

class RetryConfig(BaseModel):
    """Retry configuration for nodes."""
    max_attempts: int = 3
    backoff_strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    initial_delay_ms: int = 1000
    max_delay_ms: int = 10000
    retry_on: list[str] = Field(default_factory=lambda: ["timeout", "element_not_found", "network_error"])


class ExecutionConfig(BaseModel):
    """Execution configuration for a node."""
    timeout_ms: int = 30000
    retry: Optional[RetryConfig] = None
    resource_tier: ResourceTier = ResourceTier.STANDARD
    isolation: str = "shared"  # shared | dedicated


# =============================================================================
# STATE POLICY
# =============================================================================

class StatePolicy(BaseModel):
    """State/checkpoint policy for a node."""
    checkpoint: bool = False
    hibernate: bool = False
    save: list[str] = Field(default_factory=lambda: ["cookies", "local_storage"])
    checkpoint_id: Optional[str] = None


# =============================================================================
# NODE-SPECIFIC CONFIGS
# =============================================================================

class LoopConfig(BaseModel):
    """Configuration for loop nodes."""
    source: str  # {{ context.items }}
    iterator_var: str = "current_item"
    index_var: str = "index"
    max_iterations: int = 1000
    batch_size: int = 50
    parallel: bool = False
    continue_on_error: bool = True
    checkpoint_every: Optional[int] = None
    body: str  # Node ID to execute for each item
    on_item_error: Optional[dict[str, Any]] = None
    on_complete: Optional[str] = None


class DecisionBranch(BaseModel):
    """A single branch in a decision node."""
    condition: str  # equals | greater_than | contains | etc.
    value: Optional[Any] = None
    field: Optional[str] = None
    target: str  # Node ID to go to


class DecisionConfig(BaseModel):
    """Configuration for decision nodes."""
    source: str  # {{ context.status }}
    branches: list[DecisionBranch]
    default: str  # Default node ID


class GateOption(BaseModel):
    """Option in a human gate."""
    id: str
    label: str
    next: str  # Node ID to go to


class GateTimeout(BaseModel):
    """Timeout configuration for human gates."""
    duration_hours: int = 24
    on_timeout: str  # Node ID to go to


class GateNotification(BaseModel):
    """Notification config for human gates."""
    channels: list[str] = Field(default_factory=lambda: ["email"])
    recipients: list[str] = Field(default_factory=list)


class HumanGateConfig(BaseModel):
    """Configuration for human gate nodes."""
    reason: str
    prompt: str
    options: list[GateOption]
    timeout: GateTimeout
    notification: Optional[GateNotification] = None


class CheckpointSaveConfig(BaseModel):
    """What to save in a checkpoint."""
    browser_state: list[str] = Field(default_factory=lambda: ["cookies", "local_storage", "session_storage"])
    page_state: list[str] = Field(default_factory=lambda: ["url", "scroll_position"])
    context: bool = True
    screenshot: bool = False


class CheckpointConfig(BaseModel):
    """Configuration for checkpoint nodes."""
    id: str
    storage: str = "persistent"  # persistent | memory
    ttl_hours: int = 168  # 1 week
    save: CheckpointSaveConfig = Field(default_factory=CheckpointSaveConfig)
    compression: str = "gzip"
    encryption: bool = True


class ParallelBranch(BaseModel):
    """A branch in a parallel node."""
    id: str
    node: str  # Node ID to execute


class ParallelMerge(BaseModel):
    """Merge strategy for parallel results."""
    strategy: str = "deep_merge"
    target: str  # {{ context.merged_data }}


class ParallelConfig(BaseModel):
    """Configuration for parallel nodes."""
    max_concurrency: int = 3
    branches: list[ParallelBranch]
    join_strategy: JoinStrategy = JoinStrategy.WAIT_ALL
    wait_n_count: int = 2
    on_partial_failure: str = "continue_with_available"
    merge_results: Optional[ParallelMerge] = None


# =============================================================================
# TELEMETRY
# =============================================================================

class TelemetryConfig(BaseModel):
    """Telemetry configuration for a node."""
    emit_events: list[str] = Field(default_factory=lambda: ["node_started", "node_finished"])
    custom_metrics: dict[str, str] = Field(default_factory=dict)


# =============================================================================
# NODE
# =============================================================================

class Node(BaseModel):
    """
    A single node in the recipe DAG.

    Supports all node types through optional fields.
    """
    id: str
    type: NodeType = NodeType.ACTION
    name: str = ""
    description: str = ""

    # Execution config (required for all nodes)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    # Conditions (optional)
    pre_conditions: list[Condition] = Field(default_factory=list)
    post_conditions: list[Condition] = Field(default_factory=list)

    # Actions (for action nodes)
    actions: list[Action] = Field(default_factory=list)

    # State policy (optional)
    state_policy: Optional[StatePolicy] = None

    # Telemetry (optional)
    telemetry: Optional[TelemetryConfig] = None

    # Node-specific configs (only one should be set based on type)
    loop: Optional[LoopConfig] = None
    evaluate: Optional[DecisionConfig] = None
    gate: Optional[HumanGateConfig] = None
    checkpoint: Optional[CheckpointConfig] = None
    parallel: Optional[ParallelConfig] = None

    @field_validator("loop", mode="before")
    @classmethod
    def validate_loop(cls, v, info):
        """Ensure loop config is only on loop nodes."""
        return v

    @field_validator("evaluate", mode="before")
    @classmethod
    def validate_evaluate(cls, v, info):
        """Ensure evaluate config is only on decision nodes."""
        return v


# =============================================================================
# EDGE
# =============================================================================

class EdgeCondition(BaseModel):
    """Condition for conditional edges."""
    type: str  # post_condition_failed | expression
    post_condition_id: Optional[str] = None
    failure_reason: Optional[str] = None
    expression: Optional[str] = None


class Edge(BaseModel):
    """An edge connecting two nodes."""
    id: Optional[str] = None
    source: str = Field(alias="from")  # "from" is Python reserved
    target: str = Field(alias="to")
    type: Optional[str] = None  # loop_body | loop_continue | null
    condition: Optional[EdgeCondition] = None

    class Config:
        populate_by_name = True


# =============================================================================
# GLOBAL GUARDS
# =============================================================================

class ModalDetector(BaseModel):
    """Modal/popup detection configuration."""
    enabled: bool = True
    selectors: list[str] = Field(default_factory=list)
    action: str = "dismiss_or_escalate"
    dismiss_strategies: list[dict[str, Any]] = Field(default_factory=list)
    max_dismiss_attempts: int = 3
    on_dismiss_failure: Optional[dict[str, Any]] = None


class SessionValidator(BaseModel):
    """Session validation configuration."""
    check_interval_ms: int = 30000
    indicator: str = "element_visible"
    selector: str = ""
    on_failure: str = ""  # goto:node_login


class CaptchaDetector(BaseModel):
    """CAPTCHA detection configuration."""
    enabled: bool = True
    selectors: list[str] = Field(default_factory=list)
    on_detection: dict[str, Any] = Field(default_factory=dict)


class GlobalGuards(BaseModel):
    """Global guard configurations."""
    modal_detector: Optional[ModalDetector] = None
    session_validator: Optional[SessionValidator] = None
    captcha_detector: Optional[CaptchaDetector] = None


# =============================================================================
# EXIT POINTS
# =============================================================================

class ExitPoints(BaseModel):
    """Recipe exit points."""
    success: str
    failure: str
    timeout: str


# =============================================================================
# CONTEXT
# =============================================================================

class RecipeContext(BaseModel):
    """Initial context configuration."""
    description: str = "Shared state across all nodes"
    initial: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# RECIPE (ROOT)
# =============================================================================

class Recipe(BaseModel):
    """
    The root Recipe model - Universal Recipe Schema v2.0.

    Example:
        recipe = Recipe.model_validate(json_data)
        print(recipe.metadata.name)
        for node in recipe.nodes:
            print(f"  - {node.id}: {node.type}")
    """
    schema_url: Optional[str] = Field(None, alias="$schema")
    version: str = "2.0.0"

    metadata: RecipeMetadata
    config: RecipeConfig = Field(default_factory=RecipeConfig)
    inputs: RecipeInputs = Field(default_factory=RecipeInputs)
    context: RecipeContext = Field(default_factory=RecipeContext)

    nodes: list[Node]
    edges: list[Edge] = Field(default_factory=list)

    entry_point: str
    exit_points: ExitPoints

    global_guards: Optional[GlobalGuards] = None

    class Config:
        populate_by_name = True

    def get_node(self, node_id: str) -> Optional[Node]:
        """Get a node by ID."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_outgoing_edges(self, node_id: str) -> list[Edge]:
        """Get all edges originating from a node."""
        return [e for e in self.edges if e.source == node_id]


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def load_recipe(json_data: dict[str, Any]) -> Recipe:
    """
    Load and validate a recipe from JSON.

    Args:
        json_data: Recipe JSON object

    Returns:
        Validated Recipe model

    Raises:
        ValidationError: If schema is invalid
    """
    return Recipe.model_validate(json_data)


def dump_recipe(recipe: Recipe) -> dict[str, Any]:
    """
    Serialize a recipe to JSON-compatible dict.

    Args:
        recipe: Recipe model

    Returns:
        JSON-serializable dictionary
    """
    return recipe.model_dump(by_alias=True, exclude_none=True)


# =============================================================================
# EXAMPLE
# =============================================================================

if __name__ == "__main__":
    import json

    print("=" * 60)
    print("RECIPE SCHEMA - Pydantic Models Test")
    print("=" * 60)

    # Test with minimal recipe
    test_json = {
        "version": "2.0.0",
        "metadata": {
            "id": "test-123",
            "name": "test_recipe",
            "description": "A test recipe"
        },
        "nodes": [
            {
                "id": "node_start",
                "type": "action",
                "name": "Start",
                "execution": {"timeout_ms": 30000},
                "actions": [
                    {"seq": 1, "type": "navigate", "url": "https://example.com"}
                ],
                "post_conditions": [
                    {"check": "page_loaded", "on_failure": {"action": "retry"}}
                ]
            }
        ],
        "edges": [],
        "entry_point": "node_start",
        "exit_points": {
            "success": "node_start",
            "failure": "node_start",
            "timeout": "node_start"
        }
    }

    try:
        recipe = load_recipe(test_json)
        print(f"\n✅ Recipe loaded: {recipe.metadata.name}")
        print(f"   Version: {recipe.version}")
        print(f"   Nodes: {len(recipe.nodes)}")
        print(f"   Entry: {recipe.entry_point}")

        for node in recipe.nodes:
            print(f"\n   Node: {node.id} ({node.type.value})")
            for action in node.actions:
                print(f"     - {action.type}: {action.url or action.intent or ''}")

    except Exception as e:
        print(f"\n❌ Validation failed: {e}")

    print("\n" + "=" * 60)
