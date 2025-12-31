"""
RAG Module - Recipe Memory and Preflight Pipeline

This module provides:
- RAGService: Memory-based template search with pgvector
- URLClassifier: Website categorization for better matching
- StaticValidator: Logic verification without browser
- JustifierEngine: Dynamic browser verification
- RecipePlanner: LLM-powered recipe generation
- PreflightPipeline: Full quality control orchestrator
"""

from core.rag.ragService import RAGService, get_rag_service, TemplateMatch
from core.rag.classifier import URLClassifier, classify_url, ClassificationResult
from core.rag.staticValidator import (
    StaticValidator,
    validate_recipe_static,
    ValidationResult,
    ValidationIssue,
    RecipeValidationError
)
from core.rag.justifier import (
    JustifierEngine,
    justify_recipe,
    JustificationResult,
    VerificationStatus
)
from core.rag.planner import (
    RecipePlanner,
    get_planner,
    PlannerResult
)
from core.rag.preflight import (
    PreflightPipeline,
    PreflightResult,
    handle_preflight_request
)

__all__ = [
    # RAG Service
    "RAGService",
    "get_rag_service",
    "TemplateMatch",

    # Classifier
    "URLClassifier",
    "classify_url",
    "ClassificationResult",

    # Static Validator
    "StaticValidator",
    "validate_recipe_static",
    "ValidationResult",
    "ValidationIssue",
    "RecipeValidationError",

    # Justifier
    "JustifierEngine",
    "justify_recipe",
    "JustificationResult",
    "VerificationStatus",

    # Planner
    "RecipePlanner",
    "get_planner",
    "PlannerResult",

    # Preflight
    "PreflightPipeline",
    "PreflightResult",
    "handle_preflight_request",
]
