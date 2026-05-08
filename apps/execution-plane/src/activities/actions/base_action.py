from abc import ABC, abstractmethod
from typing import Dict, Any
from ..context import ExecutionContext

class BaseAction(ABC):
    @abstractmethod
    async def execute(self, ctx: ExecutionContext, payload: dict) -> dict:
        pass
