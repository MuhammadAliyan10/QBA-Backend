"""
planner.py - LLM-Powered Recipe Generator

Converts user prompts into executable Recipe Schema v2.0 DAGs.
Uses classification and RAG context for better generation.

Supports:
- OpenAI (GPT-4o)
- Google Gemini (gemini-1.5-flash / gemini-1.5-pro)

Author: Quanta Box Paradox Engineering
Version: 2.1.0
"""

import os
import json
import logging
import time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger("planner")


# =============================================================================
# CONFIGURATION
# =============================================================================

# Auto-detect which provider to use (OpenAI takes priority)
def get_llm_provider():
    openai_key = os.getenv("OPENAI_API_KEY", "")
    # Skip invalid/placeholder keys
    if openai_key and not openai_key.startswith("nvapi-") and len(openai_key) > 20:
        return "openai"
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return None

LLM_PROVIDER = get_llm_provider()
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "gemini-2.0-flash-exp" if LLM_PROVIDER == "gemini" else "gpt-4o")
MAX_TOKENS = 4000


# =============================================================================
# RESULT TYPE
# =============================================================================

@dataclass
class PlannerResult:
    """Result of recipe generation."""
    success: bool
    recipe: Optional[Dict] = None
    error: Optional[str] = None
    generation_ms: int = 0
    model_used: str = PLANNER_MODEL
    tokens_used: int = 0


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

SYSTEM_PROMPT = """You are an Automation Architect specializing in browser automation DAGs.

Your task is to generate a Recipe Schema v2.0 JSON that automates the user's request.

## SCHEMA RULES (STRICT):
1. Every recipe MUST have: metadata, nodes[], edges[], entry_point, exit_points
2. Every node MUST have: id, type, execution.timeout_ms, post_conditions[]
3. Every action node MUST have: actions[] with {seq, type, intent}
4. Every loop MUST have: loop.max_iterations (safety brake)
5. Variables use: {{ inputs.name }} or {{ context.name }}

## NODE TYPES:
- action: Browser actions (navigate, find_and_click, find_and_type, extract_text)
- decision: Conditional branching based on context
- loop: Iterate over items with max_iterations
- checkpoint: Save browser state for crash recovery

## ACTION TYPES:
- navigate: Go to URL. Params: {url}
- find_and_click: Click element by intent. Params: {intent}
- find_and_type: Type into input. Params: {intent, value, clear_first, mask_in_logs}
- extract_text: Get text content. Params: {intent, store_in}
- wait_for_selector: Wait for element. Params: {selector, timeout_ms}
- screenshot: Capture viewport. Params: {store_in}

## IMPORTANT:
- Use semantic intents like "login button" not CSS selectors
- For passwords, always set mask_in_logs: true
- After login, set state_policy.checkpoint: true
- Return ONLY valid JSON, no explanations"""


# =============================================================================
# FEW-SHOT EXAMPLE
# =============================================================================

EXAMPLE_RECIPE = """{
  "version": "2.0.0",
  "metadata": {
    "id": "example-login-scrape",
    "name": "Login and Scrape Profile",
    "description": "Authenticate and extract profile data"
  },
  "inputs": {
    "required": [
      {"name": "username", "type": "string"},
      {"name": "password", "type": "string", "encrypted": true}
    ]
  },
  "context": {
    "initial": {"profile_data": null}
  },
  "nodes": [
    {
      "id": "node_navigate",
      "type": "action",
      "name": "Navigate to Login",
      "execution": {"timeout_ms": 30000},
      "actions": [
        {"seq": 1, "type": "navigate", "url": "{{ inputs.url }}"}
      ],
      "post_conditions": [
        {"check": "page_loaded", "on_failure": {"action": "retry"}}
      ]
    },
    {
      "id": "node_login",
      "type": "action",
      "name": "Enter Credentials",
      "execution": {"timeout_ms": 30000},
      "actions": [
        {"seq": 1, "type": "find_and_type", "intent": "email input field", "value": "{{ inputs.username }}", "clear_first": true},
        {"seq": 2, "type": "find_and_type", "intent": "password input field", "value": "{{ inputs.password }}", "mask_in_logs": true},
        {"seq": 3, "type": "find_and_click", "intent": "sign in button"}
      ],
      "post_conditions": [
        {"check": "url_contains", "value": "dashboard", "on_failure": {"action": "fail", "reason": "Login failed"}}
      ],
      "state_policy": {"checkpoint": true}
    },
    {
      "id": "node_extract",
      "type": "action",
      "name": "Extract Profile",
      "execution": {"timeout_ms": 20000},
      "actions": [
        {"seq": 1, "type": "extract_text", "intent": "profile name heading", "store_in": "context.profile_data.name"},
        {"seq": 2, "type": "extract_text", "intent": "email display", "store_in": "context.profile_data.email"}
      ],
      "post_conditions": [
        {"check": "context_value", "path": "context.profile_data.name", "condition": "exists"}
      ]
    },
    {
      "id": "node_success",
      "type": "action",
      "name": "Workflow Complete",
      "execution": {"timeout_ms": 5000},
      "actions": [
        {"seq": 1, "type": "screenshot", "store_in": "context.final_screenshot"}
      ],
      "post_conditions": []
    }
  ],
  "edges": [
    {"from": "node_navigate", "to": "node_login"},
    {"from": "node_login", "to": "node_extract"},
    {"from": "node_extract", "to": "node_success"}
  ],
  "entry_point": "node_navigate",
  "exit_points": {
    "success": "node_success",
    "failure": "node_login",
    "timeout": "node_navigate"
  }
}"""


# =============================================================================
# PLANNER CLASS
# =============================================================================

class RecipePlanner:
    """
    LLM-Powered Recipe Generator.

    Supports:
    - OpenAI (GPT-4o)
    - Google Gemini (gemini-1.5-flash / gemini-1.5-pro)

    Auto-detects which provider to use based on env vars.
    """

    def __init__(self, model: str = None):
        """Initialize planner with auto-detected LLM client."""
        self.model = model or PLANNER_MODEL
        self.provider = LLM_PROVIDER
        self.client = None

        if self.provider == "gemini":
            self._init_gemini()
        elif self.provider == "openai":
            self._init_openai()
        else:
            logger.warning("[Planner] No LLM provider configured")

    def _init_gemini(self):
        """Initialize Google Gemini client."""
        try:
            import google.generativeai as genai

            api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
            genai.configure(api_key=api_key)

            self.client = genai.GenerativeModel(self.model)
            logger.info(f"[Planner] Initialized Gemini: {self.model}")

        except ImportError:
            logger.error("[Planner] google-generativeai not installed. Run: pip install google-generativeai")
            self.client = None
        except Exception as e:
            logger.error(f"[Planner] Gemini init failed: {e}")
            self.client = None

    def _init_openai(self):
        """Initialize OpenAI client."""
        try:
            import openai

            api_key = os.getenv("OPENAI_API_KEY")
            self.client = openai.OpenAI(api_key=api_key)
            logger.info(f"[Planner] Initialized OpenAI: {self.model}")

        except Exception as e:
            logger.error(f"[Planner] OpenAI init failed: {e}")
            self.client = None

    async def generate(
        self,
        prompt: str,
        url: str,
        classification: Optional[Dict] = None,
        similar_template: Optional[Dict] = None
    ) -> PlannerResult:
        """
        Generate a Recipe Schema v2.0 from user prompt.
        """
        if not self.client:
            return PlannerResult(
                success=False,
                error=f"LLM client not initialized (provider: {self.provider})"
            )

        start_time = time.time()

        try:
            # Build the prompt
            user_prompt = self._build_prompt(prompt, url, classification, similar_template)
            full_prompt = f"{SYSTEM_PROMPT}\n\n---\n\nExample of a valid recipe:\n{EXAMPLE_RECIPE}\n\n---\n\n{user_prompt}"

            # Call appropriate provider
            if self.provider == "gemini":
                result = await self._call_gemini(full_prompt)
            else:
                result = await self._call_openai(user_prompt)

            if not result["success"]:
                return PlannerResult(
                    success=False,
                    error=result.get("error", "Unknown error"),
                    generation_ms=int((time.time() - start_time) * 1000)
                )

            # Parse JSON from response
            content = result["content"]

            # Extract JSON if wrapped in markdown
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            recipe = json.loads(content.strip())

            # Inject metadata
            if "metadata" not in recipe:
                recipe["metadata"] = {}
            recipe["metadata"]["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            recipe["metadata"]["generator"] = f"RecipePlanner/{self.provider}"
            recipe["metadata"]["source_prompt"] = prompt[:200]

            generation_ms = int((time.time() - start_time) * 1000)

            logger.info(f"[Planner] Generated recipe in {generation_ms}ms via {self.provider}")

            return PlannerResult(
                success=True,
                recipe=recipe,
                generation_ms=generation_ms,
                model_used=self.model,
                tokens_used=result.get("tokens", 0)
            )

        except json.JSONDecodeError as e:
            logger.error(f"[Planner] Invalid JSON from LLM: {e}")
            return PlannerResult(
                success=False,
                error=f"LLM returned invalid JSON: {str(e)[:100]}",
                generation_ms=int((time.time() - start_time) * 1000)
            )

        except Exception as e:
            logger.error(f"[Planner] Generation failed: {e}")

            # MOCK MODE: Return sample recipe for testing when API unavailable
            if os.getenv("PLANNER_MOCK_MODE", "").lower() == "true":
                logger.warning("[Planner] Using MOCK MODE - returning sample recipe")
                return self._get_mock_recipe(prompt, url)

            return PlannerResult(
                success=False,
                error=str(e)[:200],
                generation_ms=int((time.time() - start_time) * 1000)
            )

    def _get_mock_recipe(self, prompt: str, url: str) -> PlannerResult:
        """Return a mock recipe for testing."""
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.replace("www.", "")

        mock_recipe = {
            "version": "2.0.0",
            "metadata": {
                "id": f"mock-{int(time.time())}",
                "name": f"Mock Recipe for {domain}",
                "description": prompt,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "generator": "RecipePlanner/MOCK"
            },
            "inputs": {
                "required": [
                    {"name": "username", "type": "string"},
                    {"name": "password", "type": "string", "encrypted": True}
                ]
            },
            "context": {"initial": {"profile_data": None}},
            "nodes": [
                {
                    "id": "node_navigate",
                    "type": "action",
                    "name": "Navigate to Login",
                    "execution": {"timeout_ms": 30000},
                    "actions": [
                        {"seq": 1, "type": "navigate", "url": url}
                    ],
                    "post_conditions": [
                        {"check": "page_loaded", "on_failure": {"action": "retry"}}
                    ]
                },
                {
                    "id": "node_login",
                    "type": "action",
                    "name": "Enter Credentials",
                    "execution": {"timeout_ms": 30000},
                    "actions": [
                        {"seq": 1, "type": "find_and_type", "intent": "email input field", "value": "{{ inputs.username }}", "clear_first": True},
                        {"seq": 2, "type": "find_and_type", "intent": "password input field", "value": "{{ inputs.password }}", "mask_in_logs": True},
                        {"seq": 3, "type": "find_and_click", "intent": "sign in button"}
                    ],
                    "post_conditions": [],
                    "state_policy": {"checkpoint": True}
                },
                {
                    "id": "node_extract",
                    "type": "action",
                    "name": "Extract Profile",
                    "execution": {"timeout_ms": 20000},
                    "actions": [
                        {"seq": 1, "type": "extract_text", "intent": "profile name", "store_in": "context.profile_data.name"}
                    ],
                    "post_conditions": []
                }
            ],
            "edges": [
                {"from": "node_navigate", "to": "node_login"},
                {"from": "node_login", "to": "node_extract"}
            ],
            "entry_point": "node_navigate",
            "exit_points": {
                "success": "node_extract",
                "failure": "node_login",
                "timeout": "node_navigate"
            }
        }

        return PlannerResult(
            success=True,
            recipe=mock_recipe,
            generation_ms=0,
            model_used="MOCK",
            tokens_used=0
        )

    async def _call_gemini(self, prompt: str) -> Dict:
        """Call Google Gemini API."""
        try:
            response = self.client.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.2,
                    "max_output_tokens": MAX_TOKENS,
                    "response_mime_type": "application/json"
                }
            )

            return {
                "success": True,
                "content": response.text,
                "tokens": 0  # Gemini doesn't easily expose token count
            }

        except Exception as e:
            logger.error(f"[Planner] Gemini call failed: {e}")
            return {"success": False, "error": str(e)}

    async def _call_openai(self, user_prompt: str) -> Dict:
        """Call OpenAI API."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Example of a valid recipe:\n{EXAMPLE_RECIPE}"},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2,
                max_tokens=MAX_TOKENS,
                response_format={"type": "json_object"}
            )

            return {
                "success": True,
                "content": response.choices[0].message.content,
                "tokens": response.usage.total_tokens if response.usage else 0
            }

        except Exception as e:
            logger.error(f"[Planner] OpenAI call failed: {e}")
            return {"success": False, "error": str(e)}

    def _build_prompt(
        self,
        prompt: str,
        url: str,
        classification: Optional[Dict],
        similar_template: Optional[Dict]
    ) -> str:
        """Build the user prompt with all context."""

        parts = [f"## USER REQUEST\n{prompt}\n"]
        parts.append(f"## TARGET URL\n{url}\n")

        if classification:
            parts.append(f"""## WEBSITE CLASSIFICATION
- Category: {classification.get('category', 'Unknown')}
- Platform: {classification.get('platform', 'Unknown')}
- Complexity: {classification.get('complexity', 'Medium')}
- Auth Required: {classification.get('features', {}).get('auth_required', 'Unknown')}
""")

        if similar_template:
            parts.append(f"""## SIMILAR TEMPLATE (Use as reference)
This template worked for a similar task. Adapt it:
```json
{json.dumps(similar_template, indent=2)[:2000]}
```
""")

        parts.append("""## YOUR TASK
Generate a complete Recipe Schema v2.0 JSON that accomplishes the user's request.
- Include all required fields
- Use semantic intents (not CSS selectors)
- Add proper post_conditions
- Include checkpoint after authentication

Return ONLY the JSON, no explanation.""")

        return "\n".join(parts)


# =============================================================================
# SINGLETON
# =============================================================================

_planner_instance: Optional[RecipePlanner] = None

def get_planner() -> RecipePlanner:
    """Get singleton planner instance."""
    global _planner_instance
    if _planner_instance is None:
        _planner_instance = RecipePlanner()
    return _planner_instance
