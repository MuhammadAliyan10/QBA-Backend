# core/planning/sightedPlanner.py
"""
Sighted Planner v3.0 — JIT Epoch Strategist with Multi-Tab Orchestration.

Implements Semantic Late Binding: the LLM outputs human-readable intents
(e.g. "the checkout button"), never hardcoded Node IDs. The GoalExecutor
resolves intents to live DOM elements at execution time via SmartFinder.

Sparse Context & Active Tab Law: only the active tab's axTree/semantic map
is sent to the LLM. Background tabs are metadata-only summaries.
"""

import json
import logging
import os
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger("sightedPlanner")


# =============================================================================
# PYDANTIC SCHEMAS — LLM OUTPUT CONTRACT
# =============================================================================

class ActionEnum(str, Enum):
    """Exhaustive set of tactical actions the LLM may emit."""
    CLICK = "click"
    TYPE = "type"
    SELECT_OPTION = "select_option"
    EXTRACT_TEXT = "extract_text"
    EXTRACT_LIST = "extract_list"
    EXTRACT_TABLE = "extract_table"
    NAVIGATE = "navigate"
    SCROLL = "scroll"
    WAIT = "wait"
    HOVER = "hover"
    CHECK = "check"
    SWITCH_TAB = "switch_tab"
    NEW_TAB = "new_tab"
    CLOSE_TAB = "close_tab"
    PRESS_KEY = "press_key"


# Fuzzy normalization map: LLM synonym → canonical ActionEnum value
_ACTION_ALIAS: Dict[str, ActionEnum] = {
    "type_text": ActionEnum.TYPE,
    "input": ActionEnum.TYPE,
    "fill": ActionEnum.TYPE,
    "press": ActionEnum.CLICK,
    "tap": ActionEnum.CLICK,
    "select": ActionEnum.SELECT_OPTION,
    "choose": ActionEnum.SELECT_OPTION,
    "extract": ActionEnum.EXTRACT_TEXT,
    "read": ActionEnum.EXTRACT_TEXT,
    "goto": ActionEnum.NAVIGATE,
    "open": ActionEnum.NAVIGATE,
    "switch": ActionEnum.SWITCH_TAB,
    "switch_page": ActionEnum.SWITCH_TAB,
    "new_tab": ActionEnum.NEW_TAB,
    "close": ActionEnum.CLOSE_TAB,
    "close_page": ActionEnum.CLOSE_TAB,
    "key": ActionEnum.PRESS_KEY,
}


class GoalAction(BaseModel):
    """Single tactical intent emitted by the LLM planner."""
    goal_id: str = Field(description="Unique ID for this intent within the epoch, e.g. 'g1'.")
    action: ActionEnum = Field(description="The action to perform.")
    intent: str = Field(
        default="",
        description="Semantic description of the target element (e.g. 'the search button'). "
                    "MUST NOT contain DOM Node IDs.",
    )
    value: str = Field(
        default="",
        description="Payload for the action: text to type, URL to navigate, key to press.",
    )
    target_tab_index: int = Field(
        default=-1,
        description="Tab index for SWITCH_TAB. -1 means active tab.",
    )
    store_as: Optional[str] = Field(
        default=None,
        description="Variable name to store extracted data under.",
    )

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: Any) -> ActionEnum:
        if isinstance(v, ActionEnum):
            return v
        raw = str(v).lower().strip().replace(" ", "_")
        if raw in ActionEnum.__members__:
            return ActionEnum(raw)
        alias = _ACTION_ALIAS.get(raw)
        if alias is not None:
            return alias
        return ActionEnum.CLICK

    @field_validator("value", mode="before")
    @classmethod
    def coerce_value_to_str(cls, v: Any) -> str:
        """LLM sometimes emits integers (e.g. tab index) in the value field."""
        if v is None:
            return ""
        return str(v)


class EpochPlan(BaseModel):
    """LLM-generated plan for one Epoch (current browser state)."""
    model_config = ConfigDict(extra="allow")

    feasible: bool = Field(default=True, description="Whether the objective is achievable from this state.")
    rejection_reason: str = Field(default="", description="If not feasible, why.")
    strategic_objective: str = Field(default="", description="High-level summary of this epoch's goals.")
    intents: List[GoalAction] = Field(default_factory=list, description="Ordered list of tactical intents.")
    expected_outcome: str = Field(default="", description="Predicate for transition verification.")
    is_final_step: bool = Field(default=False, description="True when the global objective is fully achieved.")
    planning_duration_ms: int = Field(default=0, exclude=True)
    model_used: str = Field(default="", exclude=True)
    raw_response: str = Field(default="", exclude=True)


# =============================================================================
# BACKWARD-COMPAT ALIASES (consumed by __init__.py and goalExecutor)
# =============================================================================

SightedGoal = GoalAction
SightedEpoch = EpochPlan


# =============================================================================
# JIT EPOCH SYSTEM PROMPT
# =============================================================================

SYSTEM_PROMPT = """You are a Browser Automation Strategist. You receive a JSON Semantic Map of the ACTIVE tab and a list of background tabs (metadata only).

YOUR MANDATE:
1. Plan ONLY the immediate next Epoch: 3-5 actions for the current page state.
2. Use SEMANTIC INTENTS (e.g. "the destination search input"), NEVER DOM Node IDs or CSS selectors.
3. If you need data from a background tab, emit action: "switch_tab" with target_tab_index first.
4. Define "expected_outcome" so the executor knows when the epoch's transition is complete.
5. Set "is_final_step": true ONLY when the global objective is fully satisfied.
6. If the objective is impossible from this state, set "feasible": false with a reason.

CRITICAL — PREREQUISITE SEQUENCING:
- Analyze the DOM map carefully. If elements needed for later steps DO NOT EXIST YET, you MUST create them first.
- Example: If the objective says "add 3 tasks then mark one complete", and the todo list is EMPTY, you MUST emit "type" actions to ADD the tasks BEFORE trying to click/check any todo item.
- NEVER emit a click/check action targeting an element that does not exist in the current DOM map.

CRITICAL — FAILURE ADAPTATION:
- If the EXECUTION HISTORY contains "[DESYNC]" entries, a previous plan FAILED because an element was not found.
- You MUST NOT repeat the same plan that caused the DESYNC. Analyze WHY it failed and emit a DIFFERENT strategy.
- Common cause: trying to interact with elements that don't exist yet. Solution: emit actions to CREATE those elements first.

VALID ACTIONS: click, type, select_option, extract_text, extract_list, extract_table, navigate, scroll, wait, hover, check, switch_tab, new_tab, close_tab, press_key.

IMPORTANT NOTES ON ACTIONS:
- "type": Types text into an input field. Requires "intent" (which input to target) AND "value" (text to type). After typing, if the input requires submission (like a todo input), emit a follow-up "press_key" with value "Enter".
- "check": Toggles a checkbox element (e.g. marking a todo as completed).
- "click": Clicks a button, link, or interactive element.
- "press_key": Presses a keyboard key. Common values: "Enter", "Tab", "Escape".

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "feasible": true,
  "strategic_objective": "Add 3 todo items to the list",
  "intents": [
    {"goal_id": "g1", "action": "type", "intent": "the new todo input field", "value": "Buy milk"},
    {"goal_id": "g2", "action": "press_key", "intent": "", "value": "Enter"},
    {"goal_id": "g3", "action": "type", "intent": "the new todo input field", "value": "Fix pipeline"},
    {"goal_id": "g4", "action": "press_key", "intent": "", "value": "Enter"}
  ],
  "expected_outcome": "The todo list shows the newly added items",
  "is_final_step": false
}"""

USER_PROMPT_TEMPLATE = """## GLOBAL OBJECTIVE
{objective}

## EXECUTION HISTORY
{history}

## ACTIVE TAB (index {active_index})
URL: {active_url}
Title: {active_title}
--- Semantic Map ---
{active_dom}

## BACKGROUND TABS
{background_tabs}

Output the JIT Epoch plan for the active tab. JSON only, no explanation."""


# =============================================================================
# SIGHTED PLANNER
# =============================================================================

class SightedPlanner:
    """
    JIT Epoch Strategist with multi-tab awareness.

    Calls the LLM with the harvested semantic map (active tab only) and
    background tab metadata. Returns a validated EpochPlan whose intents
    contain only semantic descriptions, never DOM Node IDs.
    """

    MAX_LLM_RETRIES = 2
    MAX_DOM_CHARS = 6000

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.model = model or os.getenv("NVIDIA_NIM_MODEL", "meta/llama-3.1-8b-instruct")
        self.api_key = api_key or os.getenv("NVIDIA_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("NVIDIA_NIM_URL", "https://integrate.api.nvidia.com/v1")
        self._client = None

    async def _ensure_client(self) -> None:
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=30.0,
            )

    async def plan_epoch(
        self,
        objective: str,
        history: List[str],
        active_tab: Dict[str, Any],
        background_tabs: List[Dict[str, Any]],
    ) -> EpochPlan:
        """Generate an Epoch plan for the current browser state."""
        start = time.time()
        await self._ensure_client()

        bg_lines = "\n".join(
            f"- [index={t.get('index', '?')}] {t.get('title', '')} ({t.get('url', '')})"
            for t in background_tabs
        ) or "(No other tabs open)"

        dom_text = active_tab.get("dom_map_text", "(Empty DOM)")
        if len(dom_text) > self.MAX_DOM_CHARS:
            dom_text = dom_text[: self.MAX_DOM_CHARS] + "\n... (truncated)"

        user_prompt = USER_PROMPT_TEMPLATE.format(
            objective=objective,
            history="\n".join(history[-5:]) if history else "Start of mission.",
            active_index=active_tab.get("index", 0),
            active_url=active_tab.get("url", ""),
            active_title=active_tab.get("title", ""),
            active_dom=dom_text,
            background_tabs=bg_lines,
        )

        for attempt in range(1, self.MAX_LLM_RETRIES + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=2048,
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content.strip()
                epoch = self._parse_response(raw)
                epoch.planning_duration_ms = int((time.time() - start) * 1000)
                epoch.model_used = self.model
                epoch.raw_response = raw

                # Token telemetry: attach usage for downstream capture
                usage = getattr(response, "usage", None)
                if usage:
                    epoch._telemetry_prompt_tokens = getattr(usage, "prompt_tokens", 0)
                    epoch._telemetry_completion_tokens = getattr(usage, "completion_tokens", 0)
                    epoch._telemetry_total_tokens = getattr(usage, "total_tokens", 0)

                return epoch

            except Exception as exc:
                logger.error(f"[SightedPlanner] Attempt {attempt}/{self.MAX_LLM_RETRIES} failed: {exc}")
                if attempt == self.MAX_LLM_RETRIES:
                    return EpochPlan(feasible=False, rejection_reason=f"Planning error: {exc}")

        return EpochPlan(feasible=False, rejection_reason="Planning exhausted retries.")

    # ------------------------------------------------------------------
    # RESPONSE PARSING
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> EpochPlan:
        """Validate and parse raw LLM JSON into an EpochPlan."""
        raw = self._strip_markdown_fences(raw)
        data = json.loads(raw)
        epoch = EpochPlan(**data)
        return epoch

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1:]
            if text.endswith("```"):
                text = text[:-3]
        return text.strip()



