-- Migration 004: Recipe Templates with pgvector
-- Purpose: Store successful recipes with semantic embeddings for RAG
-- Requires: pgvector extension enabled in Supabase
-- Author: Quanta Box Paradox Engineering
-- Date: 2025-12-26

-- =============================================================================
-- 1. ENABLE PGVECTOR EXTENSION
-- =============================================================================
-- NOTE: In Supabase, this is done via Dashboard > Database > Extensions
-- But we include it here for completeness

CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- 2. CREATE RECIPE TEMPLATES TABLE
-- =============================================================================

CREATE TABLE IF NOT EXISTS recipe_templates (
    -- Primary Key
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Classification
    category TEXT NOT NULL,              -- "ecommerce", "social", "banking", "saas"
    domain TEXT NOT NULL,                -- "amazon.com", "linkedin.com"
    task_type TEXT NOT NULL,             -- "login", "scrape_list", "checkout", "form_fill"

    -- Content
    description TEXT,                    -- Human-readable description
    recipe_json JSONB NOT NULL,          -- The actual recipe structure

    -- Semantic Search
    -- Dimension: 384 = all-MiniLM-L6-v2 (sentence-transformers model used by recipe_manager.py)
    -- DO NOT change to 1536 (OpenAI) — the execution-plane does not use OpenAI embeddings.
    embedding vector(384),

    -- Quality Metrics
    success_count INT DEFAULT 1,         -- Number of successful executions
    failure_count INT DEFAULT 0,         -- Number of failed executions
    avg_duration_ms INT,                 -- Average execution time

    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_used_at TIMESTAMP,

    -- Constraints
    CONSTRAINT recipe_templates_category_check CHECK (
        category IN ('ecommerce', 'social', 'banking', 'saas', 'news', 'portal', 'government', 'entertainment', 'other')
    )
);

-- =============================================================================
-- 3. INDEXES FOR FAST SEARCH
-- =============================================================================

-- Vector similarity search (IVFFlat index for approximate nearest neighbor)
-- NOTE: Requires at least 100 rows before this index is useful
CREATE INDEX IF NOT EXISTS idx_recipe_templates_embedding
    ON recipe_templates
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Domain-based filtering
CREATE INDEX IF NOT EXISTS idx_recipe_templates_domain
    ON recipe_templates(domain);

-- Category-based filtering
CREATE INDEX IF NOT EXISTS idx_recipe_templates_category
    ON recipe_templates(category);

-- Task type filtering
CREATE INDEX IF NOT EXISTS idx_recipe_templates_task_type
    ON recipe_templates(task_type);

-- Composite for common query pattern
CREATE INDEX IF NOT EXISTS idx_recipe_templates_domain_task
    ON recipe_templates(domain, task_type);

-- =============================================================================
-- 4. RLS POLICIES (Row Level Security)
-- =============================================================================
-- Templates are public/shared, so minimal RLS

ALTER TABLE recipe_templates ENABLE ROW LEVEL SECURITY;

-- Allow read access to all authenticated users
CREATE POLICY "recipe_templates_read_policy" ON recipe_templates
    FOR SELECT
    USING (true);

-- Allow insert/update only to service role (backend)
CREATE POLICY "recipe_templates_write_policy" ON recipe_templates
    FOR ALL
    USING (auth.role() = 'service_role' OR auth.role() = 'postgres');

-- =============================================================================
-- 5. HELPER FUNCTIONS
-- =============================================================================

-- Function to update success metrics
CREATE OR REPLACE FUNCTION update_template_success(
    p_template_id UUID,
    p_duration_ms INT
)
RETURNS VOID AS $$
BEGIN
    UPDATE recipe_templates
    SET
        success_count = success_count + 1,
        avg_duration_ms = COALESCE(
            (avg_duration_ms * (success_count - 1) + p_duration_ms) / success_count,
            p_duration_ms
        ),
        last_used_at = NOW(),
        updated_at = NOW()
    WHERE id = p_template_id;
END;
$$ LANGUAGE plpgsql;

-- Function to find similar templates
CREATE OR REPLACE FUNCTION find_similar_templates(
    p_embedding vector(384),
    p_domain TEXT DEFAULT NULL,
    p_limit INT DEFAULT 3
)
RETURNS TABLE (
    id UUID,
    category TEXT,
    domain TEXT,
    task_type TEXT,
    recipe_json JSONB,
    similarity FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        t.id,
        t.category,
        t.domain,
        t.task_type,
        t.recipe_json,
        1 - (t.embedding <=> p_embedding) AS similarity
    FROM recipe_templates t
    WHERE
        (p_domain IS NULL OR t.domain = p_domain)
        AND t.embedding IS NOT NULL
    ORDER BY t.embedding <=> p_embedding
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- 6. COMMENTS
-- =============================================================================

COMMENT ON TABLE recipe_templates IS 'Proven recipe templates with semantic embeddings for RAG-based generation';
COMMENT ON COLUMN recipe_templates.embedding IS 'all-MiniLM-L6-v2 sentence-transformer vector (384 dimensions). Generated by execution-plane recipe_manager.py';
COMMENT ON COLUMN recipe_templates.success_count IS 'Number of successful job completions using this template';
COMMENT ON COLUMN recipe_templates.recipe_json IS 'Full Recipe Schema v2.0 JSON structure';

-- =============================================================================
-- 7. SEED DATA (Optional - for testing)
-- =============================================================================

-- Uncomment to seed with example template
/*
INSERT INTO recipe_templates (category, domain, task_type, description, recipe_json)
VALUES (
    'ecommerce',
    'example.com',
    'scrape',
    'Basic scrape template for example.com',
    '{
        "version": "2.0.0",
        "metadata": {"id": "seed-1", "name": "Example Scraper"},
        "nodes": [],
        "edges": [],
        "entry_point": "node_start",
        "exit_points": {"success": "node_end", "failure": "node_end", "timeout": "node_end"}
    }'::jsonb
)
ON CONFLICT DO NOTHING;
*/

-- =============================================================================
-- MIGRATION COMPLETE
-- =============================================================================
