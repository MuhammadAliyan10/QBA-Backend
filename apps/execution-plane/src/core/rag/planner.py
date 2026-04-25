"""
planner.py - LLM-Powered Recipe Generator (NIM REST Edition)

Converts user prompts into executable Recipe Schema v2.0 DAGs.
Uses NVIDIA NIM (llama-3.1-8b-instruct) via raw HTTP and strict JSON Schema enforcement.
"""

import os
import json
import logging
import asyncio
import time
from typing import Any, Dict, List, Optional
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, ValidationError, Field

from core.utils.httpclient import GetClient
from core.recipe.recipeSchema import Recipe

logger = logging.getLogger("planner")

# =============================================================================
# V2 PLANNER SCHEMA
# =============================================================================

class StepDirection(BaseModel):
    step_id: str
    intent_type: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    success_criteria: str
    fallback_intents: List[str] = Field(default_factory=list)
    timeout_ms: int = 5000
    max_retries: int = 2

class QuantaPlan(BaseModel):
    plan_version: str = "3.0"
    target_url: str
    goal: str
    subtasks: List[StepDirection]

# =============================================================================
# CONFIGURATION
# =============================================================================

NIM_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODEL = "meta/llama-3.1-8b-instruct"
MAX_RETRIES = 3

class CompilationFailedError(Exception):
    """Raised when the LLM fails to generate a valid recipe after multiple retries."""
    pass

from core.rag.prompts import PLANNER_SYSTEM_PROMPT

from dataclasses import dataclass

@dataclass
class PlannerResult:
    """Result of recipe generation."""
    success: bool
    recipe: Optional[Dict] = None
    error: Optional[str] = None
    generation_ms: int = 0
    model_used: str = "meta/llama-3.1-8b-instruct"
    tokens_used: int = 0

def build_dag_from_directions(plan: QuantaPlan, context: str) -> Recipe:
    from core.recipe.recipeSchema import RecipeMetadata, Node, Edge, ExitPoints, Action, ActionType, NodeType, ExecutionConfig, Condition

    recipe_id = f"rec_{uuid.uuid4().hex[:8]}"
    name = "Auto-generated"
    if plan.goal:
        name = f"Auto-generated: {plan.goal[:40]}"

    metadata = RecipeMetadata(
        id=recipe_id,
        name=name,
        created_at=datetime.now(timezone.utc)
    )

    nodes = []
    edges = []

    previous_node_id = None
    entry_point = None

    for idx, direction in enumerate(plan.subtasks):
        node_id = f"node_{direction.step_id}"
        if idx == 0:
            entry_point = node_id

        action_intent = direction.intent_type.upper()

        action_map = {
            "GO_TO_URL": ActionType.NAVIGATE,
            "NAVIGATE": ActionType.NAVIGATE,
            "SEARCH": ActionType.SEARCH,
            "SET_FILTER": ActionType.SET_FILTER,
            "SET_SORT": ActionType.APPLY_SORT,
            "OPEN_RESULT": ActionType.OPEN_RESULT,
            "OPEN_SECTION": ActionType.CLICK,
            "CLICK_INTENT": ActionType.CLICK,
            "TYPE_TEXT": ActionType.TYPE,
            "SELECT_OPTION": ActionType.SELECT,
            "EXTRACT": ActionType.EXTRACT,
            "VERIFY": ActionType.WAIT,
        }

        # Fallback to generic action if no strict mapping
        mapped_type = action_map.get(action_intent) or ActionType(direction.intent_type) if direction.intent_type in [e.value for e in ActionType] else ActionType.CLICK

        action = Action(
            seq=1,
            type=mapped_type,
            intent=direction.intent_type,
            value=str(direction.arguments.get("value", "")),
            data=direction.arguments
        )

        post_conditions = [
            Condition(
                check="custom_verification",
                value=direction.success_criteria,
                id=f"verify_{direction.step_id}"
            )
        ]

        node = Node(
            id=node_id,
            name=direction.intent_type,
            type=NodeType.ACTION,
            execution=ExecutionConfig(
                timeout_ms=direction.timeout_ms,
                retry={
                    "max_attempts": direction.max_retries,
                    "backoff_ms": 2000,
                    "strategy": "constant"
                }
            ),
            actions=[action],
            post_conditions=post_conditions,
            on_success=f"node_{plan.subtasks[idx+1].step_id}" if idx < len(plan.subtasks)-1 else "node_success",
            on_failure="node_failure",
            on_timeout="node_timeout"
        )
        nodes.append(node)

        if previous_node_id:
            edges.append(Edge(source=previous_node_id, target=node_id))
        previous_node_id = node_id

    # Add default checkpoint nodes
    success_node = Node(id="node_success", name="Exit Success", type=NodeType.CHECKPOINT, execution=ExecutionConfig(timeout_ms=5000))
    failure_node = Node(id="node_failure", name="Exit Failure", type=NodeType.CHECKPOINT, execution=ExecutionConfig(timeout_ms=5000))
    timeout_node = Node(id="node_timeout", name="Exit Timeout", type=NodeType.CHECKPOINT, execution=ExecutionConfig(timeout_ms=5000))
    nodes.extend([success_node, failure_node, timeout_node])

    return Recipe(
        metadata=metadata,
        nodes=nodes,
        edges=edges,
        entry_point=entry_point or "node_success",
        exit_points=ExitPoints(success="node_success", failure="node_failure", timeout="node_timeout")
    )

class RecipePlanner:
    def __init__(self):
        self.api_key = os.getenv("NVIDIA_API_KEY")

    async def generate_with_retry(self, prompt: str, url: str, classification: Optional[Dict] = None, job_id: Optional[str] = None) -> Recipe:
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": f"## TARGET_URL\n{url}\n\n## USER_REQUEST\n{prompt}\n\nNow output STRICT JSON only, no markdown, no commentary."}
        ]

        # Use Pydantic JSON schema
        schema = QuantaPlan.model_json_schema()
        response_format = {"type": "json_schema", "json_schema": {"name": "quantaplan", "schema": schema, "strict": True}}

        attempts = 0
        while attempts < MAX_RETRIES:
            attempts += 1
            try:
                content = await self._call_nim(messages, response_format)
                clean_content = content.strip()
                s, e = clean_content.find('{'), clean_content.rfind('}')
                if s != -1 and e != -1: clean_content = clean_content[s:e+1]

                plan = QuantaPlan.model_validate_json(clean_content)
                recipe = build_dag_from_directions(plan, prompt)

                if job_id:
                    from core.NervousSystem import NervousSystem
                    await NervousSystem.publish(f"quanta.telemetry.{job_id}", json.dumps({"type": "log", "message": "Compiled DAG"}))

                return recipe
            except ValidationError as ve:
                messages.extend([{"role": "assistant", "content": content}, {"role": "user", "content": f"Fix schema errors:\n{ve.json()}"}])
                continue
            except Exception as e:
                if attempts >= MAX_RETRIES: raise CompilationFailedError(f"HTTP/API error: {e}")
                await asyncio.sleep(1)

        raise CompilationFailedError("Retries exhausted.")

    async def generate(self, prompt: str, url: str, **kwargs) -> PlannerResult:
        st = time.time()
        try:
            r = await self.generate_with_retry(prompt, url, job_id=kwargs.get('job_id'))
            return PlannerResult(success=True, recipe=r.model_dump(mode='json'), generation_ms=int((time.time()-st)*1000))
        except Exception as e:
            return PlannerResult(success=False, error=str(e), generation_ms=int((time.time()-st)*1000))

    async def _call_nim(self, messages: list[dict[str, str]], response_format: Dict) -> str:
        r = await GetClient().post(NIM_API_URL, json={"model": NIM_MODEL, "messages": messages, "temperature": 0.1, "response_format": response_format}, headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

_planner_instance = None
def get_planner() -> RecipePlanner:
    global _planner_instance
    if not _planner_instance: _planner_instance = RecipePlanner()
    return _planner_instance
