import os
import json
import logging
import httpx
from typing import List, Dict, Any, Optional
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)

from security.pii_scrubber import sanitize_payload

logger = logging.getLogger("safe_llm_client")

class LLMValidationException(Exception):
    """Raised when LLM output fails validation."""
    pass


def _estimate_tokens_heuristic(prompt: str, response: str) -> int:
    """Crude but effective token approximation: (len(prompt) + len(response)) // 4."""
    return (len(prompt) + len(response)) // 4


def _extract_token_usage(api_data: dict, prompt_text: str, response_text: str) -> dict:
    """
    Extracts token usage from the API response. Falls back to local
    heuristic if the provider omits the usage object or returns 0.
    """
    usage = api_data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0
    total_tokens = usage.get("total_tokens", 0) or 0

    is_heuristic = False
    if total_tokens == 0:
        total_tokens = _estimate_tokens_heuristic(prompt_text, response_text)
        prompt_tokens = len(prompt_text) // 4
        completion_tokens = len(response_text) // 4
        is_heuristic = True
        logger.warning(
            f"Nvidia API omitted usage stats. "
            f"Using local heuristic approximation: {total_tokens} tokens"
        )

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "is_heuristic": is_heuristic,
    }

class SafeLLMClient:
    """
    Robust LLM Client with retries, validation, and fail-safety.
    Wraps standard API calls with Tenacity for exponential backoff.

    FIX RC7: Now respects LLM_PROVIDER environment variable.
    Supported providers: "gemini" (Google), "nvidia" (default).

    FIX RC8: Tracks cumulative token usage with local heuristic fallback
    when Nvidia NIM omits the usage object from API responses.
    """

    def __init__(self, api_key: str = None, base_url: str = None):
        # FIX RC7: Read provider from env and branch accordingly.
        # Previously hardcoded to NVIDIA regardless of LLM_PROVIDER setting.
        self.provider = os.getenv("LLM_PROVIDER", "nvidia").strip().lower()

        if self.provider == "gemini":
            self.api_key  = api_key  or os.getenv("GOOGLE_API_KEY")
            self.base_url = base_url or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            self.model    = os.getenv("LLM_MODEL", "gemini-2.0-flash")
        else:
            # Default: NVIDIA
            self.provider = "nvidia"
            self.api_key  = api_key  or os.getenv("NVIDIA_API_KEY")
            self.base_url = base_url or "https://integrate.api.nvidia.com/v1/chat/completions"
            self.model    = os.getenv("LLM_MODEL", "meta/llama-3.3-70b-instruct")

        if not self.api_key:
            logger.warning(
                f"No API key configured for SafeLLMClient (provider: {self.provider})"
            )

        # Cumulative token counter for financial telemetry
        self.total_tokens_used = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, LLMValidationException, json.JSONDecodeError)),
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    async def safe_plan_recipe(self, prompt: str, system_prompt: str) -> list[dict[str, Any]]:
        """
        Safely generate a workflow recipe from a prompt.

        Args:
            prompt: User request
            system_prompt: Context and instructions

        Returns:
            List of steps (dict)

        Raises:
            RetryError: If all retries fail
        """
        if not self.api_key:
             raise ValueError("API key missing")

        # PII Shield: Sanitize payloads before network transmission
        system_prompt = sanitize_payload(system_prompt)
        prompt = sanitize_payload(prompt)

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.base_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}"
                },
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ]
                }
            )

            if response.status_code != 200:
                raise httpx.RequestError(f"API Error {response.status_code}: {response.text}")

            data = response.json()
            content = data["choices"][0]["message"]["content"]

            # TOKEN TELEMETRY: Extract or estimate
            prompt_text = f"{system_prompt}\n{prompt}"
            token_info = _extract_token_usage(data, prompt_text, content)
            self.total_tokens_used += token_info["total_tokens"]
            self.total_prompt_tokens += token_info["prompt_tokens"]
            self.total_completion_tokens += token_info["completion_tokens"]
            logger.info(
                f"[Tokens] safe_plan_recipe: {token_info['total_tokens']} "
                f"({'heuristic' if token_info['is_heuristic'] else 'api'}) | "
                f"cumulative: {self.total_tokens_used}"
            )

            # PARSING & CLEANING
            content = self._clean_json(content)

            # VALIDATION
            try:
                steps = json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning(f"JSON Parse Error: {e}")
                # Re-raising triggers retry
                raise

            if not isinstance(steps, list):
                raise LLMValidationException("Output is not a valid list of steps")

            if len(steps) == 0:
                 raise LLMValidationException("Output list is empty")

            # Validate step structure
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                     raise LLMValidationException(f"Step {i} is not a dictionary")
                if "action" not in step:
                     raise LLMValidationException(f"Step {i} missing 'action' field")

            return steps

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, LLMValidationException, json.JSONDecodeError)),
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    async def safe_evaluate_step(self, prompt: str, system_prompt: str) -> dict[str, Any]:
        """Safely evaluate JIT next step, returning a single dict."""
        if not self.api_key:
             raise ValueError("API key missing")

        # PII Shield: Sanitize payloads before network transmission
        system_prompt = sanitize_payload(system_prompt)
        prompt = sanitize_payload(prompt)

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.base_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}"
                },
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ]
                }
            )

            if response.status_code != 200:
                raise httpx.RequestError(f"API Error {response.status_code}: {response.text}")

            data = response.json()
            content = data["choices"][0]["message"]["content"]

            # TOKEN TELEMETRY: Extract or estimate
            prompt_text = f"{system_prompt}\n{prompt}"
            token_info = _extract_token_usage(data, prompt_text, content)
            self.total_tokens_used += token_info["total_tokens"]
            self.total_prompt_tokens += token_info["prompt_tokens"]
            self.total_completion_tokens += token_info["completion_tokens"]
            logger.info(
                f"[Tokens] safe_evaluate_step: {token_info['total_tokens']} "
                f"({'heuristic' if token_info['is_heuristic'] else 'api'}) | "
                f"cumulative: {self.total_tokens_used}"
            )

            content = self._clean_json(content)

            try:
                step = json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning(f"JSON Parse Error: {e}")
                raise

            if not isinstance(step, dict):
                raise LLMValidationException("Output is not a valid dictionary")

            return step

    def _clean_json(self, text: str) -> str:
        """Extract JSON from potential markdown code blocks or preambles."""
        text = text.strip()

        # Find the first '{' and the last '}'
        start_idx = text.find("{")
        end_idx = text.rfind("}")

        if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
            return text[start_idx:end_idx+1]

        return text

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError,)),
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    async def call(self, system_prompt: str, user_prompt: str, temperature: float = 0.1) -> str:
        """Safely call LLM and return raw string."""
        if not self.api_key:
             raise ValueError("API key missing")

        # PII Shield: Sanitize payloads before network transmission
        system_prompt = sanitize_payload(system_prompt)
        user_prompt = sanitize_payload(user_prompt)

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.base_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}"
                },
                json={
                    "model": self.model,
                    "temperature": temperature,
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                }
            )

            if response.status_code != 200:
                raise httpx.RequestError(f"API Error {response.status_code}: {response.text}")

            data = response.json()
            content = data["choices"][0]["message"]["content"]

            # TOKEN TELEMETRY: Extract or estimate
            prompt_text = f"{system_prompt}\n{user_prompt}"
            token_info = _extract_token_usage(data, prompt_text, content)
            self.total_tokens_used += token_info["total_tokens"]
            self.total_prompt_tokens += token_info["prompt_tokens"]
            self.total_completion_tokens += token_info["completion_tokens"]
            logger.info(
                f"[Tokens] call: {token_info['total_tokens']} "
                f"({'heuristic' if token_info['is_heuristic'] else 'api'}) | "
                f"cumulative: {self.total_tokens_used}"
            )

            return content

