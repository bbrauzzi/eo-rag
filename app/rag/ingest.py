"""
Ingestion pipeline: takes a technical document (markdown), splits it into chunks
following its structure (sections/paragraphs, not fixed character counts), computes
the embeddings and stores them in pgvector.

Usage:
    python -m app.rag.ingest path/to/doc.md --source "stac-spec.md"
"""

import argparse
import re
from pathlib import Path

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from app.db.models import DocChunk
from app.db.session import SessionLocal
from app.rag.embeddings import embed_texts

HEADERS_TO_SPLIT_ON = [
    ("#", "title"),
    ("##", "section"),
    ("###", "subsection"),
]

HEADER_LINE = re.compile(r"^#{1,6} \S")


def _is_header_only(content: str) -> bool:
    """True if the chunk holds nothing but header lines (no usable text)."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return bool(lines) and all(HEADER_LINE.match(line) for line in lines)


def _merge_header_only_chunks(chunks: list[dict]) -> list[dict]:
    """
    The char splitter can detach a header from its body, producing chunks that hold
    only the "## Section" line: pointless to embed, and the following chunk is left
    without its context. We prepend them to the next chunk of the same section; if
    there is none (empty section) they are dropped.
    """
    merged: list[dict] = []
    pending: list[dict] = []

    for chunk in chunks:
        if _is_header_only(chunk["content"]):
            pending.append(chunk)
            continue

        if pending and pending[-1]["section"] == chunk["section"]:
            headers = "\n".join(p["content"].strip() for p in pending)
            chunk = {**chunk, "content": f"{headers}\n{chunk['content']}"}

        pending = []
        merged.append(chunk)

    return merged


def split_markdown(text: str) -> list[dict]:
    """Split by structure (sections), then fall back to paragraphs/sentences if too long."""
    md_splitter = MarkdownHeaderTextSplitter(HEADERS_TO_SPLIT_ON, strip_headers=False)
    section_docs = md_splitter.split_text(text)

    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
        separators=["\n\n", "\n", ". ", " "],  # try paragraphs first, then sentences
    )
    final_docs = char_splitter.split_documents(section_docs)

    chunks = [
        {
            "content": doc.page_content,
            "section": doc.metadata.get("section") or doc.metadata.get("title"),
        }
        for doc in final_docs
    ]

    return _merge_header_only_chunks(chunks)


def embed_chunks(chunks: list[str]) -> list[list[float]]:
    """Embed the chunks to be indexed (delegates to Bedrock/Titan)."""
    return embed_texts(chunks)


def ingest_file(path: Path, source: str, url: str | None = None) -> int:
    text = path.read_text(encoding="utf-8")
    chunks = split_markdown(text)

    if not chunks:
        return 0

    embeddings = embed_chunks([c["content"] for c in chunks])

    db = SessionLocal()
    try:
        for chunk, embedding in zip(chunks, embeddings):
            db.add(
                DocChunk(
                    content=chunk["content"],
                    embedding=embedding,
                    source=source,
                    section=chunk["section"],
                    url=url,
                )
            )
        db.commit()
    finally:
        db.close()

    return len(chunks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest a markdown doc into pgvector")
    parser.add_argument("path", type=Path, help="Path to the markdown file to index")
    parser.add_argument("--source", required=True, help="Source name (e.g. stac-spec.md)")
    parser.add_argument("--url", default=None, help="Original URL of the document")
    args = parser.parse_args()

    n = ingest_file(args.path, args.source, args.url)
    print(f"Indexed {n} chunks from {args.source}.")
