from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Any, Optional, Union, List
from datetime import datetime
import logging
import re

logger = logging.getLogger("extractionValidator")

class ExtractionSchema(BaseModel):
    """
    Strict schema for validated data extraction.
    Ensures that hallucinations (e.g. "Meta Quest" as a timestamp) are rejected.
    """
    author_name: Optional[str] = Field(None, description="The exact name of the author.")
    content: Optional[str] = Field(None, description="The text content of the post.")
    timestamp: Optional[str] = Field(None, description="ISO or human-readable timestamp.")

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        
        s = str(v).strip()
        if not s:
            return None

        # Common hallucination patterns for timestamps
        hallucination_keywords = ["meta", "quest", "facebook", "messenger", "threads", "sponsored", "ad"]
        if any(kw in s.lower() for kw in hallucination_keywords):
            logger.warning(f"[Validator] Rejected hallucinated timestamp: '{s}'")
            return None

        # Check for numeric or relative time patterns (e.g. "2h", "Just now", "Yesterday")
        # or ISO formats
        valid_patterns = [
            r"^\d{4}-\d{2}-\d{2}", # ISO Date
            r"^\d+\s*(h|m|s|d|y)",   # Relative (2h)
            r"just now", "yesterday", "today",
            r"[A-Z][a-z]+ \d+, \d{4}", # January 1, 2024
            r"\d+ (hrs|mins|days) ago"
        ]
        
        if any(re.search(p, s, re.IGNORECASE) for p in valid_patterns):
            return s
            
        logger.warning(f"[Validator] Timestamp '{s}' did not match any known patterns.")
        return None

def validate_extraction(data: Any) -> dict:
    """
    Validates a dictionary against the ExtractionSchema.
    Returns the validated (and potentially sanitized) dictionary.
    """
    if not isinstance(data, dict):
        return {}
        
    try:
        schema = ExtractionSchema(**data)
        return schema.model_dump(exclude_none=False)
    except ValidationError as e:
        logger.error(f"[Validator] Schema validation failed: {e}")
        return {}
