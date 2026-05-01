"""
core/planning/__init__.py

Planning module v3.0 — JIT Epoch, Semantic Late Binding, Reflex Arc.
"""
# Legacy exports (preserved for backward compatibility)
from core.planning.intent_parser import IntentParser, Intent
from core.planning.element_matcher import ElementMatcher, MatchResult
from core.planning.node_builder import NodeBuilder

# Sighted architecture v3.0 exports
from core.planning.harvester import harvest_context
from core.planning.sighted_planner import (
    SightedPlanner,
    SightedEpoch,
    SightedGoal,
    EpochPlan,
    GoalAction,
    ActionEnum,
)
from core.planning.goal_executor import (
    GoalExecutor,
    EpochReport,
    GoalResult,
    StateDesyncException,
)
from core.planning.site_atlas import SiteAtlas
from core.planning.sighted_pipeline import SightedPipeline, SightedPipelineResult
