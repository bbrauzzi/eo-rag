"""
Documentation lookup tool: the RAG path (embed -> pgvector top-k -> formatted context)
exposed to the model as something it can decide to call.

Thin on purpose. The retrieval logic stays in `app/rag/retrieval.py`; this module only
adapts it to the tool-calling contract - a schema Claude can read, and a return value
carrying both the text to feed back and the sources the answer should cite.
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db.models import DocChunk
from app.rag.retrieval import format_context, retrieve_with_scores

DEFAULT_TOP_K = 5

# Sent back when nothing matches: an empty tool_result tells the model nothing, and the
# API rejects empty content anyway.
NO_MATCH = "No matching documentation was found for this query."


class LookupResult(BaseModel):
    """
    What `rag_lookup` returns, split three ways by consumer.

    `context` is prose and goes to the model verbatim - this is the one tool whose result
    is *not* serialized as JSON, because the retrieved passages already carry their own
    `[Source: ...]` labels and wrapping them in JSON would only add escaping. `sources` is
    provenance and goes to the answer. `scored` is **telemetry and reaches neither**: the
    graph hands it to the trace and nothing else ever reads it.

    That third split is the same idea as `ItemSummary.geometry`, which the map needs and
    the model is only made worse by. Here it is the cosine distances: an answer built on
    the best of a uniformly poor set of chunks looks identical to a good one from the
    outside, and this is the field that tells them apart.
    """

    # DocChunk is a SQLAlchemy model, not something pydantic can validate or copy.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    context: str
    sources: list[str] = []
    scored: list[tuple[DocChunk, float]] = []


def rag_lookup(db: Session, query: str, top_k: int = DEFAULT_TOP_K) -> LookupResult:
    """
    Retrieve the top-k documentation chunks for `query`, formatted as prompt context.

    Uses `retrieve_with_scores` rather than `retrieve` because the distances were being
    computed and discarded anyway - the ranking is a `cosine_distance` ORDER BY either
    way. Keeping them costs one float per chunk and is the whole of the retrieval-quality
    signal.
    """
    scored = retrieve_with_scores(db, query, top_k=top_k)
    chunks = [chunk for chunk, _ in scored]

    return LookupResult(
        context=format_context(chunks) or NO_MATCH,
        sources=sorted({c.source for c in chunks}),
        scored=scored,
    )


# `top_k` is deliberately absent from the schema: how many chunks to retrieve is a
# retrieval-tuning decision, not something the model should be improvising per question.
RAG_LOOKUP_TOOL = {
    "name": "rag_lookup",
    "description": (
        "Search the indexed technical documentation (the STAC specification and related "
        "specs) and return the passages that best match the query, with their source. "
        "Use it for questions about how something is defined, structured or supposed to "
        "work. It knows nothing about which satellite data actually exists."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to look up. A focused phrasing of the concept retrieves better "
                    "than the user's question copied verbatim."
                ),
            },
        },
        "required": ["query"],
    },
}
