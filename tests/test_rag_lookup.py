"""Tests for the rag_lookup tool: fake retrieval, no database and no Bedrock."""

from inspect import Parameter, signature

import pytest

from app.db.models import DocChunk
from app.tools import rag_lookup as tool
from app.tools.rag_lookup import (
    DEFAULT_TOP_K,
    NO_MATCH,
    RAG_LOOKUP_TOOL,
    LookupResult,
    rag_lookup,
)


def chunk(content: str, source: str, section: str | None = None) -> DocChunk:
    """A detached DocChunk: never flushed, so no database is involved."""
    return DocChunk(content=content, source=source, section=section)


@pytest.fixture
def fake_retrieve(monkeypatch):
    """
    Patches retrieve_with_scores() and records the arguments the tool passes to it.

    Takes bare chunks and attaches plausible increasing distances, since almost every test
    here is about the context and the sources; the ones about scores pass their own.
    """

    def _install(chunks, distances=None):
        calls: list[dict] = []
        scored = list(zip(chunks, distances or [0.1 * (i + 1) for i in range(len(chunks))]))

        def _retrieve_with_scores(db, query, top_k=DEFAULT_TOP_K):
            calls.append({"db": db, "query": query, "top_k": top_k})
            return scored

        monkeypatch.setattr(tool, "retrieve_with_scores", _retrieve_with_scores)
        return calls

    return _install


def test_returns_formatted_context_and_sources(fake_retrieve):
    fake_retrieve([chunk("Items are GeoJSON.", "stac-spec.md", "Item")])
    result = rag_lookup(object(), "what is an item")

    assert "[Source: stac-spec.md - Item]" in result.context
    assert "Items are GeoJSON." in result.context
    assert result.sources == ["stac-spec.md"]


def test_the_result_is_a_model_not_a_dict(fake_retrieve):
    """Step 7's structured output: the halves are declared, not conventional."""
    fake_retrieve([chunk("Items are GeoJSON.", "stac-spec.md")])

    assert isinstance(rag_lookup(object(), "q"), LookupResult)


# --- retrieval quality (step 8) ---------------------------------------------


def test_the_distances_come_back_for_the_trace(fake_retrieve):
    """
    Step 8's third item. The ranking is a cosine_distance ORDER BY either way, so the
    scores were already being computed and thrown away.
    """
    fake_retrieve(
        [chunk("a", "stac-spec.md"), chunk("b", "api-spec.md")],
        distances=[0.13, 0.44],
    )

    result = rag_lookup(object(), "q")

    assert [round(d, 2) for _, d in result.scored] == [0.13, 0.44]
    assert [c.source for c, _ in result.scored] == ["stac-spec.md", "api-spec.md"]


def test_the_scores_do_not_leak_into_what_the_model_reads(fake_retrieve):
    """
    `context` is the only field that reaches the model, and a distance is not something
    it can reason about - the same split as the footprint geometry in stac_search.
    """
    fake_retrieve([chunk("Items are GeoJSON.", "stac-spec.md")], distances=[0.1234])

    result = rag_lookup(object(), "q")

    assert "0.1234" not in result.context
    assert "distance" not in result.context.lower()


def test_a_lookup_that_matched_nothing_has_no_scores(fake_retrieve):
    fake_retrieve([])

    assert rag_lookup(object(), "q").scored == []


def test_sources_are_deduped_and_sorted(fake_retrieve):
    fake_retrieve(
        [
            chunk("a", "b.md"),
            chunk("b", "a.md"),
            chunk("c", "b.md"),
        ]
    )

    assert rag_lookup(object(), "q").sources == ["a.md", "b.md"]


def test_retrieval_receives_the_query_and_the_default_top_k(fake_retrieve):
    calls = fake_retrieve([])
    db = object()
    rag_lookup(db, "required fields of an Item")

    assert calls == [{"db": db, "query": "required fields of an Item", "top_k": DEFAULT_TOP_K}]


def test_no_match_returns_a_readable_message_not_an_empty_string(fake_retrieve):
    """An empty tool_result tells the model nothing, and the API rejects empty content."""
    fake_retrieve([])
    result = rag_lookup(object(), "unrelated question")

    assert result.context == NO_MATCH
    assert result.sources == []


def test_tool_schema_stays_in_sync_with_the_function():
    params = signature(rag_lookup).parameters
    schema = RAG_LOOKUP_TOOL["input_schema"]

    assert set(schema["properties"]) <= set(params)

    # db is passed by the loop, not by the model, so it is not part of the schema.
    mandatory = {name for name, p in params.items() if p.default is Parameter.empty} - {"db"}
    assert set(schema["required"]) == mandatory


def test_top_k_is_not_exposed_to_the_model():
    """Retrieval tuning is ours, not something to improvise per question."""
    assert "top_k" not in RAG_LOOKUP_TOOL["input_schema"]["properties"]
