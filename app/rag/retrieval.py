"""Top-k retrieval from pgvector, given a user query."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DocChunk
from app.rag.embeddings import embed_text


def embed_query(query: str) -> list[float]:
    """Embed the user query: same model used at ingestion time."""
    return embed_text(query)


def retrieve_with_scores(db: Session, query: str, top_k: int = 5) -> list[tuple[DocChunk, float]]:
    """
    Same ranking as retrieve(), but keeps the cosine distance of every hit (0 = identical,
    1 = orthogonal). Useful to tell a genuinely good match from the least bad one.
    """
    query_embedding = embed_query(query)
    distance = DocChunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = select(DocChunk, distance).order_by(distance).limit(top_k)
    return [(chunk, dist) for chunk, dist in db.execute(stmt)]


def retrieve(db: Session, query: str, top_k: int = 5) -> list[DocChunk]:
    return [chunk for chunk, _ in retrieve_with_scores(db, query, top_k)]


def format_context(chunks: list[DocChunk]) -> str:
    """Format the retrieved chunks as prompt context, with the source cited."""
    parts = []
    for c in chunks:
        header = f"[Source: {c.source}" + (f" - {c.section}]" if c.section else "]")
        parts.append(f"{header}\n{c.content}")
    return "\n\n---\n\n".join(parts)
