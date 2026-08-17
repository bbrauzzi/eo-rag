"""
Tests for the /ask/stream endpoint: framing only.

Same division as tests/test_ask.py - the graph is faked, so what is under test here is
the SSE envelope and nothing else. The events themselves are covered in
tests/test_agent_graph.py.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.agents.graph import ConversationBudgetExceeded
from app.api import routes
from app.db.session import get_db
from app.main import app

SCRIPT = [
    {"type": "start", "conversation_id": "c1"},
    {"type": "token", "text": "Two "},
    {"type": "tool_start", "id": "tu_1", "name": "stac_search", "input": {"limit": 2}},
    {"type": "tool_end", "id": "tu_1", "name": "stac_search", "ok": True, "ms": 812, "detail": None},
    {"type": "features", "collection": {"type": "FeatureCollection", "features": []}},
    {"type": "done", "answer": "Two scenes.", "sources": ["a.md"], "steps": 2},
]


@pytest.fixture
def db_sentinel():
    sentinel = object()
    app.dependency_overrides[get_db] = lambda: sentinel
    yield sentinel
    app.dependency_overrides.clear()


@pytest.fixture
def client(db_sentinel):
    return TestClient(app)


@pytest.fixture(autouse=True)
def fake_budget(monkeypatch):
    """
    Neutralises the pre-response budget check by default, and lets a test arm it.

    Autouse because the route now calls it on every request, and this file is about the
    SSE envelope: without this the framing tests would reach into the real graph to ask
    what a thread has spent. Its own behaviour is covered in tests/test_agent_graph.py.
    """

    def _install(error=None):
        def _check(conversation_id):
            if error is not None:
                raise error

        monkeypatch.setattr(routes, "check_conversation_budget", _check)

    _install()
    return _install


@pytest.fixture
def fake_stream(monkeypatch):
    """Patches stream_answer() and records what the route passes to it."""

    def _install(events=None, error=None):
        calls: list[dict] = []

        def _stream_answer(db, question, conversation_id=None):
            calls.append({"db": db, "question": question, "conversation_id": conversation_id})
            yield from events if events is not None else SCRIPT
            if error is not None:
                raise error

        monkeypatch.setattr(routes, "stream_answer", _stream_answer)
        return calls

    return _install


def events_of(body: str) -> list[dict]:
    """Parse the body back into the objects the route was handed."""
    return [json.loads(f.removeprefix("data: ")) for f in body.split("\n\n") if f]


def test_the_response_is_an_event_stream(client, fake_stream):
    fake_stream()

    response = client.post("/ask/stream", json={"question": "q"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_every_event_arrives_as_one_data_line(client, fake_stream):
    """One JSON object per frame, type inside it: what keeps the client parser trivial."""
    fake_stream()

    body = client.post("/ask/stream", json={"question": "q"}).text

    frames = [f for f in body.split("\n\n") if f]
    assert all(f.startswith("data: ") and "\n" not in f for f in frames)
    assert [json.loads(f.removeprefix("data: ")) for f in frames] == SCRIPT


def test_the_conversation_id_arrives_first(client, fake_stream):
    """Settled before the graph runs, so a stream that dies halfway is still resumable."""
    fake_stream()

    first = events_of(client.post("/ask/stream", json={"question": "q"}).text)[0]

    assert first["type"] == "start"
    assert first["conversation_id"] == "c1"


def test_the_step_count_rides_on_the_final_event(client, fake_stream):
    """Deliberately unlike /ask, whose response shape stays what step 2 defined."""
    fake_stream()

    last = events_of(client.post("/ask/stream", json={"question": "q"}).text)[-1]

    assert last["type"] == "done"
    assert set(last) == {"type", "answer", "sources", "steps"}


def test_the_question_and_the_session_reach_the_graph(client, fake_stream, db_sentinel):
    calls = fake_stream()

    client.post("/ask/stream", json={"question": "What are STAC Items?"})

    assert calls == [
        {"db": db_sentinel, "question": "What are STAC Items?", "conversation_id": None}
    ]


def test_a_conversation_id_is_passed_through(client, fake_stream):
    calls = fake_stream()

    client.post("/ask/stream", json={"question": "q", "conversation_id": "c9"})

    assert calls[0]["conversation_id"] == "c9"


def test_a_failure_mid_stream_becomes_an_error_frame(client, fake_stream):
    """
    The status line went out with the first frame, so a 500 is no longer available.
    The events already sent stay valid and the client is told why it stopped.
    """
    fake_stream(error=RuntimeError("the catalog went away"))

    events = events_of(client.post("/ask/stream", json={"question": "q"}).text)

    assert events[:-1] == SCRIPT
    assert events[-1] == {"type": "error", "message": "the catalog went away"}


def test_newlines_and_non_ascii_survive_the_framing(client, fake_stream):
    """A frame must stay one line, and an answer is arbitrary prose."""
    token = {"type": "token", "text": "Sentinel-2 copre Roma:\n- una scena\n"}
    fake_stream(events=[token])

    body = client.post("/ask/stream", json={"question": "q"}).text

    assert body.count("\n\n") == 1
    assert events_of(body)[0] == token


def test_the_session_is_still_open_while_the_frames_are_produced(monkeypatch):
    """
    The hazard of streaming from behind a yield-dependency: if FastAPI unwound the
    dependency stack when the route function returned, every frame after the first
    would be written by a graph holding a closed Session.

    Asserted where it happens - inside the generator, one snapshot per event - rather
    than by the order of a log, because TestClient buffers the body and would make any
    ordering look right.
    """

    class RecordingSession:
        closed = False

    def recording_db():
        session = RecordingSession()
        try:
            yield session
        finally:
            session.closed = True

    seen: list[bool] = []

    def _stream_answer(db, question, conversation_id=None):
        for event in SCRIPT:
            seen.append(db.closed)
            yield event

    monkeypatch.setattr(routes, "stream_answer", _stream_answer)
    app.dependency_overrides[get_db] = recording_db

    try:
        response = TestClient(app).post("/ask/stream", json={"question": "q"})
    finally:
        app.dependency_overrides.clear()

    assert len(events_of(response.text)) == len(SCRIPT)
    assert seen == [False] * len(SCRIPT)


def test_a_missing_question_is_rejected_before_anything_streams(client, fake_stream):
    calls = fake_stream()

    assert client.post("/ask/stream", json={}).status_code == 422
    assert calls == []


# --- the conversation budget ------------------------------------------------


def test_a_conversation_over_its_budget_is_a_429_not_an_error_frame(
    client, fake_stream, fake_budget
):
    """
    The reason the check happens in the route rather than inside the generator: once the
    first frame is out, the 200 has been sent and a refusal could only be reported inside
    it. Checked before the response exists, a limit can still say so with a status code.
    """
    calls = fake_stream()
    fake_budget(error=ConversationBudgetExceeded("This conversation has reached its budget."))

    response = client.post("/ask/stream", json={"question": "q", "conversation_id": "spent"})

    assert response.status_code == 429
    assert "budget" in response.json()["detail"]
    # And the stream was never started: no frames, no graph invocation.
    assert calls == []
    assert response.headers["content-type"].startswith("application/json")
