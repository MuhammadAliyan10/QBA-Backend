"""
Checkpoint Manager Package — Multi-Page Plan Stabilization

Exports the CheckpointManager for use in the execution loop.
"""

from .checkpoint_manager import CheckpointManager, CheckpointDivergence

__all__ = ["CheckpointManager", "CheckpointDivergence"]
