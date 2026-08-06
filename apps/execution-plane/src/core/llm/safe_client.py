# src/core/llm/safe_client.py

import os
import re
import json
import logging
import httpx
from typing import List, Dict, Any, Optional
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from security.pii_scrubber import sanitize_payload

logger = logging.getLogger("safe_llm_client")

# ---------------------------------------------------------------------------
# Budget constants
# ---------------------------------------------------------------------------
TOKEN_BUDGET_PER_JOB: int = int(os.getenv("LLM_TOKEN_BUDGET", "40000"))


class LLMValidationException(Exception):
    """Raised when LLM output fails structural validation."""


class TokenBudgetExhausted(Exception):
    """Raised when the per-job token budget is consumed before task completion."""


def _estimate_tokens_heuristic(prompt: str, response: str) -> int:
    """Crude but effective token approximation: (len(prompt) + len(response)) // 4."""
    return (len(prompt) + len(response)) // 4


def _extract_token_usage(api_data: dict, prompt_text: str, response_text: str) -> dict:
    """
    Extracts token usage from the API response.
    Falls back to local heuristic if the provider omits the usage object or returns 0.
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
    Robust LLM client with:
      - Per-job token budget enforcement (raises TokenBudgetExhausted)
      - Single-quote → double-quote JSON auto-repair
      - Adaptive retry with exponential backoff (3 attempts max)
      - PII sanitization on all outbound payloads
      - Cumulative token telemetry
    """

    def __init__(self, api_key: str = None, base_url: str = None, use_extraction_model: bool = False):
        self.provider = os.getenv("LLM_PROVIDER", "nvidia").strip().lower()

        # ---- Primary provider config ----------------------------------------
        self._primary_cfg   = self._build_provider_cfg(
            self.provider, api_key, base_url, use_extraction_model
        )
        # ---- Fallback provider config (secondary, used only when primary fails) -
        _fallback_provider  = os.getenv("LLM_FALLBACK_PROVIDER", "").strip().lower()
        self._fallback_cfg  = (
            self._build_provider_cfg(_fallback_provider, None, None, use_extraction_model)
            if _fallback_provider and _fallback_provider != self.provider
            else None
        )

        # Expose primary values for backward-compat (model name used in call() timeout)
        self.api_key  = self._primary_cfg["api_key"]
        self.base_url = self._primary_cfg["base_url"]
        self.model    = self._primary_cfg["model"]

        if not self.api_key:
            logger.warning(
                f"No API key configured for SafeLLMClient (provider: {self.provider})"
            )
        if self._fallback_cfg:
            logger.info(
                f"[SafeLLMClient] Fallback provider configured: {_fallback_provider} "
                f"(model: {self._fallback_cfg['model']})"
            )

        # Per-instance cumulative counters — shared across all calls on same client
        self.total_tokens_used        = 0
        self.total_prompt_tokens      = 0
        self.total_completion_tokens  = 0
        self.token_budget             = TOKEN_BUDGET_PER_JOB

    @staticmethod
    def _build_provider_cfg(
        provider: str,
        api_key: str | None,
        base_url: str | None,
        use_extraction_model: bool,
    ) -> dict:
        """Return a dict with api_key, base_url, model for the given provider name."""
        if provider == "gemini":
            return {
                "provider": "gemini",
                "api_key":  api_key  or os.getenv("GOOGLE_API_KEY", ""),
                "base_url": base_url or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "model":    os.getenv("LLM_MODEL", "gemini-2.0-flash"),
            }
        else:
            model_env = "LLM_EXTRACTION_MODEL" if use_extraction_model else "LLM_MODEL"
            return {
                "provider": "nvidia",
                "api_key":  api_key  or os.getenv("NVIDIA_API_KEY", ""),
                "base_url": base_url or "https://integrate.api.nvidia.com/v1/chat/completions",
                "model":    os.getenv(model_env, "meta/llama-3.1-8b-instruct"),
            }

    # -----------------------------------------------------------------------
    # INTERNAL: JSON Repair
    # -----------------------------------------------------------------------
    def _clean_json(self, text: str) -> str:
        """
        Multi-stage JSON repair pipeline:
        1. Strip markdown fences (```json ... ```)
        2. Normalize Python single-quote style → valid JSON double-quote style
        3. Extract outermost JSON object or array boundary
        """
        text = text.strip()

        # Stage 1: strip markdown fences
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        # Stage 2: single → double quote normalization
        # Targets only key/value strings in JSON-like structures.
        # Strategy: replace 'word' patterns that are not inside already-valid double-quoted strings.
        # This handles: {"type": 'click'} → {"type": "click"}
        def _replace_single_quoted_values(m: re.Match) -> str:
            inner = m.group(1)
            # Escape any existing double quotes inside the inner string
            inner = inner.replace('"', '\\"')
            return f'"{inner}"'

        # Match single-quoted strings that are JSON values/keys (not possessives)
        text = re.sub(r"(?<![\\])'([^'\\]*(?:\\.[^'\\]*)*)'", _replace_single_quoted_values, text)

        # Stage 3: extract outermost JSON boundary
        obj_start = text.find("{")
        obj_end   = text.rfind("}")
        arr_start = text.find("[")
        arr_end   = text.rfind("]")

        candidates: list[tuple[int, int]] = []
        if obj_start != -1 and obj_end != -1 and obj_end >= obj_start:
            candidates.append((obj_start, obj_end))
        if arr_start != -1 and arr_end != -1 and arr_end >= arr_start:
            candidates.append((arr_start, arr_end))

        if candidates:
            best = min(candidates, key=lambda c: c[0])
            extracted = text[best[0] : best[1] + 1]

            # Truncation repair: if LLM hit max_tokens, the JSON may be cut off.
            # Count open braces/brackets and close any that are unclosed.
            try:
                json.loads(extracted)
                return extracted  # Already valid, fast path
            except json.JSONDecodeError:
                # Attempt to close unclosed structures
                stack = []
                PAIRS = {'{': '}', '[': ']'}
                CLOSE = {'}', ']'}
                for ch in extracted:
                    if ch in PAIRS:
                        stack.append(PAIRS[ch])
                    elif ch in CLOSE:
                        if stack and stack[-1] == ch:
                            stack.pop()
                # Append missing closers in reverse order
                repaired = extracted + ''.join(reversed(stack))
                logger.debug(f"[_clean_json] Attempted truncation repair: appended {len(stack)} closer(s)")
                return repaired

        return text

    # -----------------------------------------------------------------------
    # INTERNAL: Token budget check
    # -----------------------------------------------------------------------
    def _check_budget(self, tokens_used: int) -> None:
        """Raises TokenBudgetExhausted if the cumulative spend exceeds the budget."""
        if self.total_tokens_used >= self.token_budget:
            raise TokenBudgetExhausted(
                f"Token budget exhausted: {self.total_tokens_used}/{self.token_budget} tokens used. "
                f"Stopping to prevent runaway cost."
            )

    # -----------------------------------------------------------------------
    # INTERNAL: HTTP post against a specific provider config
    # -----------------------------------------------------------------------
    async def _post_to(
        self,
        cfg: dict,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> str:
        """
        Low-level HTTP call to a specific provider config dict.
        Tracks token telemetry against the instance counters.
        Raises httpx.RequestError on non-200 / rate-limit.
        """
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                cfg["base_url"],
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {cfg['api_key']}",
                },
                json={
                    "model": cfg["model"],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                },
            )

        if response.status_code == 429:
            raise httpx.RequestError(f"Rate limited (429) on {cfg['provider']}. Backing off.")
        if response.status_code != 200:
            raise httpx.RequestError(
                f"API Error {response.status_code} on {cfg['provider']}: {response.text[:200]}"
            )

        data    = response.json()
        content = data["choices"][0]["message"]["content"]

        # Token telemetry
        prompt_text = f"{system_prompt}\n{user_prompt}"
        token_info  = _extract_token_usage(data, prompt_text, content)
        self.total_tokens_used       += token_info["total_tokens"]
        self.total_prompt_tokens     += token_info["prompt_tokens"]
        self.total_completion_tokens += token_info["completion_tokens"]

        remaining = max(0, self.token_budget - self.total_tokens_used)
        logger.info(
            f"[Tokens] provider={cfg['provider']} call={token_info['total_tokens']} "
            f"({'heuristic' if token_info['is_heuristic'] else 'api'}) | "
            f"cumulative={self.total_tokens_used} | budget_remaining={remaining}"
        )
        return content

    # -----------------------------------------------------------------------
    # INTERNAL: Shared HTTP post + telemetry (with provider fallback)
    # -----------------------------------------------------------------------
    async def _post(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        timeout_override: float = None,
    ) -> str:
        """
        Tries the primary provider. If all retries on the primary fail and a
        fallback provider is configured, makes a single attempt on the fallback.
        Raises the last exception if both providers fail.
        """
        system_prompt = sanitize_payload(system_prompt)
        user_prompt   = sanitize_payload(user_prompt)

        # Pre-flight: check budget before spending more tokens
        self._check_budget(self.total_tokens_used)

        _timeout = timeout_override or float(os.getenv("TIMEOUT_LLM_SEC", "40"))

        # --- Primary provider attempt ---
        primary_error: Exception | None = None
        try:
            return await self._post_to(
                self._primary_cfg, system_prompt, user_prompt, temperature, max_tokens, _timeout
            )
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            primary_error = exc
            logger.warning(
                f"[SafeLLMClient] Primary provider '{self._primary_cfg['provider']}' failed: {exc}. "
                f"{'Attempting fallback.' if self._fallback_cfg else 'No fallback configured.'}"
            )

        # --- Fallback provider attempt (single attempt, no extra retry) ---
        if self._fallback_cfg:
            if not self._fallback_cfg.get("api_key"):
                logger.error(
                    f"[SafeLLMClient] Fallback provider '{self._fallback_cfg['provider']}' "
                    "has no API key — skipping fallback."
                )
            else:
                try:
                    content = await self._post_to(
                        self._fallback_cfg,
                        system_prompt, user_prompt, temperature, max_tokens, _timeout
                    )
                    logger.info(
                        f"[SafeLLMClient] Fallback to '{self._fallback_cfg['provider']}' succeeded."
                    )
                    return content
                except Exception as fb_exc:
                    logger.error(
                        f"[SafeLLMClient] Fallback provider '{self._fallback_cfg['provider']}' "
                        f"also failed: {fb_exc}"
                    )

        # Both providers failed — re-raise the primary error for tenacity retry
        raise primary_error

    # -----------------------------------------------------------------------
    # PUBLIC: Raw string call (used by Universal Agent Phase 1 & 2)
    # -----------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError,)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def call(self, system_prompt: str, user_prompt: str, temperature: float = 0.1) -> str:
        """
        Safely call LLM and return raw string.
        Does NOT retry on TokenBudgetExhausted — that must propagate immediately.
        """
        if not self.api_key:
            raise ValueError("API key missing")
        result = await self._post(system_prompt, user_prompt, temperature, max_tokens=800,
                                  timeout_override=90.0 if self.model == os.getenv("LLM_EXTRACTION_MODEL", "meta/llama-3.1-8b-instruct") else None)
        # Log truncation warning if response doesn't look complete
        stripped = result.strip()
        if stripped and stripped[-1] not in ('}', ']', '"'):
            logger.warning(f"[SafeLLMClient] Response may be truncated (ends with: ...{stripped[-30:]!r})")
        return result

    # -----------------------------------------------------------------------
    # PUBLIC: Structured JSON call for step planning
    # -----------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, LLMValidationException, json.JSONDecodeError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def safe_plan_recipe(self, prompt: str, system_prompt: str) -> list[dict[str, Any]]:
        """Generate a workflow recipe from a prompt. Returns a validated list of step dicts."""
        if not self.api_key:
            raise ValueError("API key missing")

        content = await self._post(system_prompt, prompt, temperature=0.1, max_tokens=1024)
        content = self._clean_json(content)

        try:
            steps = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON Parse Error in safe_plan_recipe: {e}")
            raise

        if not isinstance(steps, list):
            raise LLMValidationException("Output is not a valid list of steps")
        if len(steps) == 0:
            raise LLMValidationException("Output list is empty")
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise LLMValidationException(f"Step {i} is not a dictionary")
            if "action" not in step:
                raise LLMValidationException(f"Step {i} missing 'action' field")

        return steps

    # -----------------------------------------------------------------------
    # PUBLIC: Structured JSON call for step evaluation
    # -----------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, LLMValidationException, json.JSONDecodeError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def safe_evaluate_step(self, prompt: str, system_prompt: str) -> dict[str, Any]:
        """Safely evaluate JIT next step, returning a validated single dict."""
        if not self.api_key:
            raise ValueError("API key missing")

        content = await self._post(system_prompt, prompt, temperature=0.1, max_tokens=512)
        content = self._clean_json(content)

        try:
            step = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON Parse Error in safe_evaluate_step: {e}")
            raise

        if not isinstance(step, dict):
            raise LLMValidationException("Output is not a valid dictionary")

        return step
