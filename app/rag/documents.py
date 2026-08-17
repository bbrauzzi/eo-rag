"""
Reading the ingested corpus by identity rather than by similarity.

`app/rag/retrieval.py` answers "what is this question closest to"; this answers "give me
that document". They are deliberately separate modules because every function in retrieval
embeds a query and therefore pays Bedrock, while nothing here touches AWS at all - it is
`SELECT ... WHERE source = ?`. That separation is what lets the MCP documentation resources
be served, and tested, with no credentials of any kind.

Written for `app/mcp/resources.py`, which exposes the corpus as MCP resources: an MCP client
browses and reads whole sections, where the agent searches. Both are legitimate ways to use
the same 113 rows.

## Order comes from the primary key

`ingest_file` inserts chunks in the order `split_markdown` produced them, which is document
order, so `ORDER BY id` reassembles a document as it was written. That is a property of how
ingestion works rather than of the schema - there is no explicit position column - so it is
worth stating: **if ingestion ever stops inserting in order, or starts inserting
concurrently, these functions silently start returning scrambled prose.**

## Ingesting twice duplicates a document

`ingest_file` appends and nothing dedupes on `source`, so a corpus ingested twice comes back
from `read_document` twice over. The fix is the `TRUNCATE doc_chunks` that CLAUDE.md already
prescribes before re-ingesting, not a `DISTINCT` here: hiding it would make a
half-re-ingested database look healthy while retrieval quietly ranked against both copies.
"""

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DocChunk

# What joins two chunks back together. The chunker keeps headers in the content
# (`strip_headers=False`) and `_merge_header_only_chunks` reattaches any heading it detached,
# so a blank line between chunks reproduces readable markdown rather than a wall of text.
JOIN = "\n\n"


class SourceSummary(BaseModel):
    """One ingested document, as the resource index describes it."""

    source: str
    url: str | None = None
    sections: list[str] = []
    chunks: int = 0


def list_sources(db: Session) -> list[SourceSummary]:
    """
    Every document in the corpus, with its sections, in ingestion order.

    This is what makes the resource templates usable: a client cannot guess `stac-spec-core`
    or `Item fields`, and MCP does not enumerate templated URIs for it.
    """
    rows = db.execute(
        select(
            DocChunk.source,
            func.count(DocChunk.id),
            # An arbitrary non-null url for the document. Every chunk of one ingestion
            # carries the same one, so `min` is a way to pick it without a second query,
            # not a claim that the smallest url means anything.
            func.min(DocChunk.url),
        )
        .group_by(DocChunk.source)
        .order_by(func.min(DocChunk.id))
    ).all()

    return [
        SourceSummary(
            source=source,
            url=url,
            chunks=chunks,
            sections=list_sections(db, source),
        )
        for source, chunks, url in rows
    ]


def list_sections(db: Session, source: str) -> list[str]:
    """
    The sections of one document, deduped, in document order.

    Ordered by the *first* chunk of each section rather than alphabetically, because the
    order sections appear in is information: it is the shape of the document.

    Chunks with no section are left out. They are real content and `read_document` returns
    them, but there is no `docs://section/...` URI that could address them.
    """
    return list(
        db.execute(
            select(DocChunk.section)
            .where(DocChunk.source == source, DocChunk.section.is_not(None))
            .group_by(DocChunk.section)
            .order_by(func.min(DocChunk.id))
        ).scalars()
    )


def read_document(db: Session, source: str) -> str:
    """
    One whole document, reassembled from its chunks.

    Raises ValueError when there is no such source - the same convention `stac_search` uses
    for an item the catalog does not have, and what the HTTP layer maps to a 404.
    """
    chunks = list(
        db.execute(
            select(DocChunk.content)
            .where(DocChunk.source == source)
            .order_by(DocChunk.id)
        ).scalars()
    )

    if not chunks:
        raise ValueError(f"No document ingested under source {source!r}")

    return JOIN.join(chunks)


def read_section(db: Session, source: str, section: str) -> str:
    """
    One section of one document.

    The distinction from `read_document` matters for an MCP client: `stac-spec-core` is 65 KB
    and a section is a page, so reading the section is the difference between giving a model
    the answer and giving it the specification.
    """
    chunks = list(
        db.execute(
            select(DocChunk.content)
            .where(DocChunk.source == source, DocChunk.section == section)
            .order_by(DocChunk.id)
        ).scalars()
    )

    if not chunks:
        raise ValueError(f"No section {section!r} in document {source!r}")

    return JOIN.join(chunks)
