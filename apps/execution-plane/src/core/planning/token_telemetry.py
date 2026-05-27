# core/planning/tokenTelemetry.py
"""
Token Telemetry v1.0 — LLM Usage Tracking & Persistence.

Provides:
  - with_telemetry() decorator that wraps any AsyncOpenAI chat.completions.create
    call, extracts token usage from the response, and persists to DB.
  - TokenLedger singleton for aggregating per-job and per-user usage.
"""

import functools
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("tokenTelemetry")


# =============================================================================
# TOKEN USAGE DATA
# =============================================================================

@dataclass
class TokenSnapshot:
    """Token counts from a single LLM call."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    duration_ms: int = 0


@dataclass
class JobTokenLedger:
    """Accumulator for all LLM calls within a single job/pipeline run."""
    job_id: str = ""
    user_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    model_used: str = ""
    snapshots: list = field(default_factory=list)

    def record(self, snapshot: TokenSnapshot) -> None:
        self.prompt_tokens += snapshot.prompt_tokens
        self.completion_tokens += snapshot.completion_tokens
        self.total_tokens += snapshot.total_tokens
        self.llm_calls += 1
        self.model_used = snapshot.model or self.model_used
        self.snapshots.append(snapshot)

        logger.info(
            f"[TokenTelemetry] [{self.job_id}] Call #{self.llm_calls}: "
            f"+{snapshot.prompt_tokens}p/{snapshot.completion_tokens}c "
            f"= {snapshot.total_tokens}t ({snapshot.model}) "
            f"| Running total: {self.total_tokens}t"
        )


# =============================================================================
# GLOBAL LEDGER REGISTRY — One ledger per active job
# =============================================================================

_ACTIVE_LEDGERS: Dict[str, JobTokenLedger] = {}


def get_ledger(job_id: str, user_id: str = "") -> JobTokenLedger:
    if job_id not in _ACTIVE_LEDGERS:
        _ACTIVE_LEDGERS[job_id] = JobTokenLedger(job_id=job_id, user_id=user_id)
    return _ACTIVE_LEDGERS[job_id]


def close_ledger(job_id: str) -> Optional[JobTokenLedger]:
    return _ACTIVE_LEDGERS.pop(job_id, None)


# =============================================================================
# PERSISTENCE — Write token usage to database
# =============================================================================

async def persist_job_tokens(ledger: JobTokenLedger) -> None:
    """Persist accumulated token usage for a completed job to the DB."""
    try:
        from db import prisma

        if prisma is None or not prisma.is_connected():
            logger.warning("[TokenTelemetry] Prisma not connected, skipping persistence")
            return

        await prisma.execute_raw(
            """
            UPDATE jobs SET
                prompt_tokens = $1,
                completion_tokens = $2,
                total_tokens = $3,
                model_used = $4,
                llm_calls = $5,
                updated_at = NOW()
            WHERE id = $6
            """,
            ledger.prompt_tokens,
            ledger.completion_tokens,
            ledger.total_tokens,
            ledger.model_used,
            ledger.llm_calls,
            ledger.job_id,
        )

        logger.info(
            f"[TokenTelemetry] Persisted job {ledger.job_id}: "
            f"{ledger.total_tokens} tokens across {ledger.llm_calls} calls"
        )

    except Exception as exc:
        logger.error(f"[TokenTelemetry] Failed to persist job tokens: {exc}")


async def persist_user_usage(ledger: JobTokenLedger) -> None:
    """Upsert user-level usage aggregation for the current billing period."""
    if not ledger.user_id:
        return

    try:
        from db import prisma

        if prisma is None or not prisma.is_connected():
            return

        today = date.today()
        period_start = today.replace(day=1)
        if today.month == 12:
            period_end = today.replace(year=today.year + 1, month=1, day=1)
        else:
            period_end = today.replace(month=today.month + 1, day=1)

        await prisma.execute_raw(
            """
            INSERT INTO user_usage (user_id, period_start, period_end,
                                    prompt_tokens, completion_tokens, total_tokens,
                                    llm_calls, jobs_run)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 1)
            ON CONFLICT (user_id, period_start) DO UPDATE SET
                prompt_tokens = user_usage.prompt_tokens + EXCLUDED.prompt_tokens,
                completion_tokens = user_usage.completion_tokens + EXCLUDED.completion_tokens,
                total_tokens = user_usage.total_tokens + EXCLUDED.total_tokens,
                llm_calls = user_usage.llm_calls + EXCLUDED.llm_calls,
                jobs_run = user_usage.jobs_run + 1,
                updated_at = NOW()
            """,
            ledger.user_id,
            period_start.isoformat(),
            period_end.isoformat(),
            ledger.prompt_tokens,
            ledger.completion_tokens,
            ledger.total_tokens,
            ledger.llm_calls,
        )

        logger.info(f"[TokenTelemetry] Upserted user usage for {ledger.user_id}")

    except Exception as exc:
        logger.error(f"[TokenTelemetry] Failed to persist user usage: {exc}")


async def finalize_telemetry(job_id: str) -> Optional[JobTokenLedger]:
    """Close ledger, persist to both job and user tables, return final snapshot."""
    ledger = close_ledger(job_id)
    if ledger is None or ledger.total_tokens == 0:
        return ledger

    await persist_job_tokens(ledger)
    await persist_user_usage(ledger)
    return ledger


# =============================================================================
# DECORATOR — Wraps SightedPlanner._client.chat.completions.create
# =============================================================================

def with_telemetry(user_id: str, job_id: str):
    """
    Decorator factory that intercepts the OpenAI chat.completions.create
    response and records token usage into the job's TokenLedger.

    Usage:
        @with_telemetry(user_id="u1", job_id="j1")
        async def plan_epoch(self, ...):
            response = await self._client.chat.completions.create(...)
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ledger = get_ledger(job_id, user_id)
            start = time.time()

            result = await func(*args, **kwargs)

            duration_ms = int((time.time() - start) * 1000)

            # Extract usage from the EpochPlan's runtime attrs
            prompt_t = getattr(result, "_telemetry_prompt_tokens", 0)
            completion_t = getattr(result, "_telemetry_completion_tokens", 0)
            total_t = getattr(result, "_telemetry_total_tokens", 0)
            model = getattr(result, "model_used", "")

            if total_t > 0:
                ledger.record(TokenSnapshot(
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    total_tokens=total_t,
                    model=model,
                    duration_ms=duration_ms,
                ))

            return result
        return wrapper
    return decorator


# =============================================================================
# PLANNER MONKEY-PATCH — Inject telemetry into SightedPlanner at runtime
# =============================================================================

def instrument_planner(planner: Any, user_id: str, job_id: str) -> None:
    """
    Monkey-patch a SightedPlanner instance to inject token telemetry
    into every plan_epoch() call without modifying the planner source.
    """
    original_plan = planner.plan_epoch

    async def instrumented_plan(objective, history, active_tab, background_tabs, extracted_data=None):
        ledger = get_ledger(job_id, user_id)
        start = time.time()

        epoch = await original_plan(objective, history, active_tab, background_tabs, extracted_data)

        duration_ms = int((time.time() - start) * 1000)

        prompt_t = getattr(epoch, "_telemetry_prompt_tokens", 0)
        completion_t = getattr(epoch, "_telemetry_completion_tokens", 0)
        total_t = getattr(epoch, "_telemetry_total_tokens", 0)
        model = getattr(epoch, "model_used", "") or planner.model

        if total_t > 0:
            ledger.record(TokenSnapshot(
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                total_tokens=total_t,
                model=model,
                duration_ms=duration_ms,
            ))

        return epoch

    planner.plan_epoch = instrumented_plan
