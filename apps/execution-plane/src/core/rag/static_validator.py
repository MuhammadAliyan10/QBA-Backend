"""
staticValidator.py - Static Recipe Validator (Logic Gate)

Layer 2 of the Preflight Pipeline:
- Validates recipe JSON WITHOUT launching a browser
- Checks for logical fallacies (infinite loops, orphan nodes)
- Ensures all {{ variables }} are properly defined
- Validates against Pydantic RecipeSchema

Author: Quanta Box Paradox Engineering
Version: 1.0.0
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Optional, Tuple
from enum import Enum

logger = logging.getLogger("staticValidator")


# =============================================================================
# EXCEPTIONS
# =============================================================================

class RecipeValidationError(Exception):
    """Raised when static validation fails."""

    def __init__(self, message: str, errors: list["ValidationIssue"]):
        self.message = message
        self.errors = errors
        super().__init__(self.message)

    def __str__(self):
        error_lines = [f"  - {e.message}" for e in self.errors[:5]]
        return f"{self.message}\n" + "\n".join(error_lines)


# =============================================================================
# DATA CLASSES
# =============================================================================

class IssueSeverity(Enum):
    ERROR = "ERROR"      # Block execution
    WARNING = "WARNING"  # Allow with caution
    INFO = "INFO"        # Informational


@dataclass
class ValidationIssue:
    """A single validation issue."""
    code: str           # E001, W002, etc.
    severity: IssueSeverity
    message: str
    node_id: Optional[str] = None
    suggestion: Optional[str] = None

    def is_blocking(self) -> bool:
        return self.severity == IssueSeverity.ERROR


@dataclass
class ValidationResult:
    """Result of static validation."""
    is_valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)

    def add_error(self, code: str, message: str, **kwargs):
        self.issues.append(ValidationIssue(
            code=code,
            severity=IssueSeverity.ERROR,
            message=message,
            **kwargs
        ))
        self.is_valid = False

    def add_warning(self, code: str, message: str, **kwargs):
        self.issues.append(ValidationIssue(
            code=code,
            severity=IssueSeverity.WARNING,
            message=message,
            **kwargs
        ))

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.WARNING]


# =============================================================================
# STATIC VALIDATOR
# =============================================================================

class StaticValidator:
    """
    Static Recipe Validator - The Logic Gate.

    Performs these checks WITHOUT launching a browser:

    1. Schema Compliance - Validates against Pydantic models
    2. Variable Integrity - All {{ variables }} must be defined
    3. Graph Topology - Valid DAG, no orphan nodes
    4. Loop Safety - All loops have max_iterations
    5. Reachability - All nodes are reachable from entry_point
    """

    def __init__(self):
        self.variable_pattern = re.compile(r'\{\{\s*([\w.|\s+\-*/]+)\s*\}\}')

    def validate(self, recipe: Dict) -> ValidationResult:
        """
        Validate a recipe statically.

        Args:
            recipe: Recipe JSON dictionary

        Returns:
            ValidationResult with all issues

        Raises:
            RecipeValidationError: If blocking errors found
        """
        result = ValidationResult()

        # 1. Schema Compliance
        self._check_schema_compliance(recipe, result)
        if not result.is_valid:
            # Stop early if schema is broken
            raise RecipeValidationError(
                "Recipe schema validation failed",
                result.errors
            )

        # 2. Variable Integrity
        self._check_variable_integrity(recipe, result)

        # 3. Graph Topology
        self._check_graph_topology(recipe, result)

        # 4. Loop Safety
        self._check_loop_safety(recipe, result)

        # 5. Reachability
        self._check_reachability(recipe, result)

        # 6. Timeout Coverage
        self._check_timeout_coverage(recipe, result)

        # Log summary
        logger.info(f"[StaticValidator] Complete: {len(result.errors)} errors, "
                   f"{len(result.warnings)} warnings")

        if not result.is_valid:
            raise RecipeValidationError(
                f"Static validation failed with {len(result.errors)} errors",
                result.errors
            )

        return result

    # -------------------------------------------------------------------------
    # CHECK 1: Schema Compliance
    # -------------------------------------------------------------------------

    def _check_schema_compliance(self, recipe: Dict, result: ValidationResult):
        """Validate against Pydantic RecipeSchema."""
        try:
            from core.recipe.recipe_schema import Recipe
            Recipe.model_validate(recipe)
            logger.debug("[StaticValidator] Schema validation passed")
        except Exception as e:
            result.add_error(
                code="E001",
                message=f"Schema validation failed: {str(e)[:200]}"
            )

    # -------------------------------------------------------------------------
    # CHECK 2: Variable Integrity
    # -------------------------------------------------------------------------

    def _check_variable_integrity(self, recipe: Dict, result: ValidationResult):
        """Ensure all {{ variables }} are defined before use."""

        # Collect defined variables
        defined: set[str] = set()

        # From context.initial
        for key in recipe.get("context", {}).get("initial", {}).keys():
            defined.add(f"context.{key}")

        # From inputs
        for inp in recipe.get("inputs", {}).get("required", []):
            defined.add(f"inputs.{inp.get('name', '')}")
        for inp in recipe.get("inputs", {}).get("optional", []):
            defined.add(f"inputs.{inp.get('name', '')}")

        # From actions that set values
        for node in recipe.get("nodes", []):
            for action in node.get("actions", []):
                if action.get("store_in"):
                    defined.add(action["store_in"])
                if action.get("type") == "set_context" and action.get("path"):
                    defined.add(action["path"])

        # System variables (always available)
        system_vars = {
            "loop.index", "loop.item", "loop.current",
            "node.id", "node.duration",
            "timestamp", "uuid", "extract"
        }
        defined.update(system_vars)

        # Find all variable usages
        recipe_str = str(recipe)
        usages = self.variable_pattern.findall(recipe_str)

        for var_expr in usages:
            # Clean up the variable expression
            var = var_expr.split("|")[0].strip()  # Remove filters
            var = re.sub(r'\s*[+\-*/]\s*\d+', '', var)  # Remove arithmetic
            var = re.sub(r'\[[\d\w]+\]', '', var)  # Remove array indices

            if var and not self._is_defined(var, defined):
                result.add_warning(
                    code="W001",
                    message=f"Variable '{{{{ {var} }}}}' may not be defined",
                    suggestion=f"Ensure '{var}' is initialized in context.initial or set by a prior action"
                )

    def _is_defined(self, var: str, defined: set[str]) -> bool:
        """Check if variable is in defined set."""
        if var in defined:
            return True

        # Check parent paths
        parts = var.split(".")
        for i in range(len(parts) - 1, 0, -1):
            parent = ".".join(parts[:i])
            if parent in defined:
                return True

        return False

    # -------------------------------------------------------------------------
    # CHECK 3: Graph Topology
    # -------------------------------------------------------------------------

    def _check_graph_topology(self, recipe: Dict, result: ValidationResult):
        """Check for valid DAG structure and orphan nodes."""
        nodes = {n["id"]: n for n in recipe.get("nodes", [])}
        edges = recipe.get("edges", [])
        entry_point = recipe.get("entry_point", "")
        exit_points = set(recipe.get("exit_points", {}).values())

        # Build adjacency lists
        incoming: dict[str, list[str]] = {nid: [] for nid in nodes}
        outgoing: dict[str, list[str]] = {nid: [] for nid in nodes}

        for edge in edges:
            src = edge.get("source") or edge.get("from", "")
            dst = edge.get("target") or edge.get("to", "")
            if src in outgoing:
                outgoing[src].append(dst)
            if dst in incoming:
                incoming[dst].append(src)

        # Also consider loop body references
        for node_id, node in nodes.items():
            if node.get("type") == "loop":
                body = node.get("loop", {}).get("body")
                if body:
                    incoming[body].append(node_id) if body in incoming else None

        # Check for orphan nodes (no incoming edges, not entry point)
        for node_id in nodes:
            if node_id == entry_point:
                continue
            if node_id in exit_points:
                continue
            if not incoming.get(node_id):
                result.add_warning(
                    code="W002",
                    message=f"Node '{node_id}' has no incoming edges (orphan)",
                    node_id=node_id,
                    suggestion="Add an edge to this node or remove it"
                )

        # Check for cycles (excluding loop_continue edges)
        if self._has_cycle(nodes.keys(), edges):
            result.add_error(
                code="E002",
                message="Recipe contains circular dependencies (non-loop cycle)"
            )

    def _has_cycle(self, node_ids, edges) -> bool:
        """Detect cycles using DFS."""
        graph = {nid: [] for nid in node_ids}

        for edge in edges:
            if edge.get("type") == "loop_continue":
                continue  # Allowed
            src = edge.get("from", "")
            dst = edge.get("to", "")
            if src in graph:
                graph[src].append(dst)

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in graph}

        def dfs(node):
            color[node] = GRAY
            for neighbor in graph.get(node, []):
                if color.get(neighbor) == GRAY:
                    return True
                if color.get(neighbor) == WHITE and dfs(neighbor):
                    return True
            color[node] = BLACK
            return False

        return any(dfs(n) for n in graph if color[n] == WHITE)

    # -------------------------------------------------------------------------
    # CHECK 4: Loop Safety
    # -------------------------------------------------------------------------

    def _check_loop_safety(self, recipe: Dict, result: ValidationResult):
        """Ensure all loops have max_iterations (prevent infinite loops)."""
        MAX_ITERATIONS = 10000

        for node in recipe.get("nodes", []):
            if node.get("type") != "loop":
                continue

            loop_config = node.get("loop", {})
            max_iter = loop_config.get("max_iterations")

            if max_iter is None:
                result.add_error(
                    code="E003",
                    message=f"Loop '{node['id']}' missing max_iterations (infinite loop risk)",
                    node_id=node["id"],
                    suggestion="Add 'loop.max_iterations: 1000'"
                )
            elif max_iter > MAX_ITERATIONS:
                result.add_warning(
                    code="W003",
                    message=f"Loop '{node['id']}' max_iterations ({max_iter}) exceeds limit",
                    node_id=node["id"]
                )

            # Check for loop body reference
            if not loop_config.get("body"):
                result.add_error(
                    code="E004",
                    message=f"Loop '{node['id']}' missing 'body' node reference",
                    node_id=node["id"]
                )

    # -------------------------------------------------------------------------
    # CHECK 5: Reachability
    # -------------------------------------------------------------------------

    def _check_reachability(self, recipe: Dict, result: ValidationResult):
        """Check that all nodes are reachable from entry_point."""
        nodes = {n["id"] for n in recipe.get("nodes", [])}
        edges = recipe.get("edges", [])
        entry_point = recipe.get("entry_point", "")

        if not entry_point:
            result.add_error(code="E005", message="Missing entry_point")
            return

        if entry_point not in nodes:
            result.add_error(
                code="E006",
                message=f"Entry point '{entry_point}' not found in nodes"
            )
            return

        # BFS from entry point
        reachable = set()
        queue = [entry_point]

        # Build adjacency
        adj = {nid: [] for nid in nodes}
        for edge in edges:
            src = edge.get("source") or edge.get("from", "")
            dst = edge.get("target") or edge.get("to", "")
            if src in adj:
                adj[src].append(dst)

        # Add loop body refs
        for node in recipe.get("nodes", []):
            if node.get("type") == "loop":
                body = node.get("loop", {}).get("body")
                if body:
                    adj[node["id"]].append(body)

        while queue:
            current = queue.pop(0)
            if current in reachable:
                continue
            reachable.add(current)
            queue.extend(adj.get(current, []))

        unreachable = nodes - reachable
        for node_id in unreachable:
            result.add_warning(
                code="W004",
                message=f"Node '{node_id}' is unreachable from entry_point",
                node_id=node_id
            )

    # -------------------------------------------------------------------------
    # CHECK 6: Timeout Coverage
    # -------------------------------------------------------------------------

    def _check_timeout_coverage(self, recipe: Dict, result: ValidationResult):
        """Ensure all nodes have timeout_ms defined."""
        for node in recipe.get("nodes", []):
            execution = node.get("execution", {})
            if not execution.get("timeout_ms"):
                result.add_warning(
                    code="W005",
                    message=f"Node '{node['id']}' missing timeout_ms",
                    node_id=node["id"],
                    suggestion="Add 'execution.timeout_ms: 30000'"
                )


# =============================================================================
# CONVENIENCE FUNCTION
# =============================================================================

def validate_recipe_static(recipe: Dict) -> ValidationResult:
    """
    Validate a recipe statically.

    Args:
        recipe: Recipe JSON

    Returns:
        ValidationResult

    Raises:
        RecipeValidationError: If blocking errors
    """
    validator = StaticValidator()
    return validator.validate(recipe)
