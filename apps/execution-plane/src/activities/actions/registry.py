from typing import Dict
from .base_action import BaseAction

class ActionRegistry:
    def __init__(self) -> None:
        self._actions: Dict[str, BaseAction] = {}

    def register(self, intent: str, action: BaseAction) -> None:
        self._actions[intent.upper()] = action

    def get(self, intent: str) -> BaseAction:
        action = self._actions.get(intent.upper())
        if not action:
            raise KeyError(f"Action '{intent}' is not registered.")
        return action
