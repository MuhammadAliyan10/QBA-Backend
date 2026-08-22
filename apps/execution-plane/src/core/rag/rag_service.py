"""
ragService.py - RAG Memory Service for Recipe Generation

Tri-Layer Memory Pipeline:
1. Embed: Convert user prompt to vector (text-embedding-3-small)
2. Search: Query pgvector for similar templates
3. Generate: LLM adapts template with RAG context

The system "learns" from successful workflows by saving them back.

Author: Quanta Box Paradox Engineering
Version: 1.0.0
"""

import os
import json
import logging
import hashlib
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import openai
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

logger = logging.getLogger("ragService")


# =============================================================================
# CONFIGURATION
# =============================================================================

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1536
SIMILARITY_THRESHOLD = 0.92


@dataclass
class TemplateMatch:
    """Result of a template search."""
    id: str
    category: str
    domain: str
    task_type: str
    recipe_json: Dict
    similarity: float

    @property
    def is_high_confidence(self) -> bool:
        return self.similarity >= SIMILARITY_THRESHOLD


# =============================================================================
# RAG SERVICE
# =============================================================================

class RAGService:
    """
    RAG Pipeline for Recipe Generation.

    Flow:
    1. find_template(prompt, url) - Check memory for existing template
    2. If hit (>92% similarity): Return verified template
    3. If miss: Proceed to generation with similar templates as context

    Learning:
    - On successful job completion, call save_template()
    - The system continuously improves from experience
    """

    def __init__(self, database_url: str = None, openai_api_key: str = None):
        """
        Initialize RAG Service.

        Args:
            database_url: PostgreSQL/Supabase connection string
            openai_api_key: OpenAI API key for embeddings
        """
        # Database connection
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise ValueError("DATABASE_URL not set")

        self.engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            poolclass=NullPool  # Fresh connections for each query
        )

        # OpenAI client
        api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if api_key:
            self.openai_client = openai.OpenAI(api_key=api_key)
        else:
            self.openai_client = None
            logger.warning("[RAG] OpenAI API key not set. Embeddings disabled.")

        logger.info("[RAG] Service initialized")

    # -------------------------------------------------------------------------
    # EMBEDDING
    # -------------------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.
        Uses OpenAI text-embedding-3-small (1536 dimensions).
        Wrapped in asyncio.to_thread — the openai sync client blocks otherwise.
        """
        if not self.openai_client:
            raise RuntimeError("OpenAI client not initialized")

        clean_text = " ".join(text.lower().split())[:8000]

        def _blocking_embed() -> list[float]:
            response = self.openai_client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=clean_text,
            )
            return response.data[0].embedding

        try:
            import asyncio
            embedding = await asyncio.to_thread(_blocking_embed)
            logger.debug(f"[RAG] Generated embedding (dim: {len(embedding)})")
            return embedding
        except Exception as e:
            logger.error(f"[RAG] Embedding failed: {e}")
            raise

    # -------------------------------------------------------------------------
    # SEARCH (Memory Check)
    # -------------------------------------------------------------------------

    async def find_template(
        self,
        prompt: str,
        url: str,
        limit: int = 3
    ) -> Optional[TemplateMatch]:
        """
        Search for matching template in memory.

        Layer 1 of the Preflight Pipeline:
        - If exact/high match (>92%): Return immediately
        - If partial match: Return for context
        - If no match: Return None

        Args:
            prompt: User's task description
            url: Target URL
            limit: Max results to return

        Returns:
            TemplateMatch if found, None otherwise
        """
        if not self.openai_client:
            logger.warning("[RAG] Embeddings disabled, skipping memory check")
            return None

        try:
            query_embedding = await self.embed(f"{prompt} {url}")

            from urllib.parse import urlparse
            domain = urlparse(url).netloc.replace("www.", "")

            import asyncio

            def _query_db() -> list:
                with self.engine.connect() as conn:
                    result = conn.execute(text("""
                        SELECT
                            id::text,
                            category,
                            domain,
                            task_type,
                            recipe_json,
                            1 - (embedding <=> :query_embedding::vector) AS similarity
                        FROM recipe_templates
                        WHERE
                            domain = :domain
                            OR 1 - (embedding <=> :query_embedding::vector) > 0.80
                        ORDER BY embedding <=> :query_embedding::vector
                        LIMIT :limit
                    """), {
                        "query_embedding": str(query_embedding),
                        "domain": domain,
                        "limit": limit,
                    })
                    return result.fetchall()

            rows = await asyncio.to_thread(_query_db)

            if not rows:
                logger.info(f"[RAG] No templates found for domain: {domain}")
                return None

            top = rows[0]
            match = TemplateMatch(
                id=top[0],
                category=top[1],
                domain=top[2],
                task_type=top[3],
                recipe_json=top[4] if isinstance(top[4], dict) else json.loads(top[4]),
                similarity=float(top[5]),
            )
            logger.info(
                f"[RAG] Found template: {match.task_type}@{match.domain} "
                f"(similarity: {match.similarity:.2%})"
            )
            return match

        except Exception as e:
            logger.error(f"[RAG] Template search failed: {e}")
            return None

    # -------------------------------------------------------------------------
    # SAVE (Learning Hook)
    # -------------------------------------------------------------------------

    async def save_template(
        self,
        prompt: str = "",
        url: str = "",
        category: str = "scraping",
        task_type: str = "",
        recipe_json: Optional[Dict] = None,
        # Legacy positional params kept for backward compat
        domain: str = "",
        description: str = "",
    ) -> Optional[str]:
        """
        Save a successful recipe as a template.
        Accepts both the new call signature (prompt, url, category, task_type, recipe_json)
        and the legacy signature (recipe_json, category, domain, task_type, description).
        The sync SQLAlchemy write is wrapped in asyncio.to_thread.
        """
        if not self.openai_client:
            logger.warning("[RAG] Embeddings disabled, cannot save template")
            return None

        # Normalise parameters — support both call conventions
        effective_domain = domain or (url.split("//")[-1].split("/")[0].replace("www.", "") if url else "unknown")
        effective_desc   = description or prompt or f"{task_type} on {effective_domain}"
        effective_recipe = recipe_json or {}

        try:
            import asyncio
            embed_text = f"{task_type} on {effective_domain}: {effective_desc}"
            embedding  = await self.embed(embed_text)

            recipe_hash = hashlib.sha256(
                json.dumps(effective_recipe, sort_keys=True).encode()
            ).hexdigest()[:16]

            def _write_db() -> Optional[str]:
                with self.engine.begin() as conn:
                    result = conn.execute(text("""
                        INSERT INTO recipe_templates
                            (category, domain, task_type, description, recipe_json, embedding)
                        VALUES
                            (:category, :domain, :task_type, :description,
                             :recipe_json::jsonb, :embedding::vector)
                        ON CONFLICT (domain, task_type)
                        DO UPDATE SET
                            recipe_json   = EXCLUDED.recipe_json,
                            embedding     = EXCLUDED.embedding,
                            success_count = recipe_templates.success_count + 1,
                            updated_at    = NOW()
                        RETURNING id::text
                    """), {
                        "category":    category,
                        "domain":      effective_domain,
                        "task_type":   task_type or "auto",
                        "description": effective_desc,
                        "recipe_json": json.dumps(effective_recipe),
                        "embedding":   str(embedding),
                    })
                    row = result.fetchone()
                    return row[0] if row else None

            template_id = await asyncio.to_thread(_write_db)
            logger.info(f"[RAG] Saved template: {task_type}@{effective_domain} (ID: {template_id})")
            return template_id

        except Exception as e:
            logger.error(f"[RAG] Failed to save template: {e}")
            return None

    # -------------------------------------------------------------------------
    # CONTEXT GENERATION
    # -------------------------------------------------------------------------

    def build_rag_context(self, template: TemplateMatch) -> str:
        """
        Build RAG context string for LLM prompt.

        Args:
            template: Retrieved template match

        Returns:
            Formatted context string
        """
        return f"""
## Proven Template (Similarity: {template.similarity:.0%})
- Category: {template.category}
- Domain: {template.domain}
- Task Type: {template.task_type}

### Template Structure:
```json
{json.dumps(template.recipe_json, indent=2)}
```

INSTRUCTION: Adapt this proven template for the user's specific request.
Do not reinvent the wheel. Preserve the logic that works.
"""


# =============================================================================
# SINGLETON
# =============================================================================

_rag_instance: Optional[RAGService] = None

def get_rag_service() -> RAGService:
    """Get singleton RAG service instance."""
    global _rag_instance
    if _rag_instance is None:
        _rag_instance = RAGService()
    return _rag_instance
