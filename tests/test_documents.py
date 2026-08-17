"""
Tests for reading the corpus by identity.

Against a **real database** rather than a stub: `doc_chunks` creates cleanly on in-memory
SQLite (the `eval_cases` ARRAY column is the only thing in the metadata that does not, so
just that one table is created). That matters more than it sounds - the whole point of
these functions is ordering and grouping, which SQL does, and a stub session returning
pre-ordered canned rows would make every ordering assertion here vacuous.

Still fully offline: SQLite in memory, no Postgres, no network, and no Bedrock - which one
test asserts outright, since staying off AWS is exactly what separates this module from
`app/rag/retrieval.py`.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, DocChunk
from app.rag.documents import (
    JOIN,
    list_sections,
    list_sources,
    read_document,
    read_section,
)

# The embedding is never read by anything in app/rag/documents.py, but the column is NOT
# NULL-friendly in spirit and a real row should look like a real row.
ZERO_VECTOR = [0.0] * 1024


@pytest.fixture
def db():
    """An empty doc_chunks table on in-memory SQLite."""
    engine = create_engine("sqlite://")
    # Only this table: eval_cases uses ARRAY(String), which SQLite cannot compile, and it
    # has nothing to do with anything here.
    Base.metadata.create_all(engine, tables=[DocChunk.__table__])
    session = sessionmaker(bind=engine)()

    try:
        yield session
    finally:
        session.close()


def add(db, id, content, source="stac-spec-core", section=None, url=None):
    db.add(
        DocChunk(
            id=id,
            content=content,
            embedding=ZERO_VECTOR,
            source=source,
            section=section,
            url=url,
            created_at=datetime.now(UTC),
        )
    )
    db.commit()


# --- reading a document -------------------------------------------------------


def test_a_document_is_reassembled_in_document_order(db):
    """
    Inserted out of order on purpose. Order comes from the primary key, because ingestion
    inserts in the order the chunker produced - and this is the test that says so.
    """
    add(db, 3, "third")
    add(db, 1, "first")
    add(db, 2, "second")

    assert read_document(db, "stac-spec-core") == f"first{JOIN}second{JOIN}third"


def test_chunks_are_joined_by_a_blank_line(db):
    """Headers stay in the content, so a blank line reproduces readable markdown."""
    add(db, 1, "## Item fields", section="Item fields")
    add(db, 2, "The id field is REQUIRED.", section="Item fields")

    assert read_document(db, "stac-spec-core") == "## Item fields\n\nThe id field is REQUIRED."


def test_only_the_requested_document_comes_back(db):
    add(db, 1, "stac", source="stac-spec-core")
    add(db, 2, "other", source="something-else")

    assert read_document(db, "stac-spec-core") == "stac"


def test_an_unknown_source_raises_rather_than_returning_nothing(db):
    """
    Empty string and "no such document" are different answers, and the second is the true
    one. ValueError is the convention the HTTP layer maps to 404.
    """
    add(db, 1, "content")

    with pytest.raises(ValueError, match="No document ingested under source 'nope'"):
        read_document(db, "nope")


# --- reading a section --------------------------------------------------------


def test_a_section_returns_only_its_own_chunks_in_order(db):
    add(db, 1, "overview text", section="Item Overview")
    add(db, 3, "fields part two", section="Item fields")
    add(db, 2, "fields part one", section="Item fields")

    assert read_section(db, "stac-spec-core", "Item fields") == f"fields part one{JOIN}fields part two"


def test_a_section_of_another_document_is_not_returned(db):
    """Section names are not unique across documents; the pair is the address."""
    add(db, 1, "ours", source="a", section="Overview")
    add(db, 2, "theirs", source="b", section="Overview")

    assert read_section(db, "a", "Overview") == "ours"


def test_an_unknown_section_raises_and_names_both_parts(db):
    add(db, 1, "content", section="Item fields")

    with pytest.raises(ValueError, match="No section 'Nope' in document 'stac-spec-core'"):
        read_section(db, "stac-spec-core", "Nope")


def test_a_known_section_of_an_unknown_document_also_raises(db):
    add(db, 1, "content", section="Item fields")

    with pytest.raises(ValueError):
        read_section(db, "no-such-doc", "Item fields")


# --- listing ------------------------------------------------------------------


def test_sections_come_back_deduped_and_in_document_order(db):
    """
    Not alphabetical: the order sections appear in is the shape of the document, and
    'Catalog Overview' preceding 'Item fields' is information a client can use.
    """
    add(db, 1, "a", section="Item Overview")
    add(db, 2, "b", section="Item Overview")
    add(db, 3, "c", section="Catalog fields")
    add(db, 4, "d", section="Item fields")

    assert list_sections(db, "stac-spec-core") == [
        "Item Overview",
        "Catalog fields",
        "Item fields",
    ]


def test_chunks_without_a_section_are_not_listed_but_are_still_read(db):
    """There is no docs://section/... URI that could address them; the document has them."""
    add(db, 1, "preamble", section=None)
    add(db, 2, "body", section="Item fields")

    assert list_sections(db, "stac-spec-core") == ["Item fields"]
    assert "preamble" in read_document(db, "stac-spec-core")


def test_the_index_describes_every_document(db):
    add(db, 1, "a", source="stac-spec-core", section="Overview", url="https://example.test/stac")
    add(db, 2, "b", source="stac-spec-core", section="Item fields", url="https://example.test/stac")
    add(db, 3, "c", source="other-doc", section="Intro")

    sources = list_sources(db)

    assert [s.source for s in sources] == ["stac-spec-core", "other-doc"]
    first = sources[0]
    assert first.chunks == 2
    assert first.url == "https://example.test/stac"
    assert first.sections == ["Overview", "Item fields"]


def test_the_index_is_empty_rather_than_failing_on_an_empty_corpus(db):
    """Nothing ingested yet is a state a client should be told about, not an error."""
    assert list_sources(db) == []


def test_a_document_ingested_twice_appears_twice(db):
    """
    Recorded rather than hidden. Nothing dedupes on `source`, so a re-ingestion without the
    prescribed TRUNCATE doubles the document - and a DISTINCT here would make that look
    healthy while retrieval quietly ranked against both copies.
    """
    add(db, 1, "body", section="Item fields")
    add(db, 2, "body", section="Item fields")

    assert read_document(db, "stac-spec-core") == f"body{JOIN}body"
    assert list_sections(db, "stac-spec-core") == ["Item fields"]


# --- the point of the module --------------------------------------------------


def test_reading_the_corpus_never_reaches_bedrock(db, monkeypatch):
    """
    The reason this is not in `app/rag/retrieval.py`, asserted: every function there embeds
    a query and pays AWS, and these read by identity. It is what lets the MCP documentation
    resources work with no credentials at all.
    """

    def boom(*args, **kwargs):
        raise AssertionError("documents.py must not embed anything")

    monkeypatch.setattr("app.rag.embeddings.embed_text", boom)
    monkeypatch.setattr("app.rag.retrieval.embed_query", boom)

    add(db, 1, "content", section="Item fields")

    list_sources(db)
    list_sections(db, "stac-spec-core")
    read_document(db, "stac-spec-core")
    read_section(db, "stac-spec-core", "Item fields")
