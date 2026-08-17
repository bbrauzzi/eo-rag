"""
Per-client request rate limiting.

A different guardrail from the conversation budget in `app/agents/graph.py`, and they do
not overlap: the budget bounds *one conversation* and is keyed on a `conversation_id` the
client chooses, so a caller that never sends one is never bounded by it. This is keyed on
the caller instead, and is the thing that survives someone sending a fresh conversation id
every request.

## Sliding window, not fixed window

Each client's recent request timestamps are kept in a deque and pruned to the window on
every check. A fixed window ("50 requests per calendar minute") is cheaper and wrong at
the boundary: 50 requests at 11:59:59 and 50 more at 12:00:00 is 100 requests in one
second, both windows perfectly legal. The deque costs one timestamp per allowed request,
which the limit itself bounds - a client at 10/minute is holding ten floats.

## A refused request is not recorded

`check` appends only when it allows. Recording refusals too would let a client hammering
the endpoint keep pushing its own window forward and lock itself out indefinitely, long
after it had gone quiet - the limiter would stop being a rate limit and become a ban.

## In-process, and what that costs

The counters live in this process and die with it. Restarting the API forgives everyone,
and a second worker keeps its own tally, so N workers means an effective limit of N times
`limit`. That is the accepted trade for adding no dependency and no schema; moving to
Redis or Postgres is what changes it, and nothing else here would have to change - only
`SlidingWindow`.

## The dict is itself a resource

An unbounded map keyed by client address is a memory exhaustion vector: one request each
from a million addresses is a million deques. `_evict` drops keys whose windows have
fully expired once the map grows past `max_tracked`, so the memory ceiling is the number
of clients *currently inside the window*, not the number ever seen.
"""

import time
from collections import deque
from threading import Lock

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings

# Paths whose prefix decides the tier, longest first so a more specific prefix wins.
# Anything not matched here is unlimited on purpose: /health has to answer container
# healthchecks at any rate, and the static UI is one page load of many small files.
ASK = "ask"
PROXY = "proxy"
MCP = "mcp"

TIERS: tuple[tuple[str, str], ...] = (
    ("/ask", ASK),
    ("/mcp", MCP),
    ("/preview/", PROXY),
    ("/items/", PROXY),
)


class SlidingWindow:
    """
    Counts recent hits per key. Pure and clock-driven; knows nothing about HTTP.

    Uses `time.monotonic` rather than wall time: a limiter measures elapsed intervals, and
    wall time can step backwards over an NTP correction or a DST change, which would hand
    out a window's worth of free requests or lock everyone out for the duration.
    """

    def __init__(self, window_seconds: float, max_tracked: int):
        self.window_seconds = window_seconds
        self.max_tracked = max_tracked
        self._hits: dict[str, deque[float]] = {}
        # FastAPI runs sync endpoints in a threadpool, and this middleware runs on the
        # event loop; the lock is what makes "prune, count, append" one step rather than
        # three interleaved ones. It is only ever held across plain memory operations -
        # never across an await - so it cannot stall the loop.
        self._lock = Lock()

    def check(self, key: str, limit: int) -> float | None:
        """
        Allow or refuse one request.

        Returns None when allowed, or the seconds to wait when refused - which the caller
        sends as `Retry-After`. That is the age of the oldest hit still in the window: the
        moment it falls out is the moment there is room again.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                # Deliberately no append: see the module docstring on refusals.
                # `hits` can be empty here when the limit is 0, which means "refuse
                # everything" - there is no oldest hit to expire, so the wait is a whole
                # window rather than an IndexError.
                oldest = hits[0] if hits else now
                return max(0.0, oldest + self.window_seconds - now)

            hits.append(now)

            if len(self._hits) > self.max_tracked:
                self._evict(cutoff)

            return None

    def _evict(self, cutoff: float) -> None:
        """Drop clients whose whole window has expired. Called with the lock held."""
        self._hits = {
            key: hits for key, hits in self._hits.items() if hits and hits[-1] > cutoff
        }


def tier_for(path: str) -> str | None:
    """Which tier a path belongs to, or None for the unlimited ones."""
    for prefix, tier in TIERS:
        if path == prefix or path.startswith(prefix):
            return tier

    return None


def limit_for(tier: str) -> int:
    """
    The per-window allowance for a tier, read fresh so configuration changes take effect
    without rebuilding the middleware.

    A mapping rather than a conditional now that there are three: with two, `if ASK else`
    read fine; with three it would silently give an unrecognized tier the proxy allowance,
    which is the wrong direction for a limiter to guess in.
    """
    return {
        ASK: settings.rate_limit_ask_per_minute,
        MCP: settings.rate_limit_mcp_per_minute,
        PROXY: settings.rate_limit_proxy_per_minute,
    }[tier]


def client_key(scope: Scope) -> str:
    """
    Who to count this request against.

    The peer address by default. `X-Forwarded-For` is used **only** when
    `RATE_LIMIT_TRUST_PROXY_HEADER` says a proxy is in front, because a header the client
    can set is a header the client can forge: trusting it unconditionally turns the
    limiter off for anyone who sends a different value each request.

    When it is trusted, the **rightmost** entry is the one taken. A proxy appends the peer
    it actually saw, so everything to the left of that is whatever the client chose to
    send. This assumes exactly one trusted hop, which is the deployment the setting
    describes; behind two, the second from the right is the real client.
    """
    if settings.rate_limit_trust_proxy_header:
        for name, value in scope.get("headers") or ():
            if name == b"x-forwarded-for":
                forwarded = value.decode("latin-1").split(",")
                if forwarded and forwarded[-1].strip():
                    return forwarded[-1].strip()

    client = scope.get("client")

    # None for a connection with no peer address (an ASGI test transport, a unix socket).
    # One shared bucket is the safe reading: unattributable traffic is still traffic.
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    """
    Refuses over-rate requests before they reach the router.

    Written as raw ASGI rather than Starlette's `BaseHTTPMiddleware` on purpose. That base
    class pipes the response through a memory stream to re-emit it, which puts it directly
    in the path of `/ask/stream` - the one endpoint whose whole design is a long-lived
    body and a client that may vanish mid-flight, and around which
    `_repair_interrupted_turn` is built. This class only ever reads the request scope, and
    either answers 429 itself or calls the app and gets out of the way, so the streaming
    and disconnect behaviour of every endpoint is exactly what it was without it.
    """

    def __init__(self, app: ASGIApp, window: SlidingWindow | None = None):
        self.app = app
        self.window = window or SlidingWindow(
            window_seconds=settings.rate_limit_window_seconds,
            max_tracked=settings.rate_limit_max_tracked_clients,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Websockets and the lifespan scope have no rate to limit.
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            return await self.app(scope, receive, send)

        tier = tier_for(scope["path"])
        if tier is None:
            return await self.app(scope, receive, send)

        retry_after = self.window.check(f"{tier}:{client_key(scope)}", limit_for(tier))
        if retry_after is None:
            return await self.app(scope, receive, send)

        seconds = max(1, round(retry_after))
        response = JSONResponse(
            status_code=429,
            content={
                "detail": (
                    f"Rate limit exceeded: at most {limit_for(tier)} requests per "
                    f"{settings.rate_limit_window_seconds:g}s. Retry in {seconds}s."
                )
            },
            # Rounded up to a whole second and never zero: the header is integer seconds,
            # and a client told to retry in 0 retries immediately into another refusal.
            headers={"Retry-After": str(seconds)},
        )

        await response(scope, receive, send)
