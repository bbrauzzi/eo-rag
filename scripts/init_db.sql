-- Enable the pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Table for the technical documentation chunks (Step 1 - RAG)
CREATE TABLE IF NOT EXISTS doc_chunks (
    id BIGSERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    embedding vector(1024),          -- dimension for amazon.titan-embed-text-v2:0; keep in sync with EMBEDDING_DIM
    source TEXT NOT NULL,            -- e.g. 'stac-spec.md'
    section TEXT,                    -- e.g. 'Item properties'
    url TEXT,                        -- link to the original source, when available
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Index for similarity search (IVFFlat, fine to start with on small volumes)
CREATE INDEX IF NOT EXISTS doc_chunks_embedding_idx
    ON doc_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Table for the eval harness (Step 9), created now so we don't forget it later
CREATE TABLE IF NOT EXISTS eval_cases (
    id BIGSERIAL PRIMARY KEY,
    question TEXT NOT NULL,
    expected_answer TEXT,
    expected_tool_calls TEXT[],       -- e.g. ARRAY['stac_search', 'compute_index']
    created_at TIMESTAMPTZ DEFAULT now()
);
