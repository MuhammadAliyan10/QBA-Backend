"""
Custom exceptions for workflow execution.

These exceptions are used to signal special conditions that require
human intervention or other non-standard handling.
"""


class HumanInterventionRequired(Exception):
    """
    Raised when workflow needs human input (CAPTCHA, 2FA, price confirmation, etc.).

    This exception triggers workflow hibernation until a Signal is received
    via the /resume endpoint. The workflow will await user input with zero
    CPU cost using Temporal's wait_condition.

    Attributes:
        reason (str): Type of intervention needed (e.g., "CAPTCHA_DETECTED", "2FA_REQUIRED")
        context (dict): Additional context data (e.g., URL, screenshot, current state)

    Example:
        raise HumanInterventionRequired(
            reason="CAPTCHA_DETECTED",
            context={"url": "https://example.com", "screenshot_url": "..."}
        )
    """

    def __init__(self, reason: str, context: dict = None):
        """
        Initialize the exception.

        Args:
            reason (str): Short code describing why intervention is needed
            context (dict, optional): Additional data for debugging/UI display
        """
        self.reason = reason
        self.context = context or {}
        super().__init__(f"Human intervention required: {reason}")
