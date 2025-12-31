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

