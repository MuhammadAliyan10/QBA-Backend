"""
recipeValidator.py - Universal Recipe Schema v2.0 Validator

Enforces the 15 non-negotiable rules for enterprise-grade recipe safety.
This is the GATE that prevents broken recipes from ever reaching execution.

Usage:
    from core.recipeValidator import RecipeValidator, ValidationResult

    validator = RecipeValidator()
    result = validator.validate(recipe_json)

    if not result.is_valid:
        for error in result.errors:
            print(f"❌ {error}")
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Set, Tuple
from enum import Enum

logger = logging.getLogger("recipeValidator")


# =============================================================================
# CONSTANTS - The Hard Limits
# =============================================================================

MAX_LOOP_ITERATIONS = 10000
DEFAULT_LOOP_ITERATIONS = 1000
MAX_TIMEOUT_MS = 300000  # 5 minutes
DEFAULT_TIMEOUT_MS = 30000  # 30 seconds
MAX_RETRY_ATTEMPTS = 5
MAX_PARALLEL_CONCURRENCY = 5
MAX_CHECKPOINT_INTERVAL = 50
MIN_CHECKPOINT_INTERVAL_FOR_LARGE_LOOPS = 100


class Severity(Enum):
    """Validation error severity levels."""
    ERROR = "ERROR"      # Recipe CANNOT execute
    WARNING = "WARNING"  # Recipe CAN execute but has issues
    INFO = "INFO"        # Informational only


@dataclass
class ValidationError:
    """Represents a single validation error."""
    rule_id: int
    rule_name: str
    severity: Severity
    message: str
    node_id: Optional[str] = None
    suggestion: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of recipe validation."""
    is_valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    recipe_name: str = ""

    def add_error(self, error: ValidationError):
        if error.severity == Severity.ERROR:
            self.errors.append(error)
            self.is_valid = False
        else:
            self.warnings.append(error)

    def summary(self) -> str:
        """Generate human-readable summary."""
        if self.is_valid:
            return f"✅ Recipe '{self.recipe_name}' PASSED ({len(self.warnings)} warnings)"
        return f"❌ Recipe '{self.recipe_name}' FAILED ({len(self.errors)} errors, {len(self.warnings)} warnings)"


# =============================================================================
# VALIDATOR CLASS
# =============================================================================

class RecipeValidator:
    """
    The Gate - Enforces 15 strict rules for recipe safety.

    Rules Enforced:
    1.  Loop Limits - max_iterations required
    2.  Timeout Mandatory - timeout_ms on every node
    3.  Post-Condition Required - action nodes need validation
    4.  Checkpoint After Login - auth nodes must checkpoint
    5.  No Orphan Nodes - all nodes must be reachable
    6.  Exit Coverage - success/failure/timeout defined
    7.  Secret Masking - passwords must have mask_in_logs
    8.  Retry Limits - max_attempts <= 5
    9.  Human Gate Timeout - human gates need timeout
    10. Loop Checkpointing - large loops need checkpoints
    11. Error Storage - loops need on_item_error storage
    12. Parallel Limits - max_concurrency <= 5
    13. Variable Validation - context vars must be defined
    14. No Circular Dependencies - DAG must be acyclic
    15. No Self-Referential Fallbacks - prevent infinite loops
    """

    def __init__(self):
        self.variable_pattern = re.compile(r'\{\{\s*([\w.]+)\s*\}\}')
        self.defined_variables: Set[str] = set()
        self.node_ids: Set[str] = set()
        self.edges: List[Dict] = []
        self.nodes: Dict[str, Dict] = {}

    def validate(self, recipe: Dict) -> ValidationResult:
        """
        Validate a recipe against all 15 rules.

        Args:
            recipe: The recipe JSON object

        Returns:
            ValidationResult with all errors and warnings
        """
        result = ValidationResult(
            is_valid=True,
            recipe_name=recipe.get("metadata", {}).get("name", "unknown")
        )

        # Reset state
        self.defined_variables = set()
        self.node_ids = set()
        self.edges = recipe.get("edges", [])
        self.nodes = {}

        # Build node lookup
        for node in recipe.get("nodes", []):
            node_id = node.get("id", "")
            self.node_ids.add(node_id)
            self.nodes[node_id] = node

        # Extract initial context variables
        initial_context = recipe.get("context", {}).get("initial", {})
        for key in initial_context.keys():
            self.defined_variables.add(f"context.{key}")

        # Extract input variables
        for inp in recipe.get("inputs", {}).get("required", []):
            self.defined_variables.add(f"inputs.{inp.get('name', '')}")
        for inp in recipe.get("inputs", {}).get("optional", []):
            self.defined_variables.add(f"inputs.{inp.get('name', '')}")

        # Run all 15 rules
        self._rule_1_loop_limits(recipe, result)
        self._rule_2_timeout_mandatory(recipe, result)
        self._rule_3_post_condition_required(recipe, result)
        self._rule_4_checkpoint_after_login(recipe, result)
        self._rule_5_no_orphan_nodes(recipe, result)
        self._rule_6_exit_coverage(recipe, result)
        self._rule_7_secret_masking(recipe, result)
        self._rule_8_retry_limits(recipe, result)
        self._rule_9_human_gate_timeout(recipe, result)
        self._rule_10_loop_checkpointing(recipe, result)
        self._rule_11_error_storage(recipe, result)
        self._rule_12_parallel_limits(recipe, result)
        self._rule_13_variable_validation(recipe, result)
        self._rule_14_no_circular_dependencies(recipe, result)
        self._rule_15_no_self_referential_fallbacks(recipe, result)

        logger.info(result.summary())
        return result

    # -------------------------------------------------------------------------
    # RULE 1: Loop Limits
    # -------------------------------------------------------------------------
    def _rule_1_loop_limits(self, recipe: Dict, result: ValidationResult):
        """Every loop MUST have max_iterations (default: 1000, max: 10000)."""
        for node in recipe.get("nodes", []):
            if node.get("type") == "loop":
                loop_config = node.get("loop", {})
                max_iter = loop_config.get("max_iterations")

                if max_iter is None:
                    result.add_error(ValidationError(
                        rule_id=1,
                        rule_name="Loop Limits",
                        severity=Severity.ERROR,
                        message=f"Loop node '{node.get('id')}' missing max_iterations",
                        node_id=node.get("id"),
                        suggestion=f"Add 'max_iterations: {DEFAULT_LOOP_ITERATIONS}'"
                    ))
                elif max_iter > MAX_LOOP_ITERATIONS:
                    result.add_error(ValidationError(
                        rule_id=1,
                        rule_name="Loop Limits",
                        severity=Severity.ERROR,
                        message=f"Loop '{node.get('id')}' max_iterations ({max_iter}) exceeds limit ({MAX_LOOP_ITERATIONS})",
                        node_id=node.get("id"),
                        suggestion=f"Reduce to {MAX_LOOP_ITERATIONS} or less"
                    ))

    # -------------------------------------------------------------------------
    # RULE 2: Timeout Mandatory
    # -------------------------------------------------------------------------
    def _rule_2_timeout_mandatory(self, recipe: Dict, result: ValidationResult):
        """Every node MUST have timeout_ms (default: 30000ms, max: 300000ms)."""
        for node in recipe.get("nodes", []):
            execution = node.get("execution", {})
            timeout = execution.get("timeout_ms")

            if timeout is None:
                result.add_error(ValidationError(
                    rule_id=2,
                    rule_name="Timeout Mandatory",
                    severity=Severity.ERROR,
                    message=f"Node '{node.get('id')}' missing timeout_ms",
                    node_id=node.get("id"),
                    suggestion=f"Add 'execution.timeout_ms: {DEFAULT_TIMEOUT_MS}'"
                ))
            elif timeout > MAX_TIMEOUT_MS:
                result.add_error(ValidationError(
                    rule_id=2,
                    rule_name="Timeout Mandatory",
                    severity=Severity.WARNING,
                    message=f"Node '{node.get('id')}' timeout ({timeout}ms) exceeds recommended limit ({MAX_TIMEOUT_MS}ms)",
                    node_id=node.get("id"),
                    suggestion="Consider breaking into smaller steps"
                ))

    # -------------------------------------------------------------------------
    # RULE 3: Post-Condition Required
    # -------------------------------------------------------------------------
    def _rule_3_post_condition_required(self, recipe: Dict, result: ValidationResult):
        """Every action node MUST have at least one post_condition."""
        for node in recipe.get("nodes", []):
            if node.get("type") == "action":
                post_conditions = node.get("post_conditions", [])

                if not post_conditions:
                    result.add_error(ValidationError(
                        rule_id=3,
                        rule_name="Post-Condition Required",
                        severity=Severity.ERROR,
                        message=f"Action node '{node.get('id')}' has no post_conditions",
                        node_id=node.get("id"),
                        suggestion="Add at least one post_condition to validate success"
                    ))

    # -------------------------------------------------------------------------
    # RULE 4: Checkpoint After Login
    # -------------------------------------------------------------------------
    def _rule_4_checkpoint_after_login(self, recipe: Dict, result: ValidationResult):
        """Any authentication node MUST set state_policy.checkpoint: true."""
        auth_keywords = ["login", "auth", "signin", "sign_in", "authenticate", "credential"]

        for node in recipe.get("nodes", []):
            node_id = node.get("id", "").lower()
            node_name = node.get("name", "").lower()

            is_auth_node = any(kw in node_id or kw in node_name for kw in auth_keywords)

            if is_auth_node:
                state_policy = node.get("state_policy", {})
                if not state_policy.get("checkpoint", False):
                    result.add_error(ValidationError(
                        rule_id=4,
                        rule_name="Checkpoint After Login",
                        severity=Severity.ERROR,
                        message=f"Auth node '{node.get('id')}' must enable checkpoint",
                        node_id=node.get("id"),
                        suggestion="Add 'state_policy.checkpoint: true'"
                    ))

    # -------------------------------------------------------------------------
    # RULE 5: No Orphan Nodes
    # -------------------------------------------------------------------------
    def _rule_5_no_orphan_nodes(self, recipe: Dict, result: ValidationResult):
        """Every node (except entry_point) MUST have at least one incoming edge."""
        entry_point = recipe.get("entry_point", "")
        exit_points = recipe.get("exit_points", {})

        # Build set of nodes that have incoming edges
        nodes_with_incoming = set()
        for edge in self.edges:
            nodes_with_incoming.add(edge.get("to", ""))

        # Also consider loop body references as connections
        for node in recipe.get("nodes", []):
            if node.get("type") == "loop":
                body = node.get("loop", {}).get("body")
                if body:
                    nodes_with_incoming.add(body)
                on_complete = node.get("loop", {}).get("on_complete")
                if on_complete:
                    nodes_with_incoming.add(on_complete)

        # Check for orphans
        for node_id in self.node_ids:
            if node_id == entry_point:
                continue
            if node_id in exit_points.values():
                continue
            if node_id not in nodes_with_incoming:
                result.add_error(ValidationError(
                    rule_id=5,
                    rule_name="No Orphan Nodes",
                    severity=Severity.WARNING,
                    message=f"Node '{node_id}' has no incoming edges (orphan)",
                    node_id=node_id,
                    suggestion="Add an edge pointing to this node or remove it"
                ))

    # -------------------------------------------------------------------------
    # RULE 6: Exit Coverage
    # -------------------------------------------------------------------------
    def _rule_6_exit_coverage(self, recipe: Dict, result: ValidationResult):
        """Recipe MUST define all three exit_points: success, failure, timeout."""
        exit_points = recipe.get("exit_points", {})
        required_exits = ["success", "failure", "timeout"]

        for exit_type in required_exits:
            if exit_type not in exit_points:
                result.add_error(ValidationError(
                    rule_id=6,
                    rule_name="Exit Coverage",
                    severity=Severity.ERROR,
                    message=f"Missing exit_point: '{exit_type}'",
                    suggestion=f"Add 'exit_points.{exit_type}' to the recipe"
                ))

    # -------------------------------------------------------------------------
    # RULE 7: Secret Masking
    # -------------------------------------------------------------------------
    def _rule_7_secret_masking(self, recipe: Dict, result: ValidationResult):
        """Actions with passwords/tokens MUST set mask_in_logs: true."""
        secret_keywords = ["password", "pwd", "secret", "token", "api_key", "apikey", "credential"]

        for node in recipe.get("nodes", []):
            for action in node.get("actions", []):
                intent = str(action.get("intent", "")).lower()
                value = str(action.get("value", "")).lower()

                is_secret = any(kw in intent or kw in value for kw in secret_keywords)

                if is_secret and not action.get("mask_in_logs", False):
                    result.add_error(ValidationError(
                        rule_id=7,
                        rule_name="Secret Masking",
                        severity=Severity.ERROR,
                        message=f"Action in '{node.get('id')}' handles secrets but missing mask_in_logs",
                        node_id=node.get("id"),
                        suggestion="Add 'mask_in_logs: true' to the action"
                    ))

    # -------------------------------------------------------------------------
    # RULE 8: Retry Limits
    # -------------------------------------------------------------------------
    def _rule_8_retry_limits(self, recipe: Dict, result: ValidationResult):
        """retry.max_attempts MUST NOT exceed 5."""
        for node in recipe.get("nodes", []):
            retry = node.get("execution", {}).get("retry", {})
            max_attempts = retry.get("max_attempts", 0)

            if max_attempts > MAX_RETRY_ATTEMPTS:
                result.add_error(ValidationError(
                    rule_id=8,
                    rule_name="Retry Limits",
                    severity=Severity.ERROR,
                    message=f"Node '{node.get('id')}' max_attempts ({max_attempts}) exceeds limit ({MAX_RETRY_ATTEMPTS})",
                    node_id=node.get("id"),
                    suggestion=f"Reduce to {MAX_RETRY_ATTEMPTS} or less"
                ))

    # -------------------------------------------------------------------------
    # RULE 9: Human Gate Timeout
    # -------------------------------------------------------------------------
    def _rule_9_human_gate_timeout(self, recipe: Dict, result: ValidationResult):
        """Every human_gate MUST have a timeout with on_timeout action."""
        for node in recipe.get("nodes", []):
            if node.get("type") == "human_gate":
                gate = node.get("gate", {})
                timeout = gate.get("timeout", {})

                if not timeout:
                    result.add_error(ValidationError(
                        rule_id=9,
                        rule_name="Human Gate Timeout",
                        severity=Severity.ERROR,
                        message=f"Human gate '{node.get('id')}' missing timeout configuration",
                        node_id=node.get("id"),
                        suggestion="Add 'gate.timeout.duration_hours' and 'gate.timeout.on_timeout'"
                    ))
                elif not timeout.get("on_timeout"):
                    result.add_error(ValidationError(
                        rule_id=9,
                        rule_name="Human Gate Timeout",
                        severity=Severity.ERROR,
                        message=f"Human gate '{node.get('id')}' timeout missing on_timeout action",
                        node_id=node.get("id"),
                        suggestion="Add 'gate.timeout.on_timeout' node reference"
                    ))

    # -------------------------------------------------------------------------
    # RULE 10: Loop Checkpointing
    # -------------------------------------------------------------------------
    def _rule_10_loop_checkpointing(self, recipe: Dict, result: ValidationResult):
        """Loops with 100+ iterations MUST set checkpoint_every: N where N <= 50."""
        for node in recipe.get("nodes", []):
            if node.get("type") == "loop":
                loop_config = node.get("loop", {})
                max_iter = loop_config.get("max_iterations", 0)
                checkpoint_every = loop_config.get("checkpoint_every")

                if max_iter >= MIN_CHECKPOINT_INTERVAL_FOR_LARGE_LOOPS:
                    if checkpoint_every is None:
                        result.add_error(ValidationError(
                            rule_id=10,
                            rule_name="Loop Checkpointing",
                            severity=Severity.ERROR,
                            message=f"Loop '{node.get('id')}' with {max_iter} iterations must have checkpoint_every",
                            node_id=node.get("id"),
                            suggestion=f"Add 'loop.checkpoint_every: 25' (max: {MAX_CHECKPOINT_INTERVAL})"
                        ))
                    elif checkpoint_every > MAX_CHECKPOINT_INTERVAL:
                        result.add_error(ValidationError(
                            rule_id=10,
                            rule_name="Loop Checkpointing",
                            severity=Severity.WARNING,
                            message=f"Loop '{node.get('id')}' checkpoint_every ({checkpoint_every}) is high",
                            node_id=node.get("id"),
                            suggestion=f"Reduce to {MAX_CHECKPOINT_INTERVAL} or less for safety"
                        ))

    # -------------------------------------------------------------------------
    # RULE 11: Error Storage
    # -------------------------------------------------------------------------
    def _rule_11_error_storage(self, recipe: Dict, result: ValidationResult):
        """Loops MUST define on_item_error with storage location."""
        for node in recipe.get("nodes", []):
            if node.get("type") == "loop":
                loop_config = node.get("loop", {})
                on_item_error = loop_config.get("on_item_error", {})

                if not on_item_error:
                    result.add_error(ValidationError(
                        rule_id=11,
                        rule_name="Error Storage",
                        severity=Severity.WARNING,
                        message=f"Loop '{node.get('id')}' missing on_item_error configuration",
                        node_id=node.get("id"),
                        suggestion="Add 'loop.on_item_error' with 'store_in' field"
                    ))
                elif not on_item_error.get("store_in"):
                    result.add_error(ValidationError(
                        rule_id=11,
                        rule_name="Error Storage",
                        severity=Severity.WARNING,
                        message=f"Loop '{node.get('id')}' on_item_error missing store_in",
                        node_id=node.get("id"),
                        suggestion="Add 'on_item_error.store_in: context.failed_ids'"
                    ))

    # -------------------------------------------------------------------------
    # RULE 12: Parallel Limits
    # -------------------------------------------------------------------------
    def _rule_12_parallel_limits(self, recipe: Dict, result: ValidationResult):
        """parallel.max_concurrency MUST NOT exceed 5."""
        for node in recipe.get("nodes", []):
            if node.get("type") == "parallel":
                parallel = node.get("parallel", {})
                max_concurrency = parallel.get("max_concurrency", 1)

                if max_concurrency > MAX_PARALLEL_CONCURRENCY:
                    result.add_error(ValidationError(
                        rule_id=12,
                        rule_name="Parallel Limits",
                        severity=Severity.ERROR,
                        message=f"Node '{node.get('id')}' max_concurrency ({max_concurrency}) exceeds limit ({MAX_PARALLEL_CONCURRENCY})",
                        node_id=node.get("id"),
                        suggestion=f"Reduce to {MAX_PARALLEL_CONCURRENCY} or less"
                    ))

    # -------------------------------------------------------------------------
    # RULE 13: Variable Validation
    # -------------------------------------------------------------------------
    def _rule_13_variable_validation(self, recipe: Dict, result: ValidationResult):
        """Verify {{ context.X }} is defined before use."""
        # First pass: collect all variables that get set
        for node in recipe.get("nodes", []):
            for action in node.get("actions", []):
                store_in = action.get("store_in", "")
                if store_in:
                    self.defined_variables.add(store_in)

                # set_context actions also define variables
                if action.get("type") == "set_context":
                    path = action.get("path", "")
                    if path:
                        self.defined_variables.add(path)

        # Add standard system variables
        system_vars = ["loop.index", "loop.item", "node.id", "node.duration",
                      "timestamp", "uuid", "extract"]
        for var in system_vars:
            self.defined_variables.add(var)

        # Second pass: check all variable usages
        recipe_str = str(recipe)
        variables_used = self.variable_pattern.findall(recipe_str)

        for var in variables_used:
            # Normalize variable path (remove array indices and complex expressions)
            base_var = var.split("|")[0].strip()  # Remove pipe filters
            base_var = re.sub(r'\[\d+\]', '', base_var)  # Remove array indices
            base_var = re.sub(r'\s*[+\-*/><]=?\s*\d+', '', base_var)  # Remove arithmetic

            if base_var and not self._is_variable_defined(base_var):
                result.add_error(ValidationError(
                    rule_id=13,
                    rule_name="Variable Validation",
                    severity=Severity.WARNING,
                    message=f"Variable '{{{{ {var} }}}}' may not be defined",
                    suggestion=f"Ensure '{base_var}' is initialized in context.initial or set by a prior node"
                ))

    def _is_variable_defined(self, var: str) -> bool:
        """Check if a variable path is defined."""
        # Check exact match
        if var in self.defined_variables:
            return True

        # Check parent paths (e.g., context.current_invoice.id -> context.current_invoice)
        parts = var.split(".")
        for i in range(len(parts) - 1, 0, -1):
            parent = ".".join(parts[:i])
            if parent in self.defined_variables:
                return True

        return False

    # -------------------------------------------------------------------------
    # RULE 14: No Circular Dependencies
    # -------------------------------------------------------------------------
    def _rule_14_no_circular_dependencies(self, recipe: Dict, result: ValidationResult):
        """Edges MUST NOT create cycles (except loop_continue)."""
        # Build adjacency list excluding loop_continue edges
        graph: Dict[str, List[str]] = {node_id: [] for node_id in self.node_ids}

        for edge in self.edges:
            if edge.get("type") == "loop_continue":
                continue  # Allowed cycle
            from_node = edge.get("from", "")
            to_node = edge.get("to", "")
            if from_node in graph:
                graph[from_node].append(to_node)

        # Detect cycles using DFS
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node: WHITE for node in graph}

        def has_cycle(node: str) -> bool:
            color[node] = GRAY
            for neighbor in graph.get(node, []):
                if color.get(neighbor) == GRAY:
                    return True
                if color.get(neighbor) == WHITE and has_cycle(neighbor):
                    return True
            color[node] = BLACK
            return False

        for node in graph:
            if color[node] == WHITE:
                if has_cycle(node):
                    result.add_error(ValidationError(
                        rule_id=14,
                        rule_name="No Circular Dependencies",
                        severity=Severity.ERROR,
                        message="Recipe contains circular dependencies (non-loop cycle detected)",
                        suggestion="Review edge connections or mark intentional loops as 'type: loop_continue'"
                    ))
                    break

    # -------------------------------------------------------------------------
    # RULE 15: No Self-Referential Fallbacks
    # -------------------------------------------------------------------------
    def _rule_15_no_self_referential_fallbacks(self, recipe: Dict, result: ValidationResult):
        """on_failure MUST NOT point to the same node (infinite loop)."""
        for node in recipe.get("nodes", []):
            node_id = node.get("id", "")

            # Check post_conditions fallbacks
            for post_cond in node.get("post_conditions", []):
                on_failure = post_cond.get("on_failure", {})
                target = on_failure.get("target", "")

                if target == node_id:
                    result.add_error(ValidationError(
                        rule_id=15,
                        rule_name="No Self-Referential Fallbacks",
                        severity=Severity.ERROR,
                        message=f"Node '{node_id}' on_failure points to itself (infinite loop)",
                        node_id=node_id,
                        suggestion="Point to a different recovery node"
                    ))

                # Check nested conditions
                for condition in on_failure.get("conditions", []):
                    then_target = condition.get("then", {}).get("target", "")
                    if then_target == node_id:
                        result.add_error(ValidationError(
                            rule_id=15,
                            rule_name="No Self-Referential Fallbacks",
                            severity=Severity.ERROR,
                            message=f"Node '{node_id}' conditional fallback points to itself",
                            node_id=node_id,
                            suggestion="Point to a different recovery node"
                        ))


# =============================================================================
# STANDALONE VALIDATOR CLI
# =============================================================================

def validate_recipe_file(filepath: str) -> ValidationResult:
    """
    Validate a recipe JSON file.

    Args:
        filepath: Path to recipe JSON file

    Returns:
        ValidationResult
    """
    import json

    with open(filepath, 'r') as f:
        recipe = json.load(f)

    validator = RecipeValidator()
    return validator.validate(recipe)


if __name__ == "__main__":
    import sys
    import json

    print("=" * 60)
    print("RECIPE VALIDATOR v2.0 - 15 Rule Enforcement")
    print("=" * 60)

    if len(sys.argv) < 2:
        print("\nUsage: python recipeValidator.py <recipe.json>")
        print("\nRunning self-test with sample recipe...")

        # Self-test with a sample recipe that has issues
        test_recipe = {
            "metadata": {"name": "test_recipe"},
            "context": {"initial": {"items": []}},
            "inputs": {"required": [], "optional": []},
            "nodes": [
                {
                    "id": "node_login",
                    "type": "action",
                    "name": "Login",
                    "actions": [
                        {"type": "find_and_type", "intent": "password", "value": "{{ inputs.password }}"}
                    ],
                    "execution": {}  # Missing timeout_ms
                    # Missing post_conditions
                    # Missing state_policy.checkpoint
                },
                {
                    "id": "node_loop",
                    "type": "loop",
                    "loop": {
                        "source": "{{ context.items }}"
                        # Missing max_iterations
                    },
                    "execution": {"timeout_ms": 60000}
                }
            ],
            "edges": [
                {"from": "node_login", "to": "node_loop"}
            ],
            "entry_point": "node_login",
            "exit_points": {
                "success": "node_loop"
                # Missing failure and timeout
            }
        }

        validator = RecipeValidator()
        result = validator.validate(test_recipe)

        print(f"\n{result.summary()}\n")

        if result.errors:
            print("ERRORS:")
            for err in result.errors:
                print(f"  ❌ Rule {err.rule_id}: {err.message}")
                if err.suggestion:
                    print(f"     💡 {err.suggestion}")

        if result.warnings:
            print("\nWARNINGS:")
            for warn in result.warnings:
                print(f"  ⚠️  Rule {warn.rule_id}: {warn.message}")

    else:
        filepath = sys.argv[1]
        result = validate_recipe_file(filepath)

        print(f"\n{result.summary()}\n")

        for err in result.errors:
            print(f"❌ Rule {err.rule_id} ({err.rule_name}): {err.message}")
            if err.node_id:
                print(f"   Node: {err.node_id}")
            if err.suggestion:
                print(f"   💡 {err.suggestion}")

        for warn in result.warnings:
            print(f"⚠️  Rule {warn.rule_id} ({warn.rule_name}): {warn.message}")

        sys.exit(0 if result.is_valid else 1)
