"""
Tests for the documentation resources.

SDK-free like the module under test, so these run without the `mcp` extra. What the SDK
does with these URIs - listing the static one, listing the two templates, and decoding the
parameters - is covered in `tests/test_mcp_server.py`, which needs the extra.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, DocChunk
from app.mcp import resources as mcp_resources
from app.mcp.resources import (
    DOCUMENT_URI,
    RESOURCES,
    SECTION_URI,
    SOURCES_URI,
    document_body,
    section_body,
    sources_index,
)


@pytest.fixture
def corpus(monkeypatch):
    """A small real corpus on in-memory SQLite, wired in as the resources' session."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[DocChunk.__table__])
    session = sessionmaker(bind=engine)()

    rows = [
        (1, "# STAC Overview", "STAC Overview"),
        (2, "## Item fields", "Item fields"),
        (3, "The id field is REQUIRED.", "Item fields"),
        (4, "## Media Type for STAC Item", "Media Type for STAC Item"),
    ]
    for id, content, section in rows:
        session.add(
            DocChunk(
                id=id,
                content=content,
                embedding=[0.0] * 1024,
                source="stac-spec-core",
                section=section,
                url="https://example.test/stac",
                created_at=datetime.now(UTC),
            )
        )
    session.commit()

    # The resources open their own session; give them this one and never close it here.
    monkeypatch.setattr(mcp_resources, "_session", lambda: _fixed(session))

    try:
        yield session
    finally:
        session.close()


class _fixed:
    """A context manager yielding an already-open session."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


# --- the index ----------------------------------------------------------------


def test_the_index_is_json_a_client_can_build_uris_from(corpus):
    index = json.loads(sources_index())

    assert len(index) == 1
    assert index[0]["source"] == "stac-spec-core"
    assert index[0]["chunks"] == 4
    assert index[0]["sections"] == [
        "STAC Overview",
        "Item fields",
        "Media Type for STAC Item",
    ]


def test_the_index_says_how_to_use_the_templates():
    """
    MCP can list a template's shape but not its values, so this description is the only
    thing standing between a client and guessing. It names both templates.
    """
    uri, _, mime, description = RESOURCES[0]

    assert uri == SOURCES_URI
    assert mime == "application/json"
    assert DOCUMENT_URI in description
    assert SECTION_URI in description


def test_an_empty_corpus_is_an_empty_index_not_an_error(monkeypatch):
    """Nothing ingested yet is a state to report, not a failure."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[DocChunk.__table__])
    session = sessionmaker(bind=engine)()
    monkeypatch.setattr(mcp_resources, "_session", lambda: _fixed(session))

    assert json.loads(sources_index()) == []


# --- documents and sections ----------------------------------------------------


def test_a_document_reads_back_whole_and_in_order(corpus):
    body = document_body("stac-spec-core")

    assert body.startswith("# STAC Overview")
    assert body.index("## Item fields") < body.index("## Media Type for STAC Item")


def test_a_section_reads_back_with_its_heading_first(corpus):
    """Headers stay in the content, so a section is readable markdown on its own."""
    body = section_body("stac-spec-core", "Item fields")

    assert body == "## Item fields\n\nThe id field is REQUIRED."


def test_a_section_name_with_a_space_is_used_as_given(corpus):
    """
    The SDK hands template parameters over already percent-decoded, so nothing here
    unquotes: doing it twice would corrupt any section name containing a literal '%'.
    """
    assert section_body("stac-spec-core", "Item fields").endswith("REQUIRED.")


def test_an_unknown_document_raises(corpus):
    with pytest.raises(ValueError, match="No document ingested"):
        document_body("nope")


def test_an_unknown_section_raises(corpus):
    with pytest.raises(ValueError, match="No section"):
        section_body("stac-spec-core", "Nope")


# --- the URI scheme ------------------------------------------------------------


def test_there_is_one_static_resource_and_two_templates():
    static = [uri for uri, *_ in RESOURCES if "{" not in uri]
    templates = [uri for uri, *_ in RESOURCES if "{" in uri]

    assert static == [SOURCES_URI]
    assert sorted(templates) == sorted([DOCUMENT_URI, SECTION_URI])


def test_the_authorities_are_distinct_so_no_template_shadows_the_index():
    """
    `docs://{source}` next to `docs://sources` would match the same URI, and which one wins
    is exactly the sort of thing that holds until an SDK version changes it.
    """
    authorities = {uri.removeprefix("docs://").split("/")[0] for uri, *_ in RESOURCES}

    assert authorities == {"sources", "document", "section"}
    assert len(authorities) == len(RESOURCES)


def test_the_markdown_resources_declare_their_type():
    """A client rendering a document should not have to sniff it."""
    for uri, _, mime, _ in RESOURCES:
        assert mime == ("application/json" if uri == SOURCES_URI else "text/markdown")


def test_every_resource_has_a_description():
    assert all(description.strip() for *_, description in RESOURCES)
