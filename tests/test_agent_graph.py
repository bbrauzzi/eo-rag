"""
Tests for the LangGraph orchestration: scripted Anthropic responses and fake tools.

No network, no database, no credentials. The graph is driven entirely by the sequence
of replies the fake client hands back, so every branch - single turn, tool round trip,
several tools in one turn, tool failure, step cap, resumed conversation - is reachable
from here.

Most of these were written against the hand-rolled loop of step 3b and carried over
unchanged: they are the contract the port had to preserve.
"""

import importlib
import json
import logging
from types import SimpleNamespace

import pytest
from anthropic.types import TextBlock, ToolUseBlock, Usage

from app.agents import cost, graph
from app.config import settings
from app.db.models import DocChunk
from app.obs.tracing import Turn
from app.tools.compute_index import (
    Bands,
    IndexResult,
    PixelCounts,
    Reflectance,
    Statistics,
)
from app.tools.rag_lookup import LookupResult
from app.tools.stac_search import ItemSummary, SearchResult

# The real SDK block types rather than stand-ins: the graph calls model_dump() on them
# before putting them in the state, and the checkpointer then has to serialize the
# result. A hand-rolled fake would hide both of those. The same argument now covers the
# tools: their fakes return the same pydantic models the real ones do, so a result the
# graph could not actually consume fails here rather than only in production.

# What one scripted reply is charged. Nothing asserts on this figure except the budget
# tests, which set the cap relative to it.
REPLY_TOKENS = Usage(input_tokens=1000, output_tokens=500)


def text(content: str) -> TextBlock:
    return TextBlock(type="text", text=content)


def tool_use(name: str, tool_input: dict | None = None, id: str = "tu_1") -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", name=name, input=tool_input or {}, id=id)


def reply(*blocks, usage: Usage = REPLY_TOKENS) -> SimpleNamespace:
    """
    One scripted response. Carries a real `Usage`, because the agent node prices every
    call from it - a fake without one would make the cost cap untestable and unexercised.
    """
    return SimpleNamespace(content=list(blocks), usage=usage)


class FakeStream:
    """
    The streaming context manager the SDK returns, over one scripted reply.

    `text_stream` hands the text out in fragments rather than whole, so the token path
    is really exercised: a test that saw one delta per block would not notice the graph
    dropping every delta but the last.
    """

    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    CHUNK = 8

    @property
    def text_stream(self):
        for block in self.response.content:
            if isinstance(block, TextBlock):
                for i in range(0, len(block.text), self.CHUNK):
                    yield block.text[i : i + self.CHUNK]

    def get_final_message(self):
        return self.response


class FakeMessages:
    """Replays a scripted list of responses and records every stream() call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("the graph asked for more replies than the test scripted")
        return FakeStream(self.responses.pop(0))


@pytest.fixture(autouse=True)
def fresh_graph(monkeypatch):
    """Recompile per test, so the in-memory checkpointer never leaks between them."""
    monkeypatch.setattr(graph, "_cached_graph", None)


@pytest.fixture
def fake_llm(monkeypatch):
    """Installs a scripted Anthropic client in place of the cached one."""

    def _install(*responses):
        messages = FakeMessages(responses)
        monkeypatch.setattr(graph, "_client", lambda: SimpleNamespace(messages=messages))
        return messages

    return _install


@pytest.fixture
def fake_tools(monkeypatch):
    """Replaces all three tools with recorders; each returns a canned result or raises."""

    def _install(
        rag_result=None,
        stac_result=None,
        index_result=None,
        rag_error=None,
        stac_error=None,
        index_error=None,
    ):
        calls: list[dict] = []

        def _rag(db, **kwargs):
            calls.append({"tool": "rag_lookup", "db": db, **kwargs})
            if rag_error is not None:
                raise rag_error
            return rag_result or LookupResult(
                context="Some documentation.", sources=["stac-spec.md"]
            )

        def _stac(**kwargs):
            calls.append({"tool": "stac_search", **kwargs})
            if stac_error is not None:
                raise stac_error
            if stac_result is not None:
                return stac_result
            return SearchResult(count=0, limit=10, items=[])

        def _index(**kwargs):
            calls.append({"tool": "compute_index", **kwargs})
            if index_error is not None:
                raise index_error
            return index_result or IndexResult(
                index="ndvi",
                bands=Bands(a="nir", b="red"),
                item_id="S2B_test_L2A",
                collection="sentinel-2-l2a",
                datetime="2024-01-30T10:07:19Z",
                # The graph turns this into the AOI drawn on the map, so the fake has
                # to carry it: the real tool always does.
                bbox=[12.0, 41.0, 13.0, 42.0],
                crs="EPSG:32633",
                resolution_m=10.0,
                reflectance=Reflectance(
                    scale=[0.0001, 0.0001], offset_declared=[0.0, 0.0], offset_applied=False
                ),
                pixels=PixelCounts(read=400, valid=400, nodata_fraction=0.0),
                statistics=Statistics(
                    mean=0.42, std=0.1, min=0.1, p10=0.2, median=0.42, p90=0.6, max=0.8
                ),
            )

        monkeypatch.setattr(graph, "rag_lookup", _rag)
        monkeypatch.setattr(graph, "stac_search", _stac)
        monkeypatch.setattr(graph, "compute_index", _index)
        return calls

    return _install


# --- the simple path --------------------------------------------------------


def test_answer_without_tools_costs_one_step(fake_llm, fake_tools):
    fake_tools()
    fake_llm(reply(text("Plain answer.")))

    result = graph.answer_question(object(), "hello")

    assert result.text == "Plain answer."
    assert result.sources == []
    assert result.steps == 1


def test_text_blocks_are_concatenated(fake_llm, fake_tools):
    fake_tools()
    fake_llm(reply(text("First part. "), text("Second part.")))

    assert graph.answer_question(object(), "q").text == "First part. Second part."


def test_every_call_carries_the_system_prompt_and_the_question(fake_llm, fake_tools):
    fake_tools()
    messages = fake_llm(reply(text("ok")))

    graph.answer_question(object(), "What are STAC Items?")

    call = messages.calls[0]
    assert call["system"] == graph.SYSTEM_PROMPT
    assert call["model"] == settings.claude_model
    assert call["max_tokens"] == graph.MAX_TOKENS
    assert call["messages"] == [{"role": "user", "content": "What are STAC Items?"}]


def test_all_tools_are_offered(fake_llm, fake_tools):
    fake_tools()
    messages = fake_llm(reply(text("ok")))

    graph.answer_question(object(), "q")

    assert [t["name"] for t in messages.calls[0]["tools"]] == [
        "rag_lookup",
        "stac_search",
        "compute_index",
    ]


# --- one tool round trip ----------------------------------------------------


def test_tool_result_is_fed_back_and_the_answer_comes_from_the_second_call(fake_llm, fake_tools):
    calls = fake_tools()
    messages = fake_llm(
        reply(tool_use("rag_lookup", {"query": "items"})),
        reply(text("Grounded answer.")),
    )
    db = object()

    result = graph.answer_question(db, "What are STAC Items?")

    assert result.text == "Grounded answer."
    assert result.steps == 2
    assert calls == [{"tool": "rag_lookup", "db": db, "query": "items"}]

    # Second call: original question, the assistant turn, then the tool_result.
    sent = messages.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    tool_result = sent[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "tu_1"
    assert tool_result["content"] == "Some documentation."
    assert tool_result["is_error"] is False


def test_the_session_reaches_the_tool_through_the_context_not_the_state(fake_llm, fake_tools):
    """A SQLAlchemy Session has no business being checkpointed."""
    calls = fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "q"})), reply(text("a")))
    db = object()

    graph.answer_question(db, "q")

    assert calls[0]["db"] is db


def test_rag_sources_reach_the_answer(fake_llm, fake_tools):
    fake_tools(rag_result=LookupResult(context="ctx", sources=["stac-spec.md", "api-spec.md"]))
    fake_llm(reply(tool_use("rag_lookup", {"query": "q"})), reply(text("a")))

    assert graph.answer_question(object(), "q").sources == ["api-spec.md", "stac-spec.md"]


def test_stac_search_is_cited_as_the_catalog_it_came_from(fake_llm, fake_tools):
    fake_tools(stac_result=search(scene("S2B_x")))
    messages = fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("One scene.")),
    )

    result = graph.answer_question(object(), "which scenes?")

    assert result.sources == [settings.stac_api_url]
    # The catalog result reaches the model as JSON, not as a repr.
    payload = json.loads(messages.calls[1]["messages"][2]["content"][0]["content"])
    assert payload["items"][0]["id"] == "S2B_x"


def test_compute_index_is_cited_with_the_scene_it_measured(fake_llm, fake_tools):
    """The pixels are the source here, and the item id is what makes it reproducible."""
    calls = fake_tools()
    messages = fake_llm(
        reply(tool_use("compute_index", {"item_id": "S2B_test_L2A", "bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("Mean NDVI is 0.42.")),
    )

    result = graph.answer_question(object(), "how green is it?")

    assert calls[0]["tool"] == "compute_index"
    assert calls[0]["item_id"] == "S2B_test_L2A"
    assert result.sources == [f"{settings.stac_api_url} (S2B_test_L2A)"]
    payload = json.loads(messages.calls[1]["messages"][2]["content"][0]["content"])
    assert payload["statistics"]["mean"] == 0.42


def test_the_three_tools_can_chain_across_turns(fake_llm, fake_tools):
    """search then measure: the pattern compute_index was designed around."""
    calls = fake_tools()
    fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_1")),
        reply(tool_use("compute_index", {"item_id": "S2B_test_L2A", "bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_2")),
        reply(text("Found the scene and measured it.")),
    )

    result = graph.answer_question(object(), "how green is Rome?")

    assert [c["tool"] for c in calls] == ["stac_search", "compute_index"]
    assert result.steps == 3
    assert result.sources == sorted(
        {settings.stac_api_url, f"{settings.stac_api_url} (S2B_test_L2A)"}
    )


def test_several_tools_in_one_turn_produce_one_result_each(fake_llm, fake_tools):
    calls = fake_tools()
    messages = fake_llm(
        reply(
            tool_use("rag_lookup", {"query": "items"}, id="tu_1"),
            tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_2"),
        ),
        reply(text("Both.")),
    )

    result = graph.answer_question(object(), "q")

    assert [c["tool"] for c in calls] == ["rag_lookup", "stac_search"]
    results = messages.calls[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert result.sources == sorted({"stac-spec.md", settings.stac_api_url})


def test_text_alongside_a_tool_use_does_not_end_the_turn(fake_llm, fake_tools):
    """A turn can narrate and call a tool at once; the tool still runs."""
    calls = fake_tools()
    fake_llm(
        reply(text("Let me check. "), tool_use("rag_lookup", {"query": "q"})),
        reply(text("Final.")),
    )

    result = graph.answer_question(object(), "q")

    assert len(calls) == 1
    assert result.text == "Final."


# --- failing tools ----------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ValueError("bbox south (42.0) must be below north (41.0)"),
        RuntimeError("STAC API unreachable"),
        TypeError("unexpected keyword argument 'when'"),
    ],
)
def test_tool_failure_goes_back_to_the_model_instead_of_raising(fake_llm, fake_tools, error):
    fake_tools(stac_error=error)
    messages = fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 42.0, 13.0, 41.0]})),
        reply(text("That bounding box is invalid.")),
    )

    result = graph.answer_question(object(), "q")

    tool_result = messages.calls[1]["messages"][2]["content"][0]
    assert tool_result["is_error"] is True
    assert str(error) in tool_result["content"]
    assert result.text == "That bounding box is invalid."


def test_a_failed_tool_contributes_no_sources(fake_llm, fake_tools):
    fake_tools(rag_error=RuntimeError("Bedrock call failed"))
    fake_llm(reply(tool_use("rag_lookup", {"query": "q"})), reply(text("Could not search.")))

    assert graph.answer_question(object(), "q").sources == []


def test_an_unknown_tool_is_reported_to_the_model(fake_llm, fake_tools):
    fake_tools()
    messages = fake_llm(reply(tool_use("classify_landcover", {})), reply(text("Not available.")))

    graph.answer_question(object(), "q")

    tool_result = messages.calls[1]["messages"][2]["content"][0]
    assert tool_result["is_error"] is True
    assert "classify_landcover" in tool_result["content"]


# --- footprints -------------------------------------------------------------


def scene(item_id: str, west: float = 12.0) -> ItemSummary:
    """One summarized item, in the shape _summarize_item returns."""
    return ItemSummary(
        id=item_id,
        collection="sentinel-2-l2a",
        datetime="2024-01-30T10:07:19Z",
        cloud_cover=4.2,
        platform="sentinel-2b",
        bbox=[west, 41.0, west + 1, 42.0],
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [west, 41.0],
                    [west + 1, 41.0],
                    [west + 1, 42.0],
                    [west, 42.0],
                    [west, 41.0],
                ]
            ],
        },
        asset_keys=["nir", "red", "thumbnail"],
        assets={"thumbnail": "https://example.test/thumb.jpg"},
    )


def search(*items: ItemSummary) -> SearchResult:
    return SearchResult(count=len(items), limit=10, items=list(items))


def test_footprints_are_collected_but_never_reach_the_model(fake_llm, fake_tools):
    """
    The whole point of the split: the map gets the polygon, the model gets the bbox.
    Coordinates are something the model cannot reason about and pays context for.
    """
    fake_tools(stac_result=search(scene("S2A_one"), scene("S2A_two", west=13.0)))
    messages = fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 14.0, 42.0]})),
        reply(text("Two scenes.")),
    )

    result = graph.answer_question(object(), "q")

    sent = messages.calls[1]["messages"][2]["content"][0]["content"]
    assert "geometry" not in sent
    assert "coordinates" not in sent
    assert "S2A_one" in sent

    assert [f["id"] for f in result.features] == ["S2A_one", "S2A_two"]
    assert result.features[0]["geometry"]["type"] == "Polygon"
    assert result.features[0]["properties"]["kind"] == "footprint"


def test_the_same_scene_twice_in_one_turn_is_one_footprint(fake_llm, fake_tools):
    """Two searches over neighbouring areas routinely return the same scene."""
    fake_tools(stac_result=search(scene("S2A_one")))
    fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_1")),
        reply(tool_use("stac_search", {"bbox": [12.1, 41.1, 13.1, 42.1]}, id="tu_2")),
        reply(text("One scene covers both.")),
    )

    result = graph.answer_question(object(), "q")

    assert [f["id"] for f in result.features] == ["S2A_one"]


def test_a_failed_search_contributes_no_footprints(fake_llm, fake_tools):
    fake_tools(stac_error=RuntimeError("STAC API unreachable"))
    fake_llm(reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})), reply(text("no")))

    assert graph.answer_question(object(), "q").features == []


def test_compute_index_contributes_its_area_of_interest(fake_llm, fake_tools):
    """A few km inside a 110 km tile: worth drawing apart from the scene it sits on."""
    fake_tools()
    fake_llm(
        reply(tool_use("compute_index", {"item_id": "S2B_test_L2A", "bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("Mean NDVI 0.42.")),
    )

    feature = graph.answer_question(object(), "q").features[0]

    assert feature["properties"]["kind"] == "aoi"
    assert feature["properties"]["index"] == "ndvi"
    assert feature["geometry"]["type"] == "Polygon"

    # Dumped to plain data on the way out: this Feature is JSON-serialized for the map,
    # and a pydantic object left in here would fail at that point rather than this one.
    statistics = feature["properties"]["statistics"]
    assert statistics["mean"] == 0.42
    assert isinstance(statistics, dict)
    json.dumps(feature)


def test_a_documentation_lookup_contributes_no_footprints(fake_llm, fake_tools):
    fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "q"})), reply(text("Grounded.")))

    assert graph.answer_question(object(), "q").features == []


# --- the step cap -----------------------------------------------------------


def test_the_graph_stops_calling_tools_at_max_agent_steps(fake_llm, fake_tools, monkeypatch):
    monkeypatch.setattr(settings, "max_agent_steps", 2)
    calls = fake_tools()
    fake_llm(
        reply(tool_use("rag_lookup", {"query": "1"})),
        reply(tool_use("rag_lookup", {"query": "2"})),
        reply(text("Concluding with what I have.")),
    )

    result = graph.answer_question(object(), "q")

    assert len(calls) == 2, "no tool may run after the cap"
    assert result.text == "Concluding with what I have."
    assert result.steps == 3


def test_the_final_call_offers_no_tools(fake_llm, fake_tools, monkeypatch):
    """Hard cap: the model cannot ask for another tool because it is not given any."""
    monkeypatch.setattr(settings, "max_agent_steps", 1)
    fake_tools()
    messages = fake_llm(
        reply(tool_use("rag_lookup", {"query": "1"})),
        reply(text("Done.")),
    )

    graph.answer_question(object(), "q")

    assert "tools" in messages.calls[0]
    assert "tools" not in messages.calls[1]


def test_sources_gathered_before_the_cap_are_still_cited(fake_llm, fake_tools, monkeypatch):
    monkeypatch.setattr(settings, "max_agent_steps", 1)
    fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "1"})), reply(text("Done.")))

    assert graph.answer_question(object(), "q").sources == ["stac-spec.md"]


def test_a_cap_of_zero_answers_without_ever_offering_tools(fake_llm, fake_tools, monkeypatch):
    monkeypatch.setattr(settings, "max_agent_steps", 0)
    calls = fake_tools()
    messages = fake_llm(reply(text("No tools for me.")))

    result = graph.answer_question(object(), "q")

    assert calls == []
    assert "tools" not in messages.calls[0]
    assert result.text == "No tools for me."
    assert result.steps == 1


# --- the conversation budget ------------------------------------------------
#
# The step cap above bounds one turn. These bound the thread, which nothing did before:
# a conversation could be continued indefinitely, each turn resending a growing history,
# and the step cap would dutifully permit every one of them.


@pytest.fixture
def budget(monkeypatch):
    """Sets both conversation limits; either at 0 turns that check off."""

    def _set(turns: int = 0, usd: float = 0.0):
        monkeypatch.setattr(settings, "max_conversation_turns", turns)
        monkeypatch.setattr(settings, "max_conversation_cost_usd", usd)

    return _set


def test_a_conversation_stops_after_its_turn_limit(fake_llm, fake_tools, budget):
    budget(turns=2)
    fake_tools()
    fake_llm(reply(text("one")), reply(text("two")))

    first = graph.answer_question(object(), "q1")
    graph.answer_question(object(), "q2", first.conversation_id)

    with pytest.raises(graph.ConversationBudgetExceeded, match="2 turns"):
        graph.answer_question(object(), "q3", first.conversation_id)


def test_the_turn_limit_is_per_conversation_not_global(fake_llm, fake_tools, budget):
    """The whole point of keying it to the thread: another user is not affected."""
    budget(turns=1)
    fake_tools()
    fake_llm(reply(text("one")), reply(text("elsewhere")))

    spent = graph.answer_question(object(), "q1")

    with pytest.raises(graph.ConversationBudgetExceeded):
        graph.answer_question(object(), "q2", spent.conversation_id)

    # A fresh thread has spent nothing and is served normally.
    assert graph.answer_question(object(), "q").text == "elsewhere"


def test_a_conversation_stops_after_its_cost_limit(fake_llm, fake_tools, budget):
    """
    The cap is crossed rather than respected exactly: the turn that exceeds it runs to
    completion and the *next* one is refused. Checking after the fact would be no limit
    at all - the tokens are already bought by then.
    """
    one_turn = cost.turn_cost_usd(settings.claude_model, REPLY_TOKENS)
    budget(usd=one_turn * 1.5)
    fake_tools()
    fake_llm(reply(text("one")), reply(text("two")))

    first = graph.answer_question(object(), "q1")
    # Still under: one turn spent against a cap of one and a half.
    second = graph.answer_question(object(), "q2", first.conversation_id)

    assert second.text == "two"
    with pytest.raises(graph.ConversationBudgetExceeded, match="budget"):
        graph.answer_question(object(), "q3", first.conversation_id)


def test_the_cost_of_a_turn_accumulates_over_every_model_call_it_made(fake_llm, fake_tools, budget):
    """A turn that calls a tool pays for two model calls, not one."""
    budget()
    fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "q"})), reply(text("Grounded.")))

    graph.answer_question(object(), "q", "c1")

    spent = graph._graph().get_state(graph._turn_config("c1")).values["cost_usd"]
    assert spent == pytest.approx(2 * cost.turn_cost_usd(settings.claude_model, REPLY_TOKENS))


def test_spend_accumulates_across_turns_unlike_sources_and_steps(fake_llm, fake_tools, budget):
    budget()
    fake_tools()
    fake_llm(reply(text("one")), reply(text("two")))

    first = graph.answer_question(object(), "q1")
    graph.answer_question(object(), "q2", first.conversation_id)

    turns, spent = graph._spent(graph._turn_config(first.conversation_id))
    assert turns == 2
    assert spent == pytest.approx(2 * cost.turn_cost_usd(settings.claude_model, REPLY_TOKENS))


def test_either_limit_at_zero_turns_that_check_off(fake_llm, fake_tools, budget):
    """What a deployment that does not want the cap sets, and the default for tests."""
    budget(turns=0, usd=0.0)
    fake_tools()
    fake_llm(*[reply(text("still going")) for _ in range(5)])

    conversation = graph.answer_question(object(), "q1").conversation_id
    for _ in range(4):
        graph.answer_question(object(), "again", conversation)

    turns, _ = graph._spent(graph._turn_config(conversation))
    assert turns == 5


def test_the_refusal_costs_nothing_because_the_model_is_never_called(
    fake_llm, fake_tools, budget
):
    """Checked before the turn, so a refused turn spends no tokens and adds no history."""
    budget(turns=1)
    fake_tools()
    # Exactly one reply is scripted: asking for a second would raise from FakeMessages.
    messages = fake_llm(reply(text("one")))

    first = graph.answer_question(object(), "q1")
    before = list(history(first.conversation_id))

    with pytest.raises(graph.ConversationBudgetExceeded):
        graph.answer_question(object(), "q2", first.conversation_id)

    assert len(messages.calls) == 1
    assert history(first.conversation_id) == before


def test_a_brand_new_conversation_is_never_over_budget(budget):
    """`check_conversation_budget` is what the streaming route calls before its 200."""
    budget(turns=1, usd=0.01)

    graph.check_conversation_budget(None)
    graph.check_conversation_budget("never-seen-before")


def test_the_streaming_path_refuses_before_it_yields_anything(fake_llm, fake_tools, budget):
    """
    It must raise rather than yield a `start` frame first: the route turns this into a
    429, and it can only do that while no part of the response has been sent.
    """
    budget(turns=1)
    fake_tools()
    fake_llm(reply(text("one")))
    first = graph.answer_question(object(), "q1")

    with pytest.raises(graph.ConversationBudgetExceeded):
        next(graph.stream_answer(object(), "q2", first.conversation_id))


def test_both_entry_points_enforce_the_same_budget(fake_llm, fake_tools, budget):
    """One graph, one checkpointer: turns taken on /ask count against /ask/stream."""
    budget(turns=1)
    fake_tools()
    fake_llm(reply(text("one")))

    spent = graph.answer_question(object(), "q1")

    with pytest.raises(graph.ConversationBudgetExceeded):
        graph.check_conversation_budget(spent.conversation_id)


# --- tracing (step 8) --------------------------------------------------------
#
# The point of the step: `/ask` produced no record of anything at all, because
# `get_stream_writer()` goes nowhere under `.invoke()`. Both paths now trace.


@pytest.fixture
def traced(caplog):
    """Reads back the JSON lines the turn wrote to the eo_rag.trace logger."""
    caplog.set_level(logging.INFO, logger="eo_rag.trace")

    def events(kind=None):
        parsed = [
            json.loads(r.message) for r in caplog.records if r.name == "eo_rag.trace"
        ]
        return [e for e in parsed if kind is None or e["event"] == kind]

    return events


def test_the_blocking_path_records_the_turn_it_used_to_lose(fake_llm, fake_tools, traced):
    fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "items"})), reply(text("Grounded.")))

    graph.answer_question(object(), "What are STAC Items?", "c1")

    assert [e["event"] for e in traced()] == [
        "turn_start",
        "generation",
        "retrieval",
        "tool",
        "generation",
        "turn_end",
    ]


def test_the_trace_names_the_tools_that_ran(fake_llm, fake_tools, traced):
    """Which is what the roadmap said nothing outside `sources` could tell you."""
    fake_tools(stac_result=search(scene("S2A_one")))
    fake_llm(
        reply(tool_use("rag_lookup", {"query": "q"}, id="tu_1")),
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_2")),
        reply(text("Both.")),
    )

    graph.answer_question(object(), "q")

    assert traced("turn_end")[0]["tools"] == ["rag_lookup", "stac_search"]
    assert [e["name"] for e in traced("tool")] == ["rag_lookup", "stac_search"]


def test_the_trace_accounts_for_every_model_call_of_the_turn(fake_llm, fake_tools, traced):
    """Three model calls in a two-tool turn, and the totals are the sum of the three."""
    fake_tools()
    fake_llm(
        reply(tool_use("rag_lookup", {"query": "a"}, id="tu_1")),
        reply(tool_use("rag_lookup", {"query": "b"}, id="tu_2")),
        reply(text("Done.")),
    )

    graph.answer_question(object(), "q")

    generations = traced("generation")
    assert len(generations) == 3
    assert all(g["input_tokens"] == REPLY_TOKENS.input_tokens for g in generations)

    end = traced("turn_end")[0]
    assert end["input_tokens"] == 3 * REPLY_TOKENS.input_tokens
    assert end["cost_usd"] == pytest.approx(
        3 * cost.turn_cost_usd(settings.claude_model, REPLY_TOKENS)
    )


def test_a_failing_tool_is_recorded_as_failed(fake_llm, fake_tools, traced):
    fake_tools(stac_error=RuntimeError("STAC API unreachable"))
    fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("The catalog is unreachable.")),
    )

    graph.answer_question(object(), "q")

    recorded = traced("tool")[0]
    assert recorded["ok"] is False
    assert recorded["error"] == "STAC API unreachable"


def test_retrieval_distances_reach_the_trace(fake_llm, fake_tools, traced):
    """Step 8's third item, end to end: the scores rag_lookup keeps land in the record."""
    fake_tools(
        rag_result=LookupResult(
            context="ctx",
            sources=["stac-spec.md"],
            # A real DocChunk, detached and never flushed: the model declares the type,
            # and a stand-in here would only prove the stand-in validates.
            scored=[(DocChunk(content="c", source="stac-spec.md", section="Item"), 0.17)],
        )
    )
    fake_llm(reply(tool_use("rag_lookup", {"query": "items"})), reply(text("Grounded.")))

    graph.answer_question(object(), "q")

    recorded = traced("retrieval")[0]
    assert recorded["query"] == "items"
    assert recorded["chunks"] == 1
    assert recorded["best"] == pytest.approx(0.17)


def test_the_streaming_path_traces_the_same_turn(fake_llm, fake_tools, traced):
    fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "q"})), reply(text("Grounded.")))

    stream(conversation_id="c1")

    assert traced("turn_end")[0]["tools"] == ["rag_lookup"]
    assert traced("turn_end")[0]["steps"] == 2


def test_an_abandoned_stream_still_closes_its_trace(fake_llm, fake_tools, traced):
    """
    A client that stops listening is the case worth recording, not the case to lose. The
    `finally` also closes the Langfuse span that would otherwise be left open.
    """
    fake_tools()
    fake_llm(
        reply(text("Let me check. "), tool_use("rag_lookup", {"query": "q"})),
        reply(text("Grounded.")),
    )

    events = graph.stream_answer(object(), "q", "c1")
    next(events)  # the `start` frame, before anything has run
    events.close()

    end = traced("turn_end")
    assert len(end) == 1
    # It stopped before an answer existed, and the record says so rather than inventing one.
    assert end[0]["steps"] == 0


def test_the_conversation_id_ties_the_records_together(fake_llm, fake_tools, traced):
    fake_tools()
    fake_llm(reply(text("a")))

    graph.answer_question(object(), "q", "the-thread")

    assert {e["conversation_id"] for e in traced()} == {"the-thread"}


# --- conversational memory --------------------------------------------------


def test_a_new_conversation_gets_an_id(fake_llm, fake_tools):
    fake_tools()
    fake_llm(reply(text("a")))

    assert graph.answer_question(object(), "q").conversation_id


def test_a_second_turn_sees_the_history_of_the_first(fake_llm, fake_tools):
    fake_tools()
    messages = fake_llm(reply(text("Items are Features.")), reply(text("Collections group them.")))

    first = graph.answer_question(object(), "What are Items?")
    second = graph.answer_question(object(), "And Collections?", first.conversation_id)

    assert second.conversation_id == first.conversation_id
    sent = messages.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "What are Items?"
    assert sent[2]["content"] == "And Collections?"


def test_tool_results_stay_in_the_history_of_the_conversation(fake_llm, fake_tools):
    fake_tools()
    messages = fake_llm(
        reply(tool_use("rag_lookup", {"query": "items"})),
        reply(text("Items are Features.")),
        reply(text("Yes, exactly.")),
    )

    first = graph.answer_question(object(), "What are Items?")
    graph.answer_question(object(), "Really?", first.conversation_id)

    sent = messages.calls[2]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user", "assistant", "user"]
    assert sent[2]["content"][0]["type"] == "tool_result"


def test_separate_conversations_do_not_share_history(fake_llm, fake_tools):
    fake_tools()
    messages = fake_llm(reply(text("first")), reply(text("second")))

    one = graph.answer_question(object(), "q1")
    two = graph.answer_question(object(), "q2")

    assert one.conversation_id != two.conversation_id
    assert messages.calls[1]["messages"] == [{"role": "user", "content": "q2"}]


def test_an_unknown_conversation_id_simply_starts_that_conversation(fake_llm, fake_tools):
    fake_tools()
    messages = fake_llm(reply(text("a")))

    result = graph.answer_question(object(), "q", "not-seen-before")

    assert result.conversation_id == "not-seen-before"
    assert messages.calls[0]["messages"] == [{"role": "user", "content": "q"}]


def test_steps_and_sources_are_per_turn_not_per_conversation(fake_llm, fake_tools):
    """Only the message history accumulates; the rest describes the turn just taken."""
    fake_tools()
    fake_llm(
        reply(tool_use("rag_lookup", {"query": "q"})),
        reply(text("Grounded.")),
        reply(text("No tools this time.")),
    )

    first = graph.answer_question(object(), "q1")
    second = graph.answer_question(object(), "q2", first.conversation_id)

    assert (first.steps, first.sources) == (2, ["stac-spec.md"])
    assert (second.steps, second.sources) == (1, [])


def test_footprints_are_per_turn_like_sources(fake_llm, fake_tools):
    """
    Which is what lets the map leave them alone: a follow-up answered from history
    reports no footprints, and the UI reads that as "nothing new", not "clear".
    """
    fake_tools(stac_result=search(scene("S2A_one")))
    fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("One scene.")),
        reply(text("The first one, from what I already found.")),
    )

    first = graph.answer_question(object(), "q1")
    second = graph.answer_question(object(), "q2", first.conversation_id)

    assert [f["id"] for f in first.features] == ["S2A_one"]
    assert second.features == []


# --- streaming --------------------------------------------------------------


def stream(question="q", conversation_id=None, db=None):
    return list(graph.stream_answer(db or object(), question, conversation_id))


def of_type(events, kind):
    return [e for e in events if e["type"] == kind]


def test_the_stream_opens_with_the_conversation_id(fake_llm, fake_tools):
    """Settled before the graph runs: a stream that dies halfway is still resumable."""
    fake_tools()
    fake_llm(reply(text("a")))

    events = stream(conversation_id="c1")

    assert events[0] == {"type": "start", "conversation_id": "c1"}


def test_the_tokens_concatenate_to_the_answer(fake_llm, fake_tools):
    fake_tools()
    fake_llm(reply(text("STAC Items are GeoJSON Features.")))

    events = stream()

    assert "".join(e["text"] for e in of_type(events, "token")) == "STAC Items are GeoJSON Features."
    assert events[-1] == {
        "type": "done",
        "answer": "STAC Items are GeoJSON Features.",
        "sources": [],
        "steps": 1,
    }


def test_the_streamed_text_is_a_superset_of_the_final_answer(fake_llm, fake_tools):
    """
    Tokens come from every agent turn, including the preamble the model writes next to
    a tool_use; `done.answer` is the last turn alone. The UI renders both in order, so
    this is a contract rather than a discrepancy.
    """
    fake_tools()
    fake_llm(
        reply(text("Let me check. "), tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("One scene.")),
    )

    events = stream()

    assert "".join(e["text"] for e in of_type(events, "token")) == "Let me check. One scene."
    assert of_type(events, "done")[0]["answer"] == "One scene."


def test_each_tool_call_is_announced_and_then_reported(fake_llm, fake_tools):
    fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "items"})), reply(text("Grounded.")))

    events = stream()

    assert of_type(events, "tool_start") == [
        {"type": "tool_start", "id": "tu_1", "name": "rag_lookup", "input": {"query": "items"}}
    ]
    end = of_type(events, "tool_end")[0]
    assert (end["id"], end["name"], end["ok"], end["detail"]) == ("tu_1", "rag_lookup", True, None)
    assert isinstance(end["ms"], int)


def test_a_failed_tool_is_reported_as_such_and_the_turn_continues(fake_llm, fake_tools):
    """The visible half of "a failing tool is not a 500": until now nothing said so."""
    fake_tools(stac_error=RuntimeError("STAC API unreachable"))
    fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("The catalog is unreachable.")),
    )

    events = stream()

    end = of_type(events, "tool_end")[0]
    assert end["ok"] is False
    assert end["detail"] == "STAC API unreachable"
    assert of_type(events, "done")[0]["answer"] == "The catalog is unreachable."


def test_the_footprints_arrive_before_the_answer_finishes(fake_llm, fake_tools):
    """Which is what puts them on the map while the prose is still being written."""
    fake_tools(stac_result=search(scene("S2A_one")))
    fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("One scene.")),
    )

    events = stream()
    kinds = [e["type"] for e in events]

    collection = of_type(events, "features")[0]["collection"]
    assert collection["type"] == "FeatureCollection"
    assert [f["id"] for f in collection["features"]] == ["S2A_one"]
    # After the tool that produced them, before the answer they are the subject of.
    assert kinds.index("features") > kinds.index("tool_end")
    assert kinds.index("features") < len(kinds) - 1


def test_a_turn_that_ran_no_tools_emits_no_footprints(fake_llm, fake_tools):
    """
    No event at all, not an empty collection: the map then has no rule to get wrong,
    and a follow-up about the scenes already on screen leaves them there.
    """
    fake_tools()
    fake_llm(reply(text("From what I already found, the first one.")))

    assert of_type(stream(), "features") == []


def test_the_collection_grows_once_per_search_not_once_per_node(fake_llm, fake_tools):
    fake_tools(stac_result=search(scene("S2A_one")))
    fake_llm(
        reply(tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_1")),
        reply(tool_use("compute_index", {"item_id": "S2A_one", "bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_2")),
        reply(text("Mean NDVI 0.42.")),
    )

    collections = [e["collection"] for e in of_type(stream(), "features")]

    assert [len(c["features"]) for c in collections] == [1, 2]
    assert [f["properties"]["kind"] for f in collections[-1]["features"]] == ["footprint", "aoi"]


def test_the_streaming_and_the_blocking_path_agree(fake_llm, fake_tools):
    """
    Two entry points over one graph. This is what makes that safe: the same script has
    to produce the same text, sources and step count either way.
    """
    script = (
        reply(text("Let me look. "), tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]})),
        reply(text("One scene.")),
    )

    fake_tools(stac_result=search(scene("S2A_one")))
    fake_llm(*script)
    blocking = graph.answer_question(object(), "q")

    fake_tools(stac_result=search(scene("S2A_one")))
    fake_llm(*script)
    done = of_type(stream(), "done")[0]

    assert done["answer"] == blocking.text
    assert done["sources"] == blocking.sources
    assert done["steps"] == blocking.steps


# --- an interrupted turn ----------------------------------------------------


def abandon_after_the_agent_turn(conversation_id="c1"):
    """
    Leave a thread exactly as a stopped stream leaves it: the assistant turn asking for a
    tool is checkpointed, the tool_result answering it never is.

    Abandoning the graph's own generator, which is what Starlette does to the response
    body when the client goes away, rather than planting a message - a planted one would
    only test the repair against a shape this file invented. The break has to land in the
    window *between* the two supersteps: `agent`'s write is committed, `tools` has not run.
    """
    run = graph._graph().stream(
        graph._turn_input("q1"),
        graph._turn_config(conversation_id),
        context=graph.AgentContext(db=object(), trace=Turn("c", "q1")),
        stream_mode=["updates"],
    )
    for _, chunk in run:
        if "agent" in chunk:
            break
    run.close()

    return conversation_id


def interrupt_after_the_tool_call(fake_llm, fake_tools, conversation_id="c1", **_):
    fake_tools()
    fake_llm(
        reply(text("Let me check. "), tool_use("rag_lookup", {"query": "items"})),
        reply(text("Grounded.")),
    )
    return abandon_after_the_agent_turn(conversation_id)


def history(conversation_id="c1"):
    return graph._graph().get_state(graph._turn_config(conversation_id)).values["messages"]


def test_stopping_a_stream_really_does_leave_a_tool_call_open(fake_llm, fake_tools):
    """The bug this repairs, asserted first: without it the next turn is a 400."""
    interrupt_after_the_tool_call(fake_llm, fake_tools)

    assert [b["type"] for b in history()[-1]["content"]] == ["text", "tool_use"]
    assert history()[-1]["role"] == "assistant"


def test_the_next_turn_closes_the_open_tool_call(fake_llm, fake_tools):
    """
    Anthropic refuses a history where a tool_use has no tool_result, so the damage falls
    on every *later* turn: one Stop would otherwise end the conversation for good.
    """
    interrupt_after_the_tool_call(fake_llm, fake_tools)
    open_call = history()[-1]["content"][1]["id"]

    fake_tools()
    messages = fake_llm(reply(text("Picking up where we left off.")))
    result = graph.answer_question(object(), "q2", "c1")

    sent = messages.calls[0]["messages"]
    healed = sent[sent.index(next(m for m in sent if m["role"] == "assistant")) + 1]
    assert healed["role"] == "user"
    assert healed["content"][0]["tool_use_id"] == open_call
    assert healed["content"][0]["is_error"] is True
    assert "interrupted" in healed["content"][0]["content"]
    assert result.text == "Picking up where we left off."


def test_every_open_call_of_the_turn_is_closed(fake_llm, fake_tools):
    """The model batches tool calls; a partial repair is still a rejected history."""
    fake_tools()
    fake_llm(
        reply(
            tool_use("rag_lookup", {"query": "a"}, id="tu_1"),
            tool_use("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}, id="tu_2"),
        ),
        reply(text("done")),
    )
    abandon_after_the_agent_turn()

    fake_tools()
    messages = fake_llm(reply(text("ok")))
    graph.answer_question(object(), "q2", "c1")

    results = messages.calls[0]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]


def test_a_healthy_conversation_is_left_alone(fake_llm, fake_tools):
    """The repair must be invisible when there is nothing to repair."""
    fake_tools()
    fake_llm(reply(tool_use("rag_lookup", {"query": "q"})), reply(text("Grounded.")))
    graph.answer_question(object(), "q1", "c1")
    before = list(history())

    fake_tools()
    fake_llm(reply(text("Second.")))
    graph.answer_question(object(), "q2", "c1")

    # The new turn appends the question and its answer, and nothing else.
    assert history()[: len(before)] == before
    assert [m["role"] for m in history()[len(before) :]] == ["user", "assistant"]


def test_a_thread_that_has_never_run_needs_no_repair(fake_llm, fake_tools):
    fake_tools()
    fake_llm(reply(text("First answer.")))

    assert graph.answer_question(object(), "q", "brand-new").text == "First answer."


def test_the_repair_reports_what_it_closed(fake_llm, fake_tools):
    interrupt_after_the_tool_call(fake_llm, fake_tools)
    config = graph._turn_config("c1")

    assert graph._repair_interrupted_turn(config) == 1
    # Idempotent: once closed, there is nothing left dangling.
    assert graph._repair_interrupted_turn(config) == 0


# --- import purity ----------------------------------------------------------


def test_import_does_not_build_an_anthropic_client(monkeypatch):
    """Importing the graph must not construct a client (same bar as the rest of app/)."""

    def boom(*args, **kwargs):
        raise AssertionError("Anthropic client built at import time")

    monkeypatch.setattr("anthropic.Anthropic", boom)

    importlib.reload(graph)

    assert graph._cached_client is None
    assert graph._cached_graph is None
