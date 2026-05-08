import re
from typing import Any
from pydantic import BaseModel, validator

UI_BLACKLIST = [
    "meta pay", "threads", "messenger", "log out", "log in", 
    "sign up", "forgot password", "home", "search", "notifications",
    "meta quest", "meta horizon", "meta store", "bulletin", "ray-ban meta",
    "email or mobile number", "password", "create a page", "developers",
    "careers", "privacy", "cookies", "ad choices", "terms", "help",
    "ای میل یا موبائل نمبر"
]

class ExtractionValidator(BaseModel):
    intent: str
    value: Any

    @validator("value")
    def validate_value(cls, v, values):
        if not v or not isinstance(v, str):
            return v
            
        lower_val = v.lower().strip()
        
        # UI Blacklist Check
        if lower_val in UI_BLACKLIST:
            raise ValueError(f"Guardrail tripped: Extracted generic UI element '{v}' instead of content.")
            
        # Strict Date/Time Enforcement
        intent = values.get("intent", "")
        intent_lower = intent.lower()
        if "time" in intent_lower or "date" in intent_lower or "when" in intent_lower or "timestamp" in intent_lower:
            time_pattern = r'(\b\d+\s+(h|hr|hrs|m|min|mins|hour|hours|d|day|days)\s+ago\b)|(\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d+\b)|(\b\d{1,2}:\d{2}\s*(am|pm)?\b)|(\byesterday\b)|(\bjust now\b)'
            if not re.search(time_pattern, lower_val):
                raise ValueError(f"Guardrail tripped: Extracted value '{v}' does not match strict date/time format.")
                
        return v
