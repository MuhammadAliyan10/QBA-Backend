from .registry import ActionRegistry
from .click_action import ClickAction
from .extract_action import ExtractAction
from .login_action import LoginAndSniffAction

registry = ActionRegistry()
registry.register("CLICK", ClickAction())
registry.register("EXTRACT", ExtractAction())
registry.register("LOGIN_AND_SNIFF", LoginAndSniffAction())

__all__ = [
    "ActionRegistry",
    "ClickAction",
    "ExtractAction",
    "LoginAndSniffAction",
    "registry",
]

