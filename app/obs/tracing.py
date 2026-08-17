"""
Tracing for one turn: which tools ran, what each model call cost, how good retrieval was.

## Why this is not just the stream events

The graph already describes itself while it works - `tool_start`, `tool_end` with an
outcome and an elapsed time, `token`, `done` with steps and sources. But that vocabulary
is a **wire protocol for a client that is holding the stream open**, and
`get_stream_writer()` returns a writer that goes nowhere under `.invoke()`. So `/ask`
produced no record of anything at all, and a stream nobody was watching produced one that
was thrown away. `sources` on the response was genuinely the only external signal.

This is the other half: the same facts, recorded where they can outlive the request.
Deliberately *not* built by tapping the stream writer, for two reasons - the timings have
to bracket the real work rather than the moment an event was emitted, and a wire format
and a telemetry format drift apart the first time either has a consumer of its own.

## Always on, exporter optional

A `Turn` always records, and always writes one JSON line per event to the `eo_rag.trace`
logger. That is the floor: `docker compose logs api | grep eo_rag.trace` answers "which
tools ran and what did they cost" with no account, no key and no dependency.

Langfuse sits on top as an exporter and is off unless configured
(`app/obs/langfuse_exporter.py`). When it is off, the only cost of all this is building a
few dataclasses and formatting some log lines.

## Per request, never checkpointed

A `Turn` travels in the LangGraph **context** next to the `Session`, for exactly the same
reason: it holds an open Langfuse span and a clock reading, and a resumed conversation
coming back with a half-finished span from a request that ended days ago would be worse
than no telemetry.
"""

import json
import logging
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.obs.langfuse_exporter import langfuse_client

logger = logging.getLogger("eo_rag.trace")


def configure_logging(level: int = logging.INFO, *, stream=None) -> None:
    """
    Give the trace logger somewhere to write. Called once, from `app/main.py`.

    Without this the whole "structured log is the floor" claim is false, and silently so:
    uvicorn configures `uvicorn`, `uvicorn.error` and `uvicorn.access` and leaves the root
    logger with **no handler**, so an INFO record from here propagates up to nothing and
    is dropped by the last-resort handler's WARNING threshold. Measured before this
    existed: a real question answered end to end put zero trace lines in the server log.

    The handler goes on `eo_rag` rather than on root, because switching on INFO for every
    library in the process is a different decision from wanting this application's own
    telemetry, and nobody asked for the first one.

    `propagate` is deliberately left alone: pytest's `caplog` captures through the root
    logger, and turning propagation off here would make every test in
    `tests/test_tracing.py` see nothing.

    ## `stream`, and why stdout is not always safe

    Defaults to stdout, which is right for a web server and **wrong for the MCP stdio
    transport, where stdout is the JSON-RPC channel itself**. One trace line written there
    corrupts the stream, and the client reports a parse error that names nothing useful.
    `app/mcp/server.py` therefore passes `sys.stderr`.

    Resolved at call time rather than as a default argument value, so that a caller (or a
    test) which has replaced `sys.stdout` gets the replacement rather than whatever the
    module was imported with.
    """
    app_logger = logging.getLogger("eo_rag")

    # Idempotent: uvicorn's --reload re-imports the module, and a handler added twice
    # prints every line twice.
    if app_logger.handlers:
        return

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    app_logger.addHandler(handler)
    app_logger.setLevel(level)


@dataclass
class ToolCall:
    """One tool invocation, whether or not it worked."""

    name: str
    input: dict
    ms: int
    ok: bool
    error: str | None = None


@dataclass
class Generation:
    """One model call. The step cap means a turn can hold several."""

    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ms: int
    stop_reason: str | None = None


@dataclass
class ChunkScore:
    """One retrieved chunk and how close it actually was."""

    source: str
    section: str | None
    distance: float


@dataclass
class Retrieval:
    """
    One `rag_lookup`, with the cosine distance of every chunk it returned.

    The roadmap's third item, and the one that answers a question the others cannot: an
    answer can be grounded in the best of a uniformly bad set of chunks, and nothing about
    the tool call or the token count would show it. A `best` of 0.6 is that.
    """

    query: str
    chunks: list[ChunkScore]

    @property
    def best(self) -> float | None:
        return min((c.distance for c in self.chunks), default=None)


@dataclass
class Turn:
    """
    What one invocation of the graph did. Created per request, discarded with it.

    Nothing here raises: a turn whose telemetry fails is still a turn that answered, and
    the whole point of `_safe` is that observability must not become a new way for the
    request to die.
    """

    conversation_id: str
    question: str
    tools: list[ToolCall] = field(default_factory=list)
    generations: list[Generation] = field(default_factory=list)
    retrievals: list[Retrieval] = field(default_factory=list)

    _started: float = field(default_factory=time.perf_counter)
    _span: Any = None

    # --- the record ---------------------------------------------------------

    @property
    def cost_usd(self) -> float:
        return sum(g.cost_usd for g in self.generations)

    @property
    def input_tokens(self) -> int:
        return sum(g.input_tokens for g in self.generations)

    @property
    def output_tokens(self) -> int:
        return sum(g.output_tokens for g in self.generations)

    @property
    def ms(self) -> int:
        return round((time.perf_counter() - self._started) * 1000)

    # --- recording ----------------------------------------------------------

    def start(self) -> None:
        """Open the trace. Separate from __init__ so a Turn is free to build in tests."""
        self._log("turn_start", question=self.question)
        self._span = _safe(
            lambda lf: lf.start_observation(
                name="turn",
                as_type="span",
                input=self.question,
                metadata={"conversation_id": self.conversation_id},
            )
        )

    @contextmanager
    def tool(self, name: str, tool_input: dict):
        """
        Time one tool call and record how it ended.

        The elapsed time brackets the call itself, which is why this is a context manager
        rather than something reconstructed from two events: `compute_index` is 5 to 15
        seconds of reading pixels and that is the number worth having.

        It does not swallow the exception - the `tools` node still turns it into an errored
        tool_result, which is its job, not this one's.
        """
        started = time.perf_counter()
        span = _safe(
            lambda lf: lf.start_observation(name=name, as_type="tool", input=tool_input)
        )
        error: str | None = None

        try:
            yield
        except Exception as e:
            error = str(e)
            raise
        finally:
            call = ToolCall(
                name=name,
                input=tool_input,
                ms=round((time.perf_counter() - started) * 1000),
                ok=error is None,
                error=error,
            )
            self.tools.append(call)
            self._log("tool", name=name, ok=call.ok, ms=call.ms, error=error)
            _safe(
                lambda _: span.update(
                    output=error or "ok",
                    level="ERROR" if error else "DEFAULT",
                    status_message=error,
                )
                or span.end(),
                when=span is not None,
            )

    @contextmanager
    def generation(self, model: str, messages: list):
        """
        Time one model call. The caller fills in what it cost once the response is in.

        Yields a `Generation` with zeroed counters: the usage is only known after the
        stream finishes, and the span has to be open before it starts for the latency to
        mean anything.
        """
        record = Generation(model=model, input_tokens=0, output_tokens=0, cost_usd=0.0, ms=0)
        started = time.perf_counter()
        span = _safe(
            lambda lf: lf.start_observation(
                name="agent",
                as_type="generation",
                model=model,
                input=messages,
            )
        )

        try:
            yield record
        finally:
            record.ms = round((time.perf_counter() - started) * 1000)
            self.generations.append(record)
            self._log(
                "generation",
                model=model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cost_usd=round(record.cost_usd, 6),
                ms=record.ms,
                stop_reason=record.stop_reason,
            )
            _safe(
                lambda _: span.update(
                    # Langfuse's own names, so its dashboards do the arithmetic: it reads
                    # `input`/`output` for tokens and totals `cost_details` per trace.
                    usage_details={
                        "input": record.input_tokens,
                        "output": record.output_tokens,
                    },
                    cost_details={"total": record.cost_usd},
                    output=record.stop_reason,
                )
                or span.end(),
                when=span is not None,
            )

    def retrieval(self, query: str, scored: list[tuple[Any, float]]) -> None:
        """Record what `rag_lookup` retrieved and how close each chunk was."""
        record = Retrieval(
            query=query,
            chunks=[
                ChunkScore(source=c.source, section=c.section, distance=float(d))
                for c, d in scored
            ],
        )
        self.retrievals.append(record)
        self._log(
            "retrieval",
            query=query,
            chunks=len(record.chunks),
            best=None if record.best is None else round(record.best, 4),
        )
        _safe(
            lambda lf: lf.start_observation(
                name="rag_lookup",
                as_type="retriever",
                input=query,
                output=[
                    {"source": c.source, "section": c.section, "distance": c.distance}
                    for c in record.chunks
                ],
                metadata={"best_distance": record.best},
            ).end()
        )

    def finish(self, answer: str, sources: list[str], steps: int) -> None:
        """Close the trace with the totals the whole turn is judged on."""
        self._log(
            "turn_end",
            steps=steps,
            tools=[t.name for t in self.tools],
            sources=sources,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=round(self.cost_usd, 6),
            ms=self.ms,
        )

        if self._span is None:
            return

        _safe(
            lambda _: self._span.update(
                output=answer,
                metadata={
                    "steps": steps,
                    "sources": sources,
                    "tools": [t.name for t in self.tools],
                    "cost_usd": self.cost_usd,
                },
            )
            or self._span.end()
        )

    # --- output -------------------------------------------------------------

    def _log(self, event: str, **fields) -> None:
        """
        One JSON object per line, on a logger of its own.

        JSON because the alternative is inventing a format that has to be parsed back out
        of prose; one line because `json.dumps` escapes newlines and a log line that spans
        several is a log line that grep cannot find.
        """
        payload = {
            "event": event,
            "conversation_id": self.conversation_id,
            **fields,
        }
        logger.info(json.dumps(payload, ensure_ascii=False, default=str))


def _safe(call, when: bool = True):
    """
    Run an exporter call, or give up quietly.

    Telemetry is not allowed to break a request. Langfuse batches over the network on a
    background thread and can fail for reasons that have nothing to do with this
    application - a wrong key, an unreachable host, a version whose span object does not
    have the method this code expects - and none of those are a reason for a question to
    go unanswered.
    """
    client = langfuse_client()
    if client is None or not when:
        return None

    try:
        return call(client)
    except Exception:
        logger.warning("langfuse export failed; continuing without it", exc_info=True)
        return None
