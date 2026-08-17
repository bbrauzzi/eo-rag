"""
Tests for the per-turn trace.

Two things kept apart: what a `Turn` records (pure, no exporter), and that the exporter
stays disabled and harmless when it is not configured - which is the state a checkout runs
in, and the state this whole suite runs in.

The log is asserted on as parsed JSON rather than as substrings. It is a machine-readable
record or it is nothing, and a test matching `"tool" in caplog.text` would pass just as
happily on prose.
"""

import importlib
import json
import logging
import sys

import pytest

from app.config import settings
from app.obs import langfuse_exporter
from app.obs.tracing import ChunkScore, Retrieval, Turn, configure_logging


@pytest.fixture(autouse=True)
def exporter_off(monkeypatch):
    """
    No Langfuse anywhere in this file: the `Turn` behaviour under test is the part that
    exists without it. `_cached_client` is reset so a decision made here cannot leak.
    """
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    monkeypatch.setattr(langfuse_exporter, "_cached_client", None)


@pytest.fixture
def traced(caplog):
    """A started Turn, plus a reader for the JSON lines it logged."""
    caplog.set_level(logging.INFO, logger="eo_rag.trace")

    def lines():
        # Both phases: the fixture starts the turn during setup, and caplog.records only
        # holds the phase currently running.
        records = [*caplog.get_records("setup"), *caplog.records]
        return [
            r.message for r in records if r.name == "eo_rag.trace" and r.levelno == logging.INFO
        ]

    def events(kind=None):
        parsed = [json.loads(line) for line in lines()]
        return [e for e in parsed if kind is None or e["event"] == kind]

    events.lines = lines

    turn = Turn(conversation_id="c1", question="What are STAC Items?")
    turn.start()
    return turn, events


def chunk(source, section=None):
    class FakeChunk:
        pass

    c = FakeChunk()
    c.source, c.section = source, section
    return c


# --- what gets recorded -------------------------------------------------------


def test_the_turn_opens_with_the_question(traced):
    _, events = traced

    assert events("turn_start")[0]["question"] == "What are STAC Items?"
    assert events("turn_start")[0]["conversation_id"] == "c1"


def test_a_tool_call_records_its_name_and_outcome(traced):
    turn, events = traced

    with turn.tool("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}):
        pass

    recorded = events("tool")[0]
    assert recorded["name"] == "stac_search"
    assert recorded["ok"] is True
    assert recorded["error"] is None
    assert isinstance(recorded["ms"], int)


def test_a_failing_tool_is_recorded_as_failed_and_still_raises(traced):
    """
    The record is the point; the exception is still the `tools` node's business. Swallowing
    it here would turn a failed search into a silent one.
    """
    turn, events = traced

    with pytest.raises(RuntimeError, match="unreachable"), turn.tool("stac_search", {}):
        raise RuntimeError("STAC API unreachable")

    recorded = events("tool")[0]
    assert recorded["ok"] is False
    assert recorded["error"] == "STAC API unreachable"
    assert turn.tools[0].ok is False


def test_a_generation_records_tokens_cost_and_latency(traced):
    turn, events = traced

    with turn.generation("claude-sonnet-4-6", messages=[]) as record:
        record.input_tokens = 1200
        record.output_tokens = 340
        record.cost_usd = 0.0087
        record.stop_reason = "end_turn"

    recorded = events("generation")[0]
    assert recorded["model"] == "claude-sonnet-4-6"
    assert (recorded["input_tokens"], recorded["output_tokens"]) == (1200, 340)
    assert recorded["cost_usd"] == pytest.approx(0.0087)
    assert recorded["stop_reason"] == "end_turn"
    assert isinstance(recorded["ms"], int)


def test_the_totals_add_up_over_every_call_of_the_turn(traced):
    """A turn chaining two tools makes three model calls, and pays for three."""
    turn, _ = traced

    for _ in range(3):
        with turn.generation("claude-sonnet-4-6", messages=[]) as record:
            record.input_tokens, record.output_tokens, record.cost_usd = 1000, 100, 0.005

    assert turn.input_tokens == 3000
    assert turn.output_tokens == 300
    assert turn.cost_usd == pytest.approx(0.015)


def test_retrieval_records_the_distance_of_every_chunk(traced):
    turn, events = traced

    turn.retrieval("what is an item", [(chunk("stac-spec.md", "Item"), 0.13), (chunk("a.md"), 0.42)])

    recorded = events("retrieval")[0]
    assert recorded["chunks"] == 2
    assert recorded["best"] == pytest.approx(0.13)
    assert turn.retrievals[0].chunks[0].section == "Item"


def test_the_best_distance_is_what_separates_grounded_from_merely_answered():
    """
    The signal the other two cannot give. Both of these ran one tool and spent the same
    tokens; only the distances say one of them had nothing good to work with.
    """
    good = Retrieval(query="q", chunks=[ChunkScore("a.md", None, 0.11)])
    poor = Retrieval(query="q", chunks=[ChunkScore("a.md", None, 0.72)])

    assert good.best < poor.best


def test_a_retrieval_that_matched_nothing_has_no_best(traced):
    turn, events = traced

    turn.retrieval("unrelated", [])

    assert turn.retrievals[0].best is None
    assert events("retrieval")[0]["best"] is None


def test_the_turn_closes_with_the_totals_it_is_judged_on(traced):
    turn, events = traced

    with turn.tool("rag_lookup", {"query": "q"}):
        pass
    with turn.generation("claude-sonnet-4-6", messages=[]) as record:
        record.input_tokens, record.output_tokens, record.cost_usd = 900, 120, 0.0045

    turn.finish("Items are GeoJSON Features.", ["stac-spec.md"], steps=2)

    end = events("turn_end")[0]
    assert end["steps"] == 2
    assert end["tools"] == ["rag_lookup"]
    assert end["sources"] == ["stac-spec.md"]
    assert end["input_tokens"] == 900
    assert end["cost_usd"] == pytest.approx(0.0045)
    assert isinstance(end["ms"], int)


def test_every_record_is_one_line_of_parseable_json(traced):
    """
    One line per event, machine-readable. `json.dumps` escapes newlines, so a question
    containing them cannot split a record in two and hide the second half from grep.
    """
    turn, events = traced
    Turn(conversation_id="c2", question="line one\nline two").start()

    with turn.tool("rag_lookup", {"query": "q"}):
        pass
    turn.finish("done", [], steps=1)

    for line in events.lines():
        assert "\n" not in line
        json.loads(line)

    # And the newline survived as data rather than as a line break.
    assert events("turn_start")[1]["question"] == "line one\nline two"


def test_the_conversation_id_is_on_every_line(traced):
    """It is what ties a turn's records together, and to the thread they belong to."""
    turn, events = traced

    with turn.tool("rag_lookup", {"query": "q"}):
        pass
    turn.finish("a", [], steps=1)

    assert {e["conversation_id"] for e in events()} == {"c1"}


def test_a_turn_that_ran_nothing_still_produces_a_record(traced):
    """A follow-up answered from history is a real turn and costs real tokens."""
    turn, events = traced
    turn.finish("From what I already found.", [], steps=1)

    assert events("turn_end")[0]["tools"] == []
    assert events("turn_end")[0]["sources"] == []


# --- the exporter is optional -------------------------------------------------


def test_no_keys_means_no_exporter(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    monkeypatch.setattr(langfuse_exporter, "_cached_client", None)

    assert langfuse_exporter.langfuse_client() is None
    assert langfuse_exporter.is_configured() is False


def test_one_key_is_not_configured(monkeypatch):
    """A half-filled .env is the common way to think tracing is on when it is not."""
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")

    assert langfuse_exporter.is_configured() is False


def test_the_switch_turns_it_off_with_the_keys_left_in_place(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-test")
    monkeypatch.setattr(langfuse_exporter, "_cached_client", None)

    assert langfuse_exporter.langfuse_client() is None


def test_the_decision_is_cached_rather_than_retaken_every_request(monkeypatch):
    """Otherwise a disabled exporter re-runs the import on every event of every turn."""
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    monkeypatch.setattr(langfuse_exporter, "_cached_client", None)

    langfuse_exporter.langfuse_client()

    assert langfuse_exporter._cached_client is langfuse_exporter._DISABLED


def test_configured_but_not_installed_warns_once_and_carries_on(monkeypatch, caplog):
    """
    The state someone lands in by setting keys without `uv sync --extra observability`.
    Worth exactly one warning - and not an ImportError at startup, which is what a
    top-level `import langfuse` would give them.
    """
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-test")
    monkeypatch.setattr(langfuse_exporter, "_cached_client", None)
    # Setting a module to None in sys.modules is what makes `import langfuse` raise.
    monkeypatch.setitem(sys.modules, "langfuse", None)

    with caplog.at_level(logging.WARNING, logger="eo_rag.trace"):
        assert langfuse_exporter.langfuse_client() is None
        assert langfuse_exporter.langfuse_client() is None

    assert sum("not installed" in r.message for r in caplog.records) == 1


def test_tracing_works_with_the_exporter_unavailable(traced):
    """
    The floor: with no keys, no package and no account, a turn still records what it did.
    Every assertion above depends on this and this test says so out loud.
    """
    turn, events = traced

    with turn.tool("stac_search", {}):
        pass
    turn.finish("answer", ["a.md"], steps=2)

    assert [e["event"] for e in events()] == ["turn_start", "tool", "turn_end"]


def test_an_exporter_that_raises_does_not_break_the_turn(monkeypatch, traced):
    """
    Telemetry is not allowed to become a new way for a request to fail. A wrong key, an
    unreachable host or an SDK whose span object lacks a method are all its problem.
    """
    turn, events = traced

    class Exploding:
        def start_observation(self, **_):
            raise RuntimeError("langfuse is having a bad day")

    monkeypatch.setattr(langfuse_exporter, "langfuse_client", lambda: Exploding())
    monkeypatch.setattr("app.obs.tracing.langfuse_client", lambda: Exploding())

    with turn.tool("stac_search", {}):
        pass
    turn.finish("answer", [], steps=1)

    # The local record is untouched by the exporter falling over.
    assert events("tool")[0]["ok"] is True
    assert events("turn_end")[0]["steps"] == 1


# --- the log has to actually go somewhere -------------------------------------


def test_configure_logging_gives_the_trace_logger_a_handler():
    """
    Load-bearing, and it was measured failing: uvicorn configures its own loggers and
    leaves root without a handler, so before this existed a real question answered end to
    end put *zero* trace lines in the server log.
    """
    app_logger = logging.getLogger("eo_rag")
    existing = list(app_logger.handlers)
    app_logger.handlers = []

    try:
        configure_logging()

        assert app_logger.handlers
        assert app_logger.level == logging.INFO
    finally:
        app_logger.handlers = existing


def test_configure_logging_is_idempotent():
    """`--reload` re-imports the module, and a handler added twice prints every line twice."""
    app_logger = logging.getLogger("eo_rag")
    existing = list(app_logger.handlers)
    app_logger.handlers = []

    try:
        configure_logging()
        configure_logging()
        configure_logging()

        assert len(app_logger.handlers) == 1
    finally:
        app_logger.handlers = existing


def test_the_handler_writes_to_stdout_by_default():
    """What a web server wants, and what `app/main.py` relies on."""
    app_logger = logging.getLogger("eo_rag")
    existing = list(app_logger.handlers)
    app_logger.handlers = []

    try:
        configure_logging()
        assert app_logger.handlers[0].stream is sys.stdout
    finally:
        app_logger.handlers = existing


def test_the_stream_can_be_redirected_to_stderr():
    """
    The MCP stdio transport owns stdout - it is the JSON-RPC channel - so one trace line
    written there corrupts the stream and the client dies with a parse error naming
    nothing useful. `app/mcp/server.py` passes stderr for exactly this reason.
    """
    app_logger = logging.getLogger("eo_rag")
    existing = list(app_logger.handlers)
    app_logger.handlers = []

    try:
        configure_logging(stream=sys.stderr)

        assert app_logger.handlers[0].stream is sys.stderr
        assert app_logger.handlers[0].stream is not sys.stdout
    finally:
        app_logger.handlers = existing


def test_the_trace_logger_still_propagates():
    """
    Turning propagation off would be the natural way to avoid double output, and it would
    make every caplog assertion in this file silently see nothing.
    """
    assert logging.getLogger("eo_rag.trace").propagate is True


# --- import purity ------------------------------------------------------------


def test_importing_the_exporter_builds_no_client(monkeypatch):
    """Same bar as Bedrock, the STAC catalog and Anthropic: nothing at import time."""

    def boom(*args, **kwargs):
        raise AssertionError("Langfuse client built at import time")

    monkeypatch.setattr("app.config.settings.langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr("app.config.settings.langfuse_secret_key", "sk-lf-test")

    importlib.reload(langfuse_exporter)
    monkeypatch.setattr(langfuse_exporter, "_cached_client", None)

    assert langfuse_exporter._cached_client is None

    # And building a Turn does not reach for one either - only recording does.
    importlib.reload(langfuse_exporter)
    assert langfuse_exporter._cached_client is None
    _ = Turn(conversation_id="c", question="q")
    assert langfuse_exporter._cached_client is None
