"""
recipe/ - Recipe Engine Module

This module contains all components for the Universal Recipe Schema v2.0:
- recipeEngine.py: DAG executor (RecipeEngine, StateManager, StepGuard, Node Processors)
- recipeSchema.py: Pydantic models for type-safe recipe validation
- recipeValidator.py: 15-rule validator for recipe integrity
- recipeManager.py: Recipe storage/retrieval with Qdrant vector search
"""

from core.recipe.recipeEngine import (
    RecipeEngine,
    StateManager,
    StepGuard,
    ExecutionContext,
    NodeResult,
    ExecutionStatus,
    # Node Processors
    BaseNodeProcessor,
    ActionNodeProcessor,
    DecisionNodeProcessor,
    LoopNodeProcessor,
    HumanGateNodeProcessor,
    CheckpointNodeProcessor,
    NodeProcessorFactory,
)

from core.recipe.recipeValidator import RecipeValidator

from core.recipe.recipeSchema import (
    RecipeModel,
    NodeModel,
    EdgeModel,
    ActionModel,
    ConditionModel,
    MetadataModel,
    ExecutionConfigModel,
)

__all__ = [
    # Engine
    "RecipeEngine",
    "StateManager",
    "StepGuard",
    "ExecutionContext",
    "NodeResult",
    "ExecutionStatus",
    # Processors
    "BaseNodeProcessor",
    "ActionNodeProcessor",
    "DecisionNodeProcessor",
    "LoopNodeProcessor",
    "HumanGateNodeProcessor",
    "CheckpointNodeProcessor",
    "NodeProcessorFactory",
    # Validator
    "RecipeValidator",
    # Schema
    "RecipeModel",
    "NodeModel",
    "EdgeModel",
    "ActionModel",
    "ConditionModel",
    "MetadataModel",
    "ExecutionConfigModel",
]
