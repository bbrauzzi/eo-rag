"""
Retrieval validation script for eo-rag.

Takes a set of test questions, embeds them with the same model used at ingestion time
(Titan Text Embeddings V2), queries Postgres/pgvector with cosine similarity and prints
the top-k chunks returned for each question - so you can judge by eye whether the
ranking makes sense before building the rag_lookup tool for the agent.

No LLM call and no FastAPI involved: this is the raw retrieval path behind POST /ask.

Run it as a module from the repository root, like the ingestion CLI:

    export DATABASE_URL="postgresql://user:pass@localhost:5432/eorag"
    export AWS_REGION="eu-west-1"

    python -m scripts.retrieve_test --top-k 3
    python -m scripts.retrieve_test --query "which fields are required on an Item?"
"""

import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db.models import DocChunk
from app.db.session import SessionLocal
from app.rag.retrieval import retrieve_with_scores

# Grounded in the sections that data/stac-spec-core.md actually contains: a question
# with no possible answer in the corpus tells you nothing about the ranking.
DEFAULT_QUESTIONS = [
    "What are the required fields of a STAC Item?",
    "What is the difference between a Catalog and a Collection?",
    "Which media type should be used for a STAC Item?",
    "How do STAC extensions work?",
    "What is a standalone Collection?",
    "What does the assets field of an Item contain?",
]

SEPARATOR = "=" * 78


def preview(content: str, max_chars: int) -> str:
    """Collapse the chunk to a single indented block, truncated unless max_chars is 0."""
    text = content.strip()
    if max_chars and len(text) > max_chars:
        text = f"{text[:max_chars].rstrip()}..."
    return "\n".join(f"    {line}" for line in text.splitlines())


def report(question: str, hits: list[tuple[DocChunk, float]], max_chars: int) -> None:
    print(f"\n{SEPARATOR}\nQ: {question}\n{SEPARATOR}")

    if not hits:
        print("  (no chunks returned)")
        return

    for rank, (chunk, distance) in enumerate(hits, start=1):
        # Titan vectors are normalized, so cosine similarity is just 1 - distance.
        location = f"{chunk.source} - {chunk.section}" if chunk.section else chunk.source
        print(f"\n  [{rank}] similarity {1 - distance:.3f}  |  {location}")
        print(preview(chunk.content, max_chars))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect what pgvector returns for a set of questions"
    )
    parser.add_argument("--top-k", type=int, default=3, help="Chunks to retrieve per question")
    parser.add_argument(
        "--query",
        action="append",
        metavar="TEXT",
        help="Question to run instead of the built-in set (repeatable)",
    )
    parser.add_argument(
        "--chars",
        type=int,
        default=400,
        help="Truncate each chunk to this many characters (0 = print it whole)",
    )
    args = parser.parse_args()

    questions = args.query or DEFAULT_QUESTIONS
    print(
        f"Model: {settings.embedding_model} (dim {settings.embedding_dim}), "
        f"region {settings.aws_region}"
    )
    print(f"Questions: {len(questions)}, top-k: {args.top_k}")

    db = SessionLocal()
    try:
        indexed = db.scalar(select(func.count(DocChunk.id)))
        if not indexed:
            print(
                "\nNo chunks indexed - retrieval has nothing to rank. Ingest first:\n"
                '  python -m app.rag.ingest data/stac-spec-core.md --source "stac-spec.md"',
                file=sys.stderr,
            )
            return 1
        print(f"Indexed chunks: {indexed}")

        for question in questions:
            report(question, retrieve_with_scores(db, question, top_k=args.top_k), args.chars)
    except RuntimeError as e:
        # Raised by embeddings.embed_text: already carries the Bedrock model-access hint.
        print(f"\n{e}", file=sys.stderr)
        return 1
    except SQLAlchemyError as e:
        print(f"\nDatabase error ({settings.database_url}): {e}", file=sys.stderr)
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
