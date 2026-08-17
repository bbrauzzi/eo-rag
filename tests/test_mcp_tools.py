"""
Tests for the MCP tool adapters.

These run in the **default** dev environment, with the `mcp` extra absent, which is the
whole reason `app/mcp/tools.py` does not import the SDK. The drift guards in particular
have to run on every change, not only when someone remembers to install an extra.

The fakes return the real `SearchResult` / `IndexResult` models, per the rule the rest of
the suite follows: a fake of a shape the consumer could not actually accept hides the bug
rather than catching it.
"""

import json
from inspect import Parameter, signature
from typing import get_args

import pytest

from app.config import settings
from app.mcp import tools as mcp_tools
from app.mcp.tools import (
    TOOLS,
    IndexName,
    mcp_compute_index,
    mcp_rag_lookup,
    mcp_stac_search,
)
from app.tools.compute_index import (
    COMPUTE_INDEX_TOOL,
    INDICES,
    Bands,
    IndexResult,
    PixelCounts,
    Reflectance,
    Statistics,
)
from app.tools.rag_lookup import RAG_LOOKUP_TOOL, LookupResult
from app.tools.stac_search import STAC_SEARCH_TOOL, ItemSummary, SearchResult


def scene(item_id="S2B_33TTG_20240130_0_L2A", west=12.0) -> ItemSummary:
    return ItemSummary(
        id=item_id,
        collection="sentinel-2-l2a",
        datetime="2024-01-30T10:09:09Z",
        cloud_cover=1.5,
        platform="sentinel-2b",
        bbox=[west, 41.0, west + 1, 42.0],
        geometry={
            "type": "Polygon",
            "coordinates": [
                [[west, 41.0], [west + 1, 41.0], [west + 1, 42.0], [west, 42.0], [west, 41.0]]
            ],
        },
        asset_keys=["nir", "red", "thumbnail"],
        assets={"thumbnail": "https://example.test/thumb.jpg"},
    )


def index_result() -> IndexResult:
    return IndexResult(
        index="ndvi",
        bands=Bands(a="nir", b="red"),
        item_id="S2B_33TTG_20240130_0_L2A",
        collection="sentinel-2-l2a",
        datetime="2024-01-30T10:09:09Z",
        cloud_cover=1.5,
        bbox=[12.0, 41.0, 13.0, 42.0],
        crs="EPSG:32633",
        resolution_m=10.0,
        reflectance=Reflectance(scale=[1e-4, 1e-4], offset_declared=[0.0, 0.0], offset_applied=False),
        pixels=PixelCounts(read=400, valid=400, nodata_fraction=0.0),
        statistics=Statistics(mean=0.42, std=0.1, min=0.1, p10=0.2, median=0.42, p90=0.6, max=0.8),
    )


@pytest.fixture
def fake_stac(monkeypatch):
    """Replaces the underlying stac_search and records the arguments it received."""

    def _install(result=None):
        calls: list[dict] = []

        def _search(**kwargs):
            calls.append(kwargs)
            return result if result is not None else SearchResult(count=1, limit=10, items=[scene()])

        monkeypatch.setattr(mcp_tools, "stac_search", _search)
        return calls

    return _install


# --- schema drift: the adapters against the hand-written tool schemas ---------


def test_stac_search_exposes_the_same_arguments_as_the_anthropic_tool():
    """
    One tool, two front ends, and they must not disagree about what can be asked for.
    `include_geometry` is the single deliberate exception - see the test below.
    """
    adapter = set(signature(mcp_stac_search).parameters)
    anthropic = set(STAC_SEARCH_TOOL["input_schema"]["properties"])

    assert adapter - {"include_geometry"} == anthropic


def test_include_geometry_is_the_one_deliberate_addition():
    """
    Named here so it is a decision rather than drift, exactly as the existing suite names
    `asset_keys`, `top_k` and `db` as deliberate omissions.
    """
    assert "include_geometry" in signature(mcp_stac_search).parameters
    assert "include_geometry" not in STAC_SEARCH_TOOL["input_schema"]["properties"]


def test_asset_keys_stays_hidden_from_mcp_too():
    """A caller-side knob for compute_index's bands, not something a client should pick."""
    assert "asset_keys" not in signature(mcp_stac_search).parameters


def test_compute_index_exposes_exactly_the_anthropic_arguments():
    assert set(signature(mcp_compute_index).parameters) == set(
        COMPUTE_INDEX_TOOL["input_schema"]["properties"]
    )


def test_rag_lookup_exposes_only_the_query():
    """`db` is ours to supply and `top_k` is retrieval tuning, as in RAG_LOOKUP_TOOL."""
    assert set(signature(mcp_rag_lookup).parameters) == set(
        RAG_LOOKUP_TOOL["input_schema"]["properties"]
    ) == {"query"}


def test_the_required_arguments_match():
    """A default here and none there would let one front end refuse what the other allows."""
    for adapter, schema in (
        (mcp_stac_search, STAC_SEARCH_TOOL),
        (mcp_compute_index, COMPUTE_INDEX_TOOL),
        (mcp_rag_lookup, RAG_LOOKUP_TOOL),
    ):
        required = {
            name
            for name, p in signature(adapter).parameters.items()
            if p.default is Parameter.empty
        }
        assert required == set(schema["input_schema"]["required"]), adapter.__name__


def test_the_index_literal_is_the_same_set_as_INDICES():
    """
    The SDK derives the enum from the type, where COMPUTE_INDEX_TOOL writes it by hand.
    This is the one place a new index could be added to INDICES and not reach MCP.
    """
    assert set(get_args(IndexName)) == set(INDICES)


def test_the_tool_names_are_the_ones_claude_uses():
    """So a person reading a trace cannot tell which front end made the call, and needn't."""
    assert set(TOOLS) == {
        STAC_SEARCH_TOOL["name"],
        COMPUTE_INDEX_TOOL["name"],
        RAG_LOOKUP_TOOL["name"],
    }


def test_the_collection_allowlist_reaches_the_description():
    """
    The same reasoning as the Anthropic schema: rejecting `sentinel2` without saying what
    exists spends a round trip on something the client cannot guess.
    """
    from app.mcp.tools import _COLLECTIONS_HELP

    assert all(name in _COLLECTIONS_HELP for name in settings.allowed_collections)


# --- what the client actually gets back ---------------------------------------


def test_footprints_are_stripped_by_default(fake_stac):
    """
    The SDK puts a returned model into both `structured_content` and the model-visible
    text, so this is one choice rather than two: the coordinates would cost every caller
    context for the benefit of the few that draw maps.
    """
    fake_stac()

    result = mcp_stac_search(bbox=[12.0, 41.0, 13.0, 42.0])

    assert result.items[0].geometry is None
    assert "coordinates" not in result.model_dump_json()
    # Everything a caller reasons about survives.
    assert result.items[0].id == "S2B_33TTG_20240130_0_L2A"
    assert result.items[0].bbox == [12.0, 41.0, 13.0, 42.0]


def test_footprints_come_back_when_asked_for(fake_stac):
    fake_stac()

    result = mcp_stac_search(bbox=[12.0, 41.0, 13.0, 42.0], include_geometry=True)

    assert result.items[0].geometry["type"] == "Polygon"
    assert "coordinates" in result.model_dump_json()


def test_the_projection_is_much_smaller(fake_stac):
    """The measurement the projection exists for, in miniature."""
    fake_stac(SearchResult(count=2, limit=10, items=[scene(), scene("S2A_x", west=13.0)]))
    without = len(mcp_stac_search(bbox=[12.0, 41.0, 14.0, 42.0]).model_dump_json())

    fake_stac(SearchResult(count=2, limit=10, items=[scene(), scene("S2A_x", west=13.0)]))
    with_geometry = len(
        mcp_stac_search(bbox=[12.0, 41.0, 14.0, 42.0], include_geometry=True).model_dump_json()
    )

    assert without < with_geometry


def test_the_result_is_still_a_search_result(fake_stac):
    """It is the declared output schema; the round trip through model_view must preserve it."""
    fake_stac()

    assert isinstance(mcp_stac_search(bbox=[12.0, 41.0, 13.0, 42.0]), SearchResult)


def test_arguments_reach_the_underlying_tool_unchanged(fake_stac):
    """The adapter adds types and descriptions, not behaviour."""
    calls = fake_stac()

    mcp_stac_search(
        bbox=[12.0, 41.0, 13.0, 42.0],
        datetime="2024-01-01/2024-01-31",
        collections=["sentinel-2-l2a"],
        limit=3,
        max_cloud_cover=20,
    )

    assert calls[0] == {
        "bbox": [12.0, 41.0, 13.0, 42.0],
        "datetime": "2024-01-01/2024-01-31",
        "collections": ["sentinel-2-l2a"],
        "limit": 3,
        "max_cloud_cover": 20,
    }


def test_include_geometry_is_not_passed_to_the_catalog(fake_stac):
    """It is a projection choice made here; `stac_search` has never heard of it."""
    calls = fake_stac()

    mcp_stac_search(bbox=[12.0, 41.0, 13.0, 42.0], include_geometry=True)

    assert "include_geometry" not in calls[0]


def test_compute_index_passes_through_and_returns_the_model(monkeypatch):
    calls: list[dict] = []

    def _compute(**kwargs):
        calls.append(kwargs)
        return index_result()

    monkeypatch.setattr(mcp_tools, "compute_index", _compute)

    result = mcp_compute_index(item_id="S2B_x", bbox=[12.0, 41.0, 13.0, 42.0], index="ndwi")

    assert calls[0] == {"item_id": "S2B_x", "bbox": [12.0, 41.0, 13.0, 42.0], "index": "ndwi"}
    assert isinstance(result, IndexResult)
    # Every field is JSON-safe, which is what makes the output schema free.
    json.loads(result.model_dump_json())


def test_a_failing_tool_raises_rather_than_returning_an_apology(monkeypatch):
    """
    The SDK turns an exception into an MCP tool error carrying the message. Swallowing it
    here would turn a bad bbox into a successful call that answered nothing.
    """

    def _boom(**kwargs):
        raise ValueError("bbox south (42.0) must be below north (41.0)")

    monkeypatch.setattr(mcp_tools, "stac_search", _boom)

    with pytest.raises(ValueError, match="below north"):
        mcp_stac_search(bbox=[12.0, 42.0, 13.0, 41.0])


# --- rag_lookup and its session ------------------------------------------------


class RecordingSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_db(monkeypatch):
    """Installs a recording session in place of SessionLocal and returns it."""
    session = RecordingSession()
    monkeypatch.setattr(mcp_tools, "SessionLocal", lambda: session)
    return session


def test_rag_lookup_returns_the_passages_as_prose(monkeypatch, fake_db):
    """
    A `str`, not the model: `LookupResult.scored` holds SQLAlchemy rows that pydantic
    cannot schema, and the passages already carry their own labels - JSON would only add
    escaping to text meant to be read.
    """
    monkeypatch.setattr(
        mcp_tools,
        "rag_lookup",
        lambda db, query: LookupResult(
            context="[Source: stac-spec-core - Item fields]\nThe id field is REQUIRED.",
            sources=["stac-spec-core"],
        ),
    )

    result = mcp_rag_lookup(query="required fields")

    assert isinstance(result, str)
    assert "[Source: stac-spec-core - Item fields]" in result


def test_the_retrieval_scores_do_not_reach_the_client(monkeypatch, fake_db):
    """The same split the agent keeps: distances are telemetry, not an answer."""
    monkeypatch.setattr(
        mcp_tools,
        "rag_lookup",
        lambda db, query: LookupResult(context="passage text", sources=["a"], scored=[]),
    )

    result = mcp_rag_lookup(query="q")

    assert "distance" not in result.lower()
    assert result == "passage text"


def test_the_session_is_closed_after_a_lookup(monkeypatch, fake_db):
    monkeypatch.setattr(mcp_tools, "rag_lookup", lambda db, query: LookupResult(context="c"))

    mcp_rag_lookup(query="q")

    assert fake_db.closed is True


def test_the_session_is_closed_even_when_the_lookup_fails(monkeypatch, fake_db):
    """A leaked session per failed call would exhaust the pool and look like a hang."""

    def _boom(db, query):
        raise RuntimeError("Bedrock call failed")

    monkeypatch.setattr(mcp_tools, "rag_lookup", _boom)

    with pytest.raises(RuntimeError):
        mcp_rag_lookup(query="q")

    assert fake_db.closed is True


def test_the_query_reaches_the_underlying_tool(monkeypatch, fake_db):
    calls: list[str] = []

    def _lookup(db, query):
        calls.append(query)
        return LookupResult(context="c")

    monkeypatch.setattr(mcp_tools, "rag_lookup", _lookup)

    mcp_rag_lookup(query="required fields of an Item")

    assert calls == ["required fields of an Item"]


# --- import purity -------------------------------------------------------------


def test_importing_the_adapters_opens_no_connection(monkeypatch):
    """
    Same bar as every other outward-talking module here. `SessionLocal` is called per tool
    call, never at import.
    """
    import importlib

    import httpx

    def boom(*args, **kwargs):
        raise AssertionError("a client was built at import time")

    monkeypatch.setattr(httpx, "Client", boom)

    importlib.reload(mcp_tools)
