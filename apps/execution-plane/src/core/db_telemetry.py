import os
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger("db_telemetry")

# Reuse the same config pattern as AccountManager
db_url = os.getenv("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
elif db_url and db_url.startswith("postgresql://"):
    pass
else:
    # Default to standard docker-compose internal URL if missing
    db_url = "postgresql://postgres:postgres@app_postgres:5432/quanta"

# Use NullPool since Temporal workers are highly concurrent and connection pooling 
# is better handled by PgBouncer or the Control Plane.
engine = create_async_engine(db_url, poolclass=NullPool)

async def record_job_token_usage(job_id: str, prompt_tokens: int, completion_tokens: int, llm_calls: int, model: str = ""):
    """
    Directly updates the jobs table with token telemetry.
    The database triggers will automatically propagate this to the user_usage table.
    """
    total_tokens = prompt_tokens + completion_tokens
    
    query = text("""
        UPDATE jobs
        SET prompt_tokens = prompt_tokens + :prompt,
            completion_tokens = completion_tokens + :completion,
            total_tokens = total_tokens + :total,
            llm_calls = llm_calls + :calls,
            model_used = COALESCE(NULLIF(:model, ''), model_used)
        WHERE id = :job_id
    """)
    
    try:
        async with engine.begin() as conn:
            result = await conn.execute(query, {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": total_tokens,
                "calls": llm_calls,
                "model": model,
                "job_id": job_id
            })
            if result.rowcount > 0:
                logger.info(f"[{job_id}] DB Telemetry updated: +{total_tokens} tokens ({llm_calls} calls)")
            else:
                logger.warning(f"[{job_id}] DB Telemetry update failed: Job ID not found in database.")
    except Exception as e:
        logger.error(f"[{job_id}] DB Telemetry error: {e}", exc_info=True)
