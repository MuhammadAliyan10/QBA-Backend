import json
import random
from typing import Dict

class LLMClient:
    def __init__(self):
        print("🧠 LLM Client Initialized (MOCK MODE)")

    def find_element(self, html_chunk: str, intent: str) -> Dict[str, float]:
        """
        Simulates an LLM finding an element.
        In a real implementation, this would call OpenAI/Gemini.
        For now, we use simple heuristics to 'fake' intelligence for the test case.
        """
        print(f"🧠 Brain processing intent: '{intent}' on {len(html_chunk)} chars of HTML...")
        
        # Mock Logic - Updated to match actual example.com structure
        if "more information" in intent.lower() or "information" in intent.lower():
            # example.com actually has: <a href="https://www.iana.org/domains/reserved">More information...</a>
            return {"selector": "a[href*='iana.org']", "confidence": 0.95}
        
        if "login" in intent.lower():
             return {"selector": "#login-btn", "confidence": 0.99}

        # Default fallback - just find any link
        return {"selector": "a", "confidence": 0.60}
