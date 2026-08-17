"""
Tests for the per-client rate limiter.

Three layers, kept apart: `SlidingWindow` on a fake clock, the middleware over a toy ASGI
app, and one check that the real application actually has it installed. No sleeping - a
limiter tested with `time.sleep` is a slow suite that still cannot exercise the boundary
precisely, so the clock is injected instead.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.api import ratelimit
from app.api.ratelimit import (
    ASK,
    MCP,
    PROXY,
    RateLimitMiddleware,
    SlidingWindow,
    client_key,
    limit_for,
    tier_for,
)
from app.config import settings
from app.main import app as real_app

WINDOW = 60.0


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test drives, so the window boundary is exact."""

    class Clock:
        now = 1000.0

        def advance(self, seconds):
            self.now += seconds

    fake = Clock()
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: fake.now)
    return fake


@pytest.fixture
def window(clock):
    return SlidingWindow(window_seconds=WINDOW, max_tracked=1000)


# --- the window ---------------------------------------------------------------


def test_requests_up_to_the_limit_are_allowed(window):
    assert [window.check("a", 3) for _ in range(3)] == [None, None, None]


def test_the_request_past_the_limit_is_refused(window):
    for _ in range(3):
        window.check("a", 3)

    assert window.check("a", 3) is not None


def test_the_refusal_says_when_the_oldest_hit_expires(window, clock):
    """Which is the moment there is room again - not a fixed backoff."""
    window.check("a", 1)
    clock.advance(20)

    assert window.check("a", 1) == pytest.approx(WINDOW - 20)


def test_room_returns_as_hits_fall_out_of_the_window(window, clock):
    window.check("a", 1)
    assert window.check("a", 1) is not None

    clock.advance(WINDOW + 0.001)
    assert window.check("a", 1) is None


def test_the_window_slides_rather_than_resetting(window, clock):
    """
    The failure a fixed window has: 2 requests at 11:59:59 and 2 more at 12:00:00 is four
    in one second, and a calendar-minute counter calls all four legal. Here the second
    pair is still refused, because both of the first pair are inside the last 60s.
    """
    window.check("a", 2)
    clock.advance(WINDOW - 1)
    window.check("a", 2)

    # Just short of the first hit's expiry: two are still in the window, so no room.
    clock.advance(0.999)
    assert window.check("a", 2) is not None

    # Once it expires there is room for exactly one, not for a fresh pair.
    clock.advance(0.002)
    assert window.check("a", 2) is None
    assert window.check("a", 2) is not None


def test_a_hit_expires_exactly_one_window_after_it_was_made(window, clock):
    """The boundary is half-open: at T + window the hit is already gone, not still held."""
    window.check("a", 1)

    clock.advance(WINDOW)

    assert window.check("a", 1) is None


def test_a_refused_request_does_not_extend_the_lockout(window, clock):
    """
    Otherwise a client hammering the endpoint keeps pushing its own window forward and
    stays locked out long after going quiet - a ban, not a rate limit.
    """
    window.check("a", 1)

    clock.advance(30)
    for _ in range(50):
        window.check("a", 1)

    # The single allowed hit ages out on its own schedule, untouched by the refusals.
    clock.advance(WINDOW - 30 + 0.001)
    assert window.check("a", 1) is None


def test_clients_are_counted_separately(window):
    window.check("a", 1)

    assert window.check("b", 1) is None
    assert window.check("a", 1) is not None


def test_a_limit_of_zero_refuses_everything_without_blowing_up(window):
    """There is no oldest hit to expire, so the wait is a whole window, not an IndexError."""
    assert window.check("a", 0) == pytest.approx(WINDOW)


def test_expired_clients_are_evicted_so_the_table_cannot_grow_forever(clock):
    """The limiter's own bookkeeping is a resource: one request each from a million
    addresses must not be a million deques held for ever."""
    window = SlidingWindow(window_seconds=WINDOW, max_tracked=10)

    for i in range(10):
        window.check(f"old-{i}", 5)

    clock.advance(WINDOW + 1)
    for i in range(10):
        window.check(f"new-{i}", 5)

    assert not any(key.startswith("old-") for key in window._hits)
    assert len(window._hits) == 10


def test_a_client_still_inside_the_window_is_never_evicted(clock):
    """Eviction is by expiry, not by age or by insertion order."""
    window = SlidingWindow(window_seconds=WINDOW, max_tracked=2)

    window.check("busy", 100)
    clock.advance(WINDOW / 2)
    for i in range(20):
        window.check(f"other-{i}", 100)

    assert "busy" in window._hits


# --- tiers --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/ask", ASK),
        ("/ask/stream", ASK),
        ("/mcp", MCP),
        ("/mcp/", MCP),
        ("/preview/S2B_test", PROXY),
        ("/items/S2B_test/assets", PROXY),
        ("/items/S2B_test/assets/red", PROXY),
        # Unlimited on purpose: a healthcheck must answer at any rate, and the built UI
        # is one page load of many small files.
        ("/health", None),
        ("/", None),
        ("/assets/index-abc123.js", None),
    ],
)
def test_paths_map_to_the_intended_tier(path, expected):
    assert tier_for(path) == expected


def test_the_tiers_have_different_limits(monkeypatch):
    """A model call, an MCP handshake and a thumbnail do not cost the same thing."""
    monkeypatch.setattr(settings, "rate_limit_ask_per_minute", 10)
    monkeypatch.setattr(settings, "rate_limit_mcp_per_minute", 60)
    monkeypatch.setattr(settings, "rate_limit_proxy_per_minute", 120)

    assert limit_for(ASK) < limit_for(MCP) < limit_for(PROXY)


def test_mcp_is_not_charged_against_the_ask_budget(monkeypatch):
    """
    One MCP session is many requests that are not tool calls - initialize, tools/list,
    resources/list, resources/templates/list. Sharing /ask's ten-per-minute would mean the
    handshake alone trips the limiter and the client reports a broken server.
    """
    monkeypatch.setattr(settings, "rate_limit_ask_per_minute", 10)
    monkeypatch.setattr(settings, "rate_limit_mcp_per_minute", 60)
    window = SlidingWindow(window_seconds=WINDOW, max_tracked=100)

    # A whole handshake's worth of MCP traffic from one client.
    for _ in range(20):
        assert window.check(f"{MCP}:1.2.3.4", limit_for(MCP)) is None

    # And /ask still has its full allowance.
    assert window.check(f"{ASK}:1.2.3.4", limit_for(ASK)) is None


def test_an_unknown_tier_is_a_KeyError_rather_than_a_guess(monkeypatch):
    """
    A mapping, not a fallback: silently handing an unrecognized tier the proxy allowance
    would be the limiter guessing in the permissive direction.
    """
    with pytest.raises(KeyError):
        limit_for("something-new")


def test_the_tiers_have_separate_budgets(window):
    """Loading a page of quicklooks must not consume the allowance for questions."""
    assert window.check(f"{PROXY}:1.2.3.4", 1) is None
    assert window.check(f"{ASK}:1.2.3.4", 1) is None


# --- who gets counted ---------------------------------------------------------


def scope(client=("1.2.3.4", 5000), headers=()):
    return {"type": "http", "path": "/ask", "client": client, "headers": headers}


def test_the_peer_address_is_the_key_by_default():
    assert client_key(scope()) == "1.2.3.4"


def test_a_forwarded_header_is_ignored_unless_a_proxy_is_declared(monkeypatch):
    """
    The important one. A header the client sets is a header the client can forge, so
    trusting it unconditionally would let anyone opt out by varying it per request.
    """
    monkeypatch.setattr(settings, "rate_limit_trust_proxy_header", False)
    forged = scope(headers=[(b"x-forwarded-for", b"9.9.9.9")])

    assert client_key(forged) == "1.2.3.4"


def test_the_rightmost_forwarded_entry_is_used_when_trusted(monkeypatch):
    """
    A proxy appends the peer it actually saw, so the rightmost entry is the one it wrote
    and everything left of it is whatever the client chose to send.
    """
    monkeypatch.setattr(settings, "rate_limit_trust_proxy_header", True)
    spoofed = scope(headers=[(b"x-forwarded-for", b"9.9.9.9, 203.0.113.7")])

    assert client_key(spoofed) == "203.0.113.7"


def test_a_connection_without_a_peer_falls_back_to_one_shared_bucket():
    """Unattributable traffic is still traffic; it must not be unlimited."""
    assert client_key(scope(client=None)) == "unknown"


# --- the middleware -----------------------------------------------------------


@pytest.fixture
def limited(monkeypatch, clock):
    """A toy app behind the middleware, with its own window and a configurable limit."""

    def _build(limit=2):
        monkeypatch.setattr(settings, "rate_limit_enabled", True)
        monkeypatch.setattr(settings, "rate_limit_ask_per_minute", limit)
        monkeypatch.setattr(settings, "rate_limit_window_seconds", WINDOW)

        toy = FastAPI()

        @toy.post("/ask")
        def ask():
            return {"answer": "served"}

        @toy.get("/health")
        def health():
            return {"status": "ok"}

        @toy.post("/ask/stream")
        def stream():
            def frames():
                yield "data: one\n\n"
                yield "data: two\n\n"

            return StreamingResponse(frames(), media_type="text/event-stream")

        toy.add_middleware(
            RateLimitMiddleware,
            window=SlidingWindow(window_seconds=WINDOW, max_tracked=100),
        )
        return TestClient(toy)

    return _build


def test_requests_within_the_limit_are_served_normally(limited):
    client = limited(limit=2)

    assert client.post("/ask").status_code == 200
    assert client.post("/ask").json() == {"answer": "served"}


def test_the_request_past_the_limit_is_a_429(limited):
    client = limited(limit=1)
    client.post("/ask")

    response = client.post("/ask")

    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["detail"]


def test_the_refusal_carries_a_retry_after_the_client_can_act_on(limited, clock):
    client = limited(limit=1)
    client.post("/ask")
    clock.advance(20)

    response = client.post("/ask")

    # Whole seconds, and the time until there is genuinely room again.
    assert response.headers["retry-after"] == str(round(WINDOW - 20))


def test_retry_after_is_never_zero(limited, clock):
    """A client told to wait 0s retries immediately into another refusal."""
    client = limited(limit=1)
    client.post("/ask")
    clock.advance(WINDOW - 0.1)

    assert int(client.post("/ask").headers["retry-after"]) >= 1


def test_an_untiered_path_is_never_refused(limited):
    client = limited(limit=1)

    assert [client.get("/health").status_code for _ in range(10)] == [200] * 10


def test_the_streaming_endpoint_still_streams_through_the_middleware(limited):
    """
    The reason this is raw ASGI and not BaseHTTPMiddleware: that base class re-emits the
    response through a memory stream, which puts it in the path of the one endpoint built
    around a long-lived body and a client that may vanish mid-flight.
    """
    client = limited(limit=5)

    with client.stream("POST", "/ask/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # The first frame, without draining the rest: what a wrapping middleware would
        # break is precisely the arrival of frame one before the body has finished.
        assert next(iter(response.iter_lines())) == "data: one"


def test_disabling_the_limiter_lets_everything_through(limited, monkeypatch):
    client = limited(limit=1)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)

    assert [client.post("/ask").status_code for _ in range(5)] == [200] * 5


def test_the_limit_is_read_per_request_not_captured_at_startup(limited, monkeypatch):
    """So configuration changes take effect without rebuilding the middleware."""
    client = limited(limit=1)
    client.post("/ask")
    assert client.post("/ask").status_code == 429

    monkeypatch.setattr(settings, "rate_limit_ask_per_minute", 100)
    assert client.post("/ask").status_code == 200


# --- wiring -------------------------------------------------------------------


def test_the_real_application_has_the_limiter_installed(monkeypatch):
    """
    The middleware being correct is worth nothing if `app/main.py` never adds it. Uses the
    real app, with the limit set to refuse outright so no request has to be spent finding
    the boundary - and so this cannot depend on what other tests left in the window.
    """
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_ask_per_minute", 0)

    response = TestClient(real_app).post("/ask", json={"question": "q"})

    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_the_limiter_sits_outside_the_router_and_its_dependencies(monkeypatch):
    """
    Refused before routing, so a request that will be turned away never opens a database
    session and never reaches a model. A 422 here would mean the body was parsed first.
    """
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_ask_per_minute", 0)

    # No `question` field: invalid, and it must still be the rate limit that answers.
    assert TestClient(real_app).post("/ask", json={}).status_code == 429


def test_health_is_never_rate_limited_on_the_real_app(monkeypatch):
    """The container healthcheck polls it; a limiter that trips it takes the app down."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_ask_per_minute", 0)
    monkeypatch.setattr(settings, "rate_limit_proxy_per_minute", 0)

    with TestClient(real_app) as client:
        assert [client.get("/health").status_code for _ in range(5)] == [200] * 5
