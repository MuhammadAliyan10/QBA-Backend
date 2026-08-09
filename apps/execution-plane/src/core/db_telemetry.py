import os
import logging
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy.pool import NullPool

logger = logging.getLogger("db_telemetry")

# Lazily created to avoid import-time crash when DATABASE_URL is not set.
_engine: Optional[AsyncEngine] = None


def _get_engine() -> Optional[AsyncEngine]:
    """
    Returns a lazily-initialized async SQLAlchemy engine using asyncpg.

    Engine is created once and reused. Returns None if DATABASE_URL is not
    configured so callers can skip gracefully.
    """
    global _engine
    if _engine is not None:
        return _engine

    raw_url = os.getenv("DATABASE_URL", "")

    # Normalise scheme → always use the asyncpg driver dialect.
    # AccountManager uses: postgres:// or postgresql://
    # We need:            postgresql+asyncpg://
    if raw_url.startswith("postgres://"):
        raw_url = raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif raw_url.startswith("postgresql://") and "+asyncpg" not in raw_url:
        raw_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif not raw_url:
        # Fall back to docker-compose service name
        raw_url = "postgresql+asyncpg://postgres:postgres@app_postgres:5432/quanta"

    try:
        _engine = create_async_engine(raw_url, poolclass=NullPool)
        logger.debug("[DBTelemetry] Async engine initialised")
    except Exception as e:
        logger.error(f"[DBTelemetry] Failed to create engine: {e}")
        _engine = None

    return _engine


async def record_job_token_usage(
    job_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    llm_calls: int,
    model: str = "",
) -> None:
    """
    Atomically increments token telemetry columns on the jobs row.
    The database trigger on ledger_transactions propagates totals to user_usage.
    Non-fatal: logs errors but does not raise.
    """
    engine = _get_engine()
    if engine is None:
        logger.warning(f"[{job_id}] DB Telemetry skipped: no database engine available.")
        return

    total_tokens = prompt_tokens + completion_tokens

    query = text("""
        UPDATE jobs
        SET prompt_tokens     = prompt_tokens     + :prompt,
            completion_tokens = completion_tokens + :completion,
            total_tokens      = total_tokens      + :total,
            llm_calls         = llm_calls         + :calls,
            model_used        = COALESCE(NULLIF(:model, ''), model_used)
        WHERE id = :job_id
    """)

    try:
        async with engine.begin() as conn:
            result = await conn.execute(query, {
                "prompt":     prompt_tokens,
                "completion": completion_tokens,
                "total":      total_tokens,
                "calls":      llm_calls,
                "model":      model,
                "job_id":     job_id,
            })
            if result.rowcount > 0:
                logger.info(
                    f"[{job_id}] DB Telemetry written: "
                    f"+{total_tokens} tokens | {llm_calls} LLM call(s) | model={model or 'unknown'}"
                )
            else:
                logger.warning(
                    f"[{job_id}] DB Telemetry UPDATE matched 0 rows — job may not exist yet."
                )
    except Exception as exc:
        logger.error(f"[{job_id}] DB Telemetry write failed: {exc}", exc_info=True)
