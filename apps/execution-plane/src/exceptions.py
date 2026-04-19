"""
Custom exceptions for workflow execution.

These exceptions are used to signal special conditions that require
human intervention or other non-standard handling.
"""

from typing import List, Optional


class HumanInterventionRequired(Exception):
    """
    Raised when workflow needs human input (CAPTCHA, 2FA, price confirmation, etc.).

    This exception triggers workflow hibernation until a Signal is received
    via the /resume endpoint. The workflow will await user input with zero
    CPU cost using Temporal's wait_condition.

    Attributes:
        reason (str): Type of intervention needed (e.g., "CAPTCHA_DETECTED", "2FA_REQUIRED")
        prompt (str): Human-readable question/request to display
        options (list): Available choices for the user
        node_id (str): The node that triggered this intervention
        context (dict): Additional context data (e.g., URL, screenshot, current state)

    Example:
        raise HumanInterventionRequired(
            reason="CAPTCHA_DETECTED",
            prompt="Please solve the CAPTCHA",
            options=["Solved", "Skip"],
            node_id="node_login",
            context={"url": "https://example.com"}
        )
    """

    def __init__(
        self,
        reason: str,
        prompt: str = "",
        options: Optional[List[str]] = None,
        node_id: str = "",
        context: dict = None
    ):
        """
        Initialize the exception.

        Args:
            reason (str): Short code describing why intervention is needed
            prompt (str): Human-readable message to display
            options (list): Available choices
            node_id (str): Node that triggered this
            context (dict, optional): Additional data for debugging/UI display
        """
        self.reason = reason
        self.prompt = prompt
        self.options = options or []
        self.node_id = node_id
        self.context = context or {}
        super().__init__(f"Human intervention required: {reason}")


class RecipeValidationError(Exception):
    """
    Raised when a recipe fails validation.

    Attributes:
        errors (list): List of validation error messages
        recipe_name (str): Name of the invalid recipe
    """

    def __init__(self, errors: List[str], recipe_name: str = "unknown"):
        self.errors = errors
        self.recipe_name = recipe_name
        super().__init__(f"Recipe '{recipe_name}' validation failed with {len(errors)} errors")


class RecipeExecutionError(Exception):
    """
    Raised when recipe execution fails.

    Attributes:
        node_id (str): The node where execution failed
        reason (str): Why it failed
        context (dict): Additional context
    """

    def __init__(self, node_id: str, reason: str, context: dict = None):
        self.node_id = node_id
        self.reason = reason
        self.context = context or {}
        super().__init__(f"Execution failed at node '{node_id}': {reason}")


class CheckpointError(Exception):
    """
    Raised when checkpoint operations fail.

    Attributes:
        checkpoint_id (str): The checkpoint that failed
        operation (str): "save" or "load"
    """

    def __init__(self, checkpoint_id: str, operation: str, reason: str = ""):
        self.checkpoint_id = checkpoint_id
        self.operation = operation
        self.reason = reason
        super().__init__(f"Checkpoint {operation} failed for '{checkpoint_id}': {reason}")


class AIFallbackTriggered(Exception):
    """
    Raised when the deterministic element scoring engine cannot resolve a match.

    Two failure modes:
        1. AbsoluteFailure  — best candidate scored below the confidence floor (0.65).
        2. AmbiguityCollision — top two candidates are within 5% of each other;
           the engine cannot deterministically choose between them.

    Attributes:
        reason (str): "ABSOLUTE_FAILURE" or "AMBIGUITY_COLLISION"
        best_score (float): Score of the highest-ranked candidate
        delta (float): Score gap between #1 and #2 (0.0 if only one candidate)
        top_candidates (list): List of (score, qId, tag, text) tuples for LLM context
    """

    def __init__(
        self,
        reason: str,
        best_score: float = 0.0,
        delta: float = 0.0,
        top_candidates: Optional[list] = None,
    ):
        self.reason = reason
        self.best_score = best_score
        self.delta = delta
        self.top_candidates = top_candidates or []
        super().__init__(
            f"AI fallback triggered ({reason}): "
            f"best_score={best_score:.3f}, delta={delta:.3f}, "
            f"candidates={len(self.top_candidates)}"
        )

    @classmethod
    def absolute_failure(
        cls, best_score: float, top_candidates: list
    ) -> "AIFallbackTriggered":
        """Best candidate scored below the confidence floor."""
        return cls(
            reason="ABSOLUTE_FAILURE",
            best_score=best_score,
            delta=0.0,
            top_candidates=top_candidates,
        )

    @classmethod
    def ambiguity_collision(
        cls, best_score: float, delta: float, top_candidates: list
    ) -> "AIFallbackTriggered":
        """Top two candidates are too close — engine cannot decide."""
        return cls(
            reason="AMBIGUITY_COLLISION",
            best_score=best_score,
            delta=delta,
            top_candidates=top_candidates,
        )
