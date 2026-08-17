"""
LangGraph orchestration behind POST /ask.

Two nodes and a conditional edge between them:

    START -> agent -> (tool_use blocks?) -> tools -> agent -> ... -> END

`agent` calls Claude, `tools` executes whatever it asked for. The conditional edge is
the router: it dispatches on what the model actually decided to call, which is why
there is no separate classification node - see "On the router" below.

`settings.max_agent_steps` is a hard cap. The `agent` node stops passing `tools` to the
model once the cap is reached, so the last turn has no choice but to conclude with what
was gathered. Tools therefore run at most `max_agent_steps` times, and the model is
called at most `max_agent_steps + 1` times.

A tool that fails is not a 500: the error goes back to the model as an errored
tool_result, because a malformed bbox or an unreachable catalog is something it can
explain or retry.

## Conversational memory

The compiled graph carries a checkpointer keyed by `thread_id`, so a conversation is
resumed by passing the same `conversation_id` back to `/ask`. Only `messages`
accumulates across turns: `steps`, `sources` and `features` are per-turn and get reset by
the input.

Everything in the state is plain data. Assistant turns are stored as dicts rather than
Anthropic SDK block objects, because the checkpointer serializes the state: SDK objects
either fail to serialize or come back as dicts on a resumed turn, so the code would have
to handle both shapes. `model_dump(exclude_none=True)` produces exactly what the API
accepts on the way back in.

The checkpointer is `MemorySaver`, which lives in the process and dies with it. Moving to
Postgres needs the `langgraph-checkpoint-postgres` package; the state is already in a
shape that survives the trip.

## On the router

The roadmap called for a router node classifying the question as documentation vs. data
vs. computation. That was written before the step-3b loop existed, and building it now
would be a regression: a hard upfront classification cannot express "both", and the
question that chains `rag_lookup` and `stac_search` in one answer is exactly the one
verified live in `VERIFY.md`. It would also cost an extra model call per request to
decide something the model already decides for free, as part of the turn it is going to
take anyway. The conditional edge below does the routing on what was actually asked for.
"""

import json
import operator
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

from anthropic import Anthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy.orm import Session

from app.agents.cost import turn_cost_usd
from app.config import settings
from app.obs.tracing import Turn
from app.tools.compute_index import COMPUTE_INDEX_TOOL, compute_index, index_footprint
from app.tools.rag_lookup import RAG_LOOKUP_TOOL, rag_lookup
from app.tools.stac_search import (
    STAC_SEARCH_TOOL,
    item_footprint,
    model_view,
    stac_search,
)

# 1000 was enough for step 2's single-source answers, but a reply that combines a
# documentation lookup with a catalog listing runs past it and gets cut mid-sentence,
# with nothing in the response to say so.
MAX_TOKENS = 4096

TOOLS = [RAG_LOOKUP_TOOL, STAC_SEARCH_TOOL, COMPUTE_INDEX_TOOL]

SYSTEM_PROMPT = (
    "You are a technical assistant with expertise in EO (Earth Observation) satellite "
    "data.\n\n"
    "Three tools are available to you:\n"
    "- rag_lookup: the indexed technical documentation. Use it for questions about how "
    "something is defined, structured or supposed to work.\n"
    "- stac_search: a live STAC catalog. Use it for questions about which satellite "
    "data exists for a given place and period.\n"
    "- compute_index: reads the pixels of one scene and returns the statistics of a "
    "spectral index (NDVI, NDWI) over an area. Find the scene with stac_search first.\n\n"
    "Call a tool before answering whenever the question falls to any of them, and "
    "ground the answer in what comes back: never invent field names, item identifiers, "
    "acquisition dates or cloud cover figures. If a tool returns nothing useful or "
    "fails, say so plainly instead of filling the gap. Cite the sources the tools "
    "report."
)

_cached_client = None
_cached_graph = None


def _client() -> Anthropic:
    """Anthropic client built and cached on first use (no side effects at import time)."""
    global _cached_client
    if _cached_client is None:
        _cached_client = Anthropic(api_key=settings.anthropic_api_key)
    return _cached_client


@dataclass
class AgentContext:
    """
    Per-request dependencies. Deliberately not part of the state: a SQLAlchemy Session
    must not be checkpointed - it would be serialized, and a resumed conversation would
    come back holding a session that closed long ago.

    The `Turn` is here for the same reason and not by analogy: it holds a clock reading
    and an open Langfuse span, so a resumed conversation would come back mid-measurement
    of a request that finished days earlier.
    """

    db: Session
    trace: Turn


class ConversationBudgetExceeded(RuntimeError):
    """
    Raised before a turn runs when the conversation has spent its allowance.

    A RuntimeError so the streaming path's existing failure handling already covers it,
    and its own type so `/ask` can answer 429 rather than 500: this is a limit being
    enforced, not something that went wrong.
    """


class ConversationState(TypedDict):
    """
    Two kinds of field, and the difference is the reducer.

    `messages`, `turns` and `cost_usd` **accumulate** across the thread - their reducers
    combine the old value with the new, so they survive the per-turn reset and describe the
    conversation. `sources`, `steps` and `features` describe the current turn only and are
    overwritten by the input of each invocation.

    `turns` counts invocations rather than model calls: `_turn_input` contributes the 1,
    so the count follows questions asked, which is what a per-conversation limit means.
    `steps` already bounds the model calls *within* a turn.
    """

    messages: Annotated[list, operator.add]
    sources: list[str]
    steps: int
    features: list[dict]
    turns: Annotated[int, operator.add]
    cost_usd: Annotated[float, operator.add]


@dataclass
class Answer:
    """What a turn produced: the text, the sources to cite, the calls it took, the thread."""

    text: str
    sources: list[str]
    steps: int
    conversation_id: str
    # The footprints of whatever the tools touched, as GeoJSON Features for the map.
    # Defaulted because it is the newest field and nothing outside the streaming path
    # reads it: `/ask` does not return it.
    features: list[dict] = field(default_factory=list)


def _run_tool(
    name: str, tool_input: dict, db: Session, trace: Turn
) -> tuple[str, list[str], list[dict]]:
    """
    Execute one tool call. Returns what the model gets back, what to cite for it, and
    the footprints for the map.

    The three are separate on purpose. The model gets prose or a compact JSON view; the
    citation is provenance; the features are coordinates, which the model cannot reason
    about and which would only cost it context.

    Each tool returns a pydantic model, and this is where they become the string the API
    accepts as `tool_result` content. `rag_lookup` is the one whose text is passed through
    rather than serialized: retrieved passages already carry their `[Source: ...]` labels,
    and JSON would only add escaping to prose the model reads as prose.
    """
    if name == RAG_LOOKUP_TOOL["name"]:
        lookup = rag_lookup(db, **tool_input)
        # Recorded here rather than inside the tool, so `rag_lookup` stays a function of
        # its arguments and the graph keeps being the only thing that knows about a turn.
        trace.retrieval(tool_input.get("query", ""), lookup.scored)
        return lookup.context, lookup.sources, []

    if name == STAC_SEARCH_TOOL["name"]:
        search = stac_search(**tool_input)
        footprints = [f for f in map(item_footprint, search.items) if f]
        # The provenance of a scene listing is the catalog it came from: the item ids
        # themselves are already in the tool result the answer is built on.
        return json.dumps(model_view(search)), [settings.stac_api_url], footprints

    if name == COMPUTE_INDEX_TOOL["name"]:
        measured = compute_index(**tool_input)
        # Cite the scene the numbers were measured on, not the catalog: the pixels are
        # the source here, and the item id is what makes the result reproducible.
        return (
            json.dumps(measured.model_dump()),
            [f"{settings.stac_api_url} ({measured.item_id})"],
            [index_footprint(measured)],
        )

    raise ValueError(f"Unknown tool: {name}")


def _blocks(messages: list) -> list[dict]:
    """The content blocks of the last turn, or nothing if it was a plain string."""
    content = messages[-1].get("content") if messages else None
    return content if isinstance(content, list) else []


def _tool_uses(messages: list) -> list[dict]:
    """The tool_use blocks of the last assistant turn, if it asked for any."""
    return [b for b in _blocks(messages) if b.get("type") == "tool_use"]


def _answer_text(messages: list) -> str:
    return "".join(b["text"] for b in _blocks(messages) if b.get("type") == "text")


def agent(state: ConversationState, runtime: Runtime[AgentContext]) -> dict:
    """
    Call the model, offering the tools only while the step cap has room left.

    The call streams, and the text deltas go out on LangGraph's custom channel as they
    arrive. This costs `/ask` nothing: under `.invoke()` `get_stream_writer()` hands
    back a writer whose output goes nowhere, so one node body serves both entry points
    and there is no async twin to keep in step.

    The generation span wraps the call rather than being recorded after it, which is the
    difference between measuring the model and measuring the moment we noticed it had
    finished. `usage` only exists once the stream is drained, so the record is filled in
    from inside the block.
    """
    kwargs = {
        "model": settings.claude_model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": state["messages"],
    }
    if state["steps"] < settings.max_agent_steps:
        kwargs["tools"] = TOOLS

    # Inside the node body, never at module scope: it reads the running config.
    writer = get_stream_writer()
    cost = 0.0

    with runtime.context.trace.generation(settings.claude_model, state["messages"]) as record:
        with _client().messages.stream(**kwargs) as stream:
            for delta in stream.text_stream:
                writer({"type": "token", "text": delta})
            response = stream.get_final_message()

        cost = turn_cost_usd(settings.claude_model, response.usage)
        record.input_tokens = response.usage.input_tokens
        record.output_tokens = response.usage.output_tokens
        record.cost_usd = cost
        record.stop_reason = getattr(response, "stop_reason", None)

    return {
        # Plain dicts, not SDK objects: the state gets checkpointed. exclude_none keeps
        # the blocks in the shape the API accepts when they are sent back.
        "messages": [
            {
                "role": "assistant",
                "content": [b.model_dump(exclude_none=True) for b in response.content],
            }
        ],
        "steps": state["steps"] + 1,
        # A delta, not a total: the reducer adds it to what the thread has already spent.
        # Charged here rather than at the end of the turn because this is the only place
        # that sees a `usage`, and a turn making three model calls has to pay for three.
        "cost_usd": cost,
    }


def tools(state: ConversationState, runtime: Runtime[AgentContext]) -> dict:
    """
    Run every tool the last turn asked for, collecting their sources and footprints.

    Each call is announced on the custom channel before it runs and reported after, so
    a caller watching the stream sees a search or an NDVI read in progress rather than
    silence - `compute_index` alone is 5 to 15 seconds of it. The outcome is reported
    too: a tool that fails comes back to the model as an errored tool_result, and until
    now nothing outside the model's context said so.
    """
    db = runtime.context.db
    trace = runtime.context.trace
    writer = get_stream_writer()
    sources = list(state["sources"])
    features = list(state["features"])
    seen = {f["properties"]["id"] for f in features}
    results = []

    for block in _tool_uses(state["messages"]):
        writer({"type": "tool_start", "id": block["id"], "name": block["name"], "input": block["input"]})
        started = time.perf_counter()

        try:
            # The trace span is inside the try, not around it: it records how the call
            # ended and then re-raises, leaving the except below to do what it always did.
            with trace.tool(block["name"], dict(block["input"])):
                content, cited, found = _run_tool(
                    block["name"], dict(block["input"]), db, trace
                )
            sources.extend(cited)
            # Two searches in one turn routinely overlap: the same scene covering the
            # same place is one footprint on the map, not two stacked polygons.
            features.extend(f for f in found if f["properties"]["id"] not in seen)
            seen.update(f["properties"]["id"] for f in found)
            is_error, detail = False, None
        except (RuntimeError, ValueError, TypeError) as e:
            # ValueError/TypeError: the model sent a bad bbox or an argument the tool has
            # not got. RuntimeError: the catalog or Bedrock refused. All recoverable by
            # the model, none of them the caller's problem.
            content, is_error, detail = str(e), True, str(e)

        writer(
            {
                "type": "tool_end",
                "id": block["id"],
                "name": block["name"],
                "ok": not is_error,
                "ms": round((time.perf_counter() - started) * 1000),
                "detail": detail,
            }
        )

        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": content,
                "is_error": is_error,
            }
        )

    return {
        "messages": [{"role": "user", "content": results}],
        "sources": sources,
        "features": features,
    }


def route(state: ConversationState) -> str:
    """
    The router. Dispatches on the blocks the model actually produced rather than on
    stop_reason: it is the blocks we have to answer, and an empty turn cannot then
    produce a tool_result message with nothing in it.
    """
    return "tools" if _tool_uses(state["messages"]) else END


def _graph():
    """Compile once and cache: the checkpointer has to be shared across requests."""
    global _cached_graph
    if _cached_graph is None:
        builder = StateGraph(ConversationState, context_schema=AgentContext)
        builder.add_node("agent", agent)
        builder.add_node("tools", tools)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
        builder.add_edge("tools", "agent")
        _cached_graph = builder.compile(checkpointer=MemorySaver())

    return _cached_graph


def _turn_input(question: str) -> dict:
    """
    The input of one turn. Everything but `messages` is reset here, which is what makes
    `sources`, `steps` and `features` per-turn rather than per-conversation.

    Shared with the streaming entry point below so the two cannot drift.
    """
    return {
        "messages": [{"role": "user", "content": question}],
        "sources": [],
        "steps": 0,
        "features": [],
        # Not a reset: `turns` accumulates, so contributing 1 per invocation is what
        # counts the questions. `cost_usd` is absent on purpose - only the agent node
        # contributes to it, and naming it here would reset nothing but confuse plenty.
        "turns": 1,
    }


def _spent(config: dict) -> tuple[int, float]:
    """What the thread has used so far: turns taken and dollars estimated."""
    values = _graph().get_state(config).values or {}

    # .get with a default, not indexing: a thread that has never run has no channels
    # written yet, and a brand new conversation is the common case, not an error.
    return values.get("turns", 0), values.get("cost_usd", 0.0)


def _check_budget(config: dict) -> None:
    """
    Refuse the turn if the conversation has already spent its allowance.

    Checked *before* the turn rather than after, because after is too late to be a limit:
    the tokens are already bought. The consequence is that the cap is crossed rather than
    respected exactly - the turn that exceeds it runs to completion and the next one is
    refused - which is the honest way to bound something whose cost is unknown until it
    has been paid.

    `settings.max_agent_steps` bounds one turn; these bound the thread. Without them a
    conversation could be continued indefinitely, each turn resending a history that only
    grows, and the step cap would dutifully permit every one of them. Either limit at 0
    turns that check off.
    """
    turns, cost = _spent(config)

    if settings.max_conversation_turns and turns >= settings.max_conversation_turns:
        raise ConversationBudgetExceeded(
            f"This conversation has reached its limit of "
            f"{settings.max_conversation_turns} turns. Start a new one to continue."
        )

    if settings.max_conversation_cost_usd and cost >= settings.max_conversation_cost_usd:
        raise ConversationBudgetExceeded(
            f"This conversation has reached its budget of "
            f"${settings.max_conversation_cost_usd:.2f} (estimated ${cost:.2f} spent). "
            "Start a new one to continue."
        )


def _repair_interrupted_turn(config: dict) -> int:
    """
    Close any tool call the previous turn left dangling. Returns how many it closed.

    A client that goes away mid-stream - the Stop button, a closed tab, a dropped
    connection - abandons the graph between `agent` and `tools`. By then the checkpointer
    has already stored the assistant turn carrying its `tool_use` blocks, and the
    `tool_result`s answering them are never written. Anthropic refuses that history
    outright ("tool_use ids were found without tool_result blocks"), so it is not the
    interrupted turn that breaks: it is *every* turn after it. One Stop and the
    conversation is dead until a new thread is started.

    Repaired on the way in rather than on the way out because there is no way out - the
    generator is already being closed when we find out. Same principle as a tool that
    raises: the model is told the call produced no result and answers around it.
    """
    dangling = _tool_uses((_graph().get_state(config).values or {}).get("messages") or [])
    if not dangling:
        return 0

    _graph().update_state(
        config,
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": (
                                "The previous turn was interrupted before this tool ran. "
                                "Its result is unavailable; call it again if you still need it."
                            ),
                            "is_error": True,
                        }
                        for block in dangling
                    ],
                }
            ]
        },
    )

    return len(dangling)


def _turn_config(conversation_id: str) -> dict:
    return {
        "configurable": {"thread_id": conversation_id},
        # agent and tools alternate, so the cap needs twice its value in nodes, plus the
        # concluding call. Without this the graph would hit LangGraph's default of 25
        # before the cap ever applied.
        "recursion_limit": 2 * settings.max_agent_steps + 5,
    }


def conversation_spend(conversation_id: str) -> tuple[int, float]:
    """
    Turns taken and dollars estimated on a thread, for a caller that wants to read the
    meter rather than be stopped by it.

    The eval harness is the reason this is public: it prices each case, and the alternative
    was reaching into `_spent` from a script.
    """
    return _spent(_turn_config(conversation_id))


def check_conversation_budget(conversation_id: str | None) -> None:
    """
    The budget guard, callable before a response has started.

    Exists for the streaming endpoint. Once `stream_answer` has yielded its first frame
    the status line is gone and a refusal can only be an error *inside* a 200, which is
    the wrong shape for "you have hit a limit". Calling this first lets that endpoint
    answer 429 like `/ask` does, and the check inside `stream_answer` stays as the
    guarantee - this one is an optimization of the status code, not the enforcement.

    A conversation that does not exist yet has spent nothing, so `None` is always allowed.
    """
    if conversation_id:
        _check_budget(_turn_config(conversation_id))


def answer_question(db: Session, question: str, conversation_id: str | None = None) -> Answer:
    """
    Run one turn through the graph and return the grounded answer.

    Passing back the `conversation_id` of a previous turn resumes that conversation; a
    new one is started when it is omitted or unknown.

    Raises `ConversationBudgetExceeded` if the thread has spent its allowance.
    """
    conversation_id = conversation_id or str(uuid.uuid4())
    config = _turn_config(conversation_id)
    _check_budget(config)
    _repair_interrupted_turn(config)

    trace = Turn(conversation_id=conversation_id, question=question)
    trace.start()

    state = _graph().invoke(
        _turn_input(question),
        config,
        context=AgentContext(db=db, trace=trace),
    )

    answer = Answer(
        text=_answer_text(state["messages"]),
        sources=sorted(set(state["sources"])),
        steps=state["steps"],
        conversation_id=conversation_id,
        features=state["features"],
    )
    trace.finish(answer.text, answer.sources, answer.steps)

    return answer


def stream_answer(
    db: Session, question: str, conversation_id: str | None = None
) -> Iterator[dict]:
    """
    Run one turn and yield what happens as it happens: `start`, then `token`,
    `tool_start`, `tool_end` and `features` in the order they occur, then `done`.

    Events are plain dicts and carry no transport framing - `app/api/routes.py` turns
    them into SSE. Same graph object and therefore same checkpointer as
    `answer_question`, so a conversation can move between the two entry points.

    `conversation_id` goes out first rather than last: it is settled before the graph
    runs, and a stream that dies halfway still leaves the caller holding a usable thread.

    Raises `ConversationBudgetExceeded` before yielding anything if the thread has spent
    its allowance - which, on this path, a caller should have already ruled out with
    `check_conversation_budget` while it could still choose a status code.

    The trace is closed in a `finally`, because this is the entry point a client can
    abandon: the Stop button closes this generator, and a turn that stopped halfway is
    exactly the one worth having a record of. Without it the trace of every interrupted
    turn would simply be missing - and an open Langfuse span left behind with it.
    """
    conversation_id = conversation_id or str(uuid.uuid4())
    config = _turn_config(conversation_id)
    _check_budget(config)

    # Opened before the first frame goes out, so that everything past the budget check is
    # inside the `finally`. Opening it after `start` would leave the shortest abandonment
    # of all - a client that disconnects immediately - as the one turn with no record.
    trace = Turn(conversation_id=conversation_id, question=question)
    trace.start()
    # Watched as it goes past rather than recomputed: `done` already carries exactly the
    # three things the trace closes on, and an abandoned turn simply never sets it.
    done: dict = {}

    try:
        yield {"type": "start", "conversation_id": conversation_id}

        # This is the entry point that produces the damage - a client that stops
        # listening - so it is also the one that most often has to clear it.
        _repair_interrupted_turn(config)

        for event in _stream_turn(db, question, config, trace):
            if event["type"] == "done":
                done = event
            yield event
    finally:
        trace.finish(
            done.get("answer", ""), done.get("sources", []), done.get("steps", 0)
        )


def _stream_turn(db: Session, question: str, config: dict, trace: Turn) -> Iterator[dict]:
    """
    The body of `stream_answer`, split out so the caller's `finally` guards one call.

    Kept separate rather than inlined because a `try/finally` wrapped around a loop that
    both yields and assigns is where a generator's cleanup semantics get subtle - here the
    wrapper does nothing but watch and close.
    """
    state, emitted = None, 0

    for mode, chunk in _graph().stream(
        _turn_input(question),
        config,
        context=AgentContext(db=db, trace=trace),
        stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            yield chunk
            continue

        state = chunk
        # Cumulative, and only when the list grew. The footprints then reach the map
        # the moment the search returns rather than after the prose finishes, a turn
        # chaining two tools emits at most twice, and a turn that ran no tools emits
        # nothing at all - which is what leaves the previous footprints alone without
        # the caller needing a rule for it.
        if len(state["features"]) > emitted:
            emitted = len(state["features"])
            yield {
                "type": "features",
                "collection": {"type": "FeatureCollection", "features": state["features"]},
            }

    yield {
        "type": "done",
        "answer": _answer_text(state["messages"]),
        "sources": sorted(set(state["sources"])),
        "steps": state["steps"],
    }
