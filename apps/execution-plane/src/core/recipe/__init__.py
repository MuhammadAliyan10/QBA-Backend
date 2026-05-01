"""
recipe/ - Recipe Engine Module

This module contains all components for the Universal Recipe Schema v2.0:
- recipeEngine.py: DAG executor (RecipeEngine, StateManager, StepGuard, Node Processors)
- recipeSchema.py: Pydantic models for type-safe recipe validation
- recipeValidator.py: 15-rule validator for recipe integrity
- recipeManager.py: Recipe storage/retrieval with Qdrant vector search
"""

from core.recipe.recipe_engine import (
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

from core.recipe.recipe_validator import RecipeValidator

from core.recipe.recipe_schema import (
    Recipe as RecipeModel,
    Node as NodeModel,
    Edge as EdgeModel,
    Action as ActionModel,
    Condition as ConditionModel,
    RecipeMetadata as MetadataModel,
    ExecutionConfig as ExecutionConfigModel,
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
