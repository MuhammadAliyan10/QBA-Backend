"""
Workflows package for Temporal workflow definitions.

This package contains all Temporal workflow classes.
"""

# Re-export workflows for backwards compatibility
from workflows.browser_workflow import BrowserWorkflow
from workflows.generation_workflow import GenerateWorkflowRecipe

__all__ = [
    "BrowserWorkflow",
    "GenerateWorkflowRecipe"
]
