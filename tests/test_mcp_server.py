"""
Tests for the MCP server itself: registration, dispatch, and the FastAPI mount.

The only file in the suite that needs the optional `mcp` extra, hence the module-level
`importorskip`. Everything that can be tested without the SDK deliberately is, next door in
`tests/test_mcp_tools.py` and `tests/test_mcp_resources.py`.

    uv run --extra dev --extra mcp pytest -q

Still offline: the SDK is a protocol implementation, not a network client, and every tool
it dispatches to is faked here.
"""

import asyncio
import importlib
import shutil
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the optional `mcp` extra is not installed")

import httpx
from fastapi.testclient import TestClient

from app import main as app_main
from app.mcp import server as mcp_server
from app.mcp import tools as mcp_tools
from app.tools.compute_index import COMPUTE_INDEX_TOOL
from app.tools.rag_lookup import RAG_LOOKUP_TOOL, LookupResult
from app.tools.stac_search import (
    STAC_SEARCH_TOOL,
    ItemSummary,
    SearchResult,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mcp():
    return mcp_server.mcp


# --- registration --------------------------------------------------------------


def test_the_three_tools_are_registered_under_claudes_names(mcp):
    """
    One implementation, two front ends, one set of names - so a trace does not have to say
    which door a call came through.
    """
    names = {t.name for t in run(mcp.list_tools())}

    assert names == {
        STAC_SEARCH_TOOL["name"],
        COMPUTE_INDEX_TOOL["name"],
        RAG_LOOKUP_TOOL["name"],
    }


def test_the_generated_schemas_match_the_hand_written_ones(mcp):
    """
    The drift guard at the protocol level. `tests/test_mcp_tools.py` compares signatures;
    this compares what a client is actually sent.
    """
    generated = {t.name: t for t in run(mcp.list_tools())}

    for schema in (STAC_SEARCH_TOOL, COMPUTE_INDEX_TOOL, RAG_LOOKUP_TOOL):
        properties = set(generated[schema["name"]].input_schema["properties"])
        expected = set(schema["input_schema"]["properties"])
        # include_geometry is the one deliberate addition; see tests/test_mcp_tools.py.
        assert properties - {"include_geometry"} == expected, schema["name"]
        assert set(generated[schema["name"]].input_schema["required"]) == set(
            schema["input_schema"]["required"]
        )


def test_every_argument_carries_its_description(mcp):
    """
    The descriptions were tuned against the live catalog - the allowlist, the bare-date
    guidance - and a client that gets thinner ones makes the mistakes those sentences exist
    to prevent. `Annotated[..., Field(description=...)]` is what puts them in the schema.
    """
    for tool in run(mcp.list_tools()):
        for name, spec in tool.input_schema["properties"].items():
            assert spec.get("description"), f"{tool.name}.{name} has no description"


def test_the_index_enum_reaches_the_client(mcp):
    """`Literal` is how the SDK is told what COMPUTE_INDEX_TOOL writes by hand."""
    compute = {t.name: t for t in run(mcp.list_tools())}["compute_index"]

    assert set(compute.input_schema["properties"]["index"]["enum"]) == {"ndvi", "ndwi"}


def test_the_tools_declare_an_output_schema(mcp):
    """Pydantic returns give structured content for free; a str return would not."""
    schemas = {t.name: t.output_schema for t in run(mcp.list_tools())}

    assert schemas["stac_search"] is not None
    assert schemas["compute_index"] is not None


def test_one_static_resource_and_two_templates(mcp):
    """
    MCP lists a template's shape, never its values, so the index is the entry point and
    has to be the thing `resources/list` returns.
    """
    assert [str(r.uri) for r in run(mcp.list_resources())] == ["docs://sources"]
    assert sorted(t.uri_template for t in run(mcp.list_resource_templates())) == [
        "docs://document/{source}",
        "docs://section/{source}/{section}",
    ]


def test_the_server_tells_a_client_where_to_start(mcp):
    """The instructions are the only place the docs:// scheme is explained to a client."""
    assert "docs://sources" in mcp.instructions


# --- dispatch -------------------------------------------------------------------


def test_a_tool_call_returns_both_text_and_structured_content(mcp, monkeypatch):
    """
    Both, from one return value - which is why the geometry decision is a single choice
    rather than one per consumer.
    """
    monkeypatch.setattr(
        mcp_tools,
        "stac_search",
        lambda **kw: SearchResult(
            count=1,
            limit=10,
            items=[ItemSummary(id="S2B_x", collection="sentinel-2-l2a", bbox=[12.0, 41.0, 13.0, 42.0])],
        ),
    )

    result = run(mcp.call_tool("stac_search", {"bbox": [12.0, 41.0, 13.0, 42.0]}))

    assert result.structured_content["items"][0]["id"] == "S2B_x"
    assert "S2B_x" in result.content[0].text
    assert result.is_error is False


def test_a_tool_error_reaches_the_client_as_its_message(mcp, monkeypatch):
    """
    A malformed bbox is something the caller can fix, and the message says how. It must
    not arrive as a traceback or a generic failure.
    """
    def _boom(**kwargs):
        raise ValueError("bbox south (42.0) must be below north (41.0)")

    monkeypatch.setattr(mcp_tools, "stac_search", _boom)

    with pytest.raises(Exception, match="below north"):
        run(mcp.call_tool("stac_search", {"bbox": [12.0, 42.0, 13.0, 41.0]}))


def test_rag_lookup_arrives_as_prose_not_json(mcp, monkeypatch):
    """The one tool whose result is text, so the [Source: ...] labels are readable."""
    monkeypatch.setattr(mcp_tools, "SessionLocal", lambda: _NullSession())
    monkeypatch.setattr(
        mcp_tools,
        "rag_lookup",
        lambda db, query: LookupResult(context="[Source: stac-spec-core - Item fields]\nText."),
    )

    result = run(mcp.call_tool("rag_lookup", {"query": "items"}))

    assert result.content[0].text.startswith("[Source: stac-spec-core - Item fields]")
    assert not result.content[0].text.startswith("{")


class _NullSession:
    def close(self):
        pass


# --- the mount ------------------------------------------------------------------

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2026-07-28",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def started_app():
    """
    A freshly built app, started, as a factory.

    Rebuilt per use because **a session manager can only be run once per instance** - the
    SDK says so outright, and reusing one raises "run() can only be called once per
    instance". A module-level MCP server plus more than one `with TestClient(...)` in a
    process is therefore a trap, and reloading both modules is what sidesteps it: a new
    MCPServer gets a new session manager.

    The base_url carries a **port** on purpose. The SDK's default allowlist is
    `["127.0.0.1:*", "localhost:*", "[::1]:*"]`, and those patterns require one: a bare
    `Host: 127.0.0.1` does not match `127.0.0.1:*` and is refused. TestClient's own default
    of `http://testserver` is refused too - which is precisely the production failure the
    MCP_ALLOWED_HOSTS setting exists for, and which one test below exercises deliberately.
    """
    clients = []

    def _build(base_url="http://127.0.0.1:8000"):
        importlib.reload(mcp_server)
        reloaded = importlib.reload(app_main)
        client = TestClient(reloaded.app, base_url=base_url)
        clients.append(client)
        return client

    try:
        yield _build
    finally:
        importlib.reload(mcp_server)
        importlib.reload(app_main)


def test_the_endpoint_is_at_mcp_and_not_at_mcp_mcp(started_app):
    """
    The regression guard for the trap this mount was written around:
    `streamable_http_app()` already serves at /mcp inside its own app, so mounting that at
    /mcp without `streamable_http_path="/"` puts the real endpoint at **/mcp/mcp** and
    leaves /mcp answering 404 with nothing to explain it.

    `with TestClient(...)` and not the bare form: the lifespan has to run, or the session
    manager is never started and every request fails "Task group is not initialized". The
    other test files use the bare form and are unaffected because they never touch /mcp.
    """
    with started_app() as client:
        at_mcp = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        at_double = client.post("/mcp/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert at_mcp.status_code == 200, at_mcp.text
    assert at_double.status_code == 404


def test_bare_mcp_redirects_to_the_slashed_form(started_app):
    """
    Pinned because it is real and mildly surprising: the endpoint lives at the mounted
    app's root, and `Mount("/mcp")` compiles to `^/mcp(?P<path>/.*)$`, which does not match
    `/mcp` itself. The explicit route in `app/main.py` is what turns that into a 307 the
    SDK's client follows transparently.
    """
    with started_app() as client:
        redirected = client.post(
            "/mcp", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False
        )
        slashed = client.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)

    assert redirected.status_code == 307
    assert redirected.headers["location"].endswith("/mcp/")
    assert slashed.status_code == 200, slashed.text


@pytest.fixture
def frontend_build():
    """
    A real `frontend_dist/` for the duration of one test.

    It has to be the real path rather than a monkeypatched one: `app/main.py` computes
    `_UI` and decides whether to mount at *import*, and `started_app` reloads the module -
    so a patched attribute would be recomputed away before the app was built.

    Removed afterwards, and only if this fixture created it, so a developer who really has
    run `npm run build` keeps their build.
    """
    build = Path(app_main.__file__).resolve().parent.parent / "frontend_dist"
    created = not build.exists()

    if created:
        build.mkdir(parents=True)
        (build / "index.html").write_text("<html></html>", encoding="utf-8")

    try:
        yield build
    finally:
        if created:
            shutil.rmtree(build, ignore_errors=True)


def test_the_bare_path_survives_a_frontend_build(frontend_build, started_app):
    """
    The regression test for a bug no other test could see, because none of them has a
    frontend build and every deployed image does.

    With `frontend_dist/` present, StaticFiles is mounted at `/` and matches `/mcp` before
    the router's `redirect_slashes` ever gets a chance - answering **405**, since it serves
    GET and HEAD only. Measured in the container before the fix: `POST /mcp` 405, `POST
    /mcp/` 200. The explicit route is what makes the bare path work in the image as well as
    in a checkout.
    """
    with started_app() as client:
        # Sanity: the UI really is mounted in this build, or the test proves nothing.
        assert client.get("/").status_code == 200
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 200


def test_the_handshake_actually_succeeds(started_app):
    """A 200 is not enough - the body has to be a real initialize result."""
    with started_app() as client:
        response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    # The transport answers SSE by default, so the JSON rides in a `data:` line.
    assert "eo-rag" in response.text
    assert "protocolVersion" in response.text


def test_the_mount_does_not_shadow_the_rest_of_the_api(started_app):
    """/health and the router keep their paths; the same property the UI mount has."""
    with started_app() as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/ask", json={}).status_code == 422


def test_a_foreign_host_header_is_refused_by_default(started_app):
    """
    DNS-rebinding protection, on by default and right for a laptop. It is also the thing
    that will be reported as "the MCP server is broken in production", so it is pinned
    here: behind any real hostname this is a 4xx until MCP_ALLOWED_HOSTS says otherwise.
    """
    with started_app(base_url="http://evil.example:8000") as client:
        response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code >= 400
    assert "Host" in response.text


def test_a_localhost_host_without_a_port_is_also_refused(started_app):
    """
    The subtlety inside the subtlety: the default patterns are `127.0.0.1:*` and
    `localhost:*`, and `*` does not match nothing - so a request that reaches the app on
    port 80, where browsers and curl omit the port from Host, is refused even though it
    *is* localhost. Behind a reverse proxy on 80 or 443, MCP_ALLOWED_HOSTS is not optional.
    """
    with started_app(base_url="http://localhost") as client:
        response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code >= 400


def test_the_allowlist_lets_a_real_hostname_through(started_app, monkeypatch):
    """The escape hatch, and the reason the setting exists at all."""
    from app.config import settings

    monkeypatch.setattr(settings, "mcp_allowed_hosts", ["mcp.example.test"])

    with started_app(base_url="http://mcp.example.test") as client:
        response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 200, response.text


def test_the_app_starts_without_the_extra(started_app, monkeypatch):
    """
    The other half of "the suite passes with and without": an image built without the mcp
    extra must still serve the API, with /mcp simply absent.
    """
    monkeypatch.setattr("app.mcp.load_mcp_server", lambda: None)

    with started_app() as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 404


def test_the_switch_turns_the_mount_off(started_app, monkeypatch):
    """MCP_HTTP_ENABLED=false, for a deployment that wants the stdio transport only."""
    from app.config import settings

    monkeypatch.setattr(settings, "mcp_http_enabled", False)

    with started_app() as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 404


# --- import purity ---------------------------------------------------------------


def test_building_the_server_opens_nothing(monkeypatch):
    """
    Registering tools is filling a registry, not connecting to anything - and the same bar
    applies here as everywhere else in `app/`. This is what keeps it true.
    """

    def boom(*args, **kwargs):
        raise AssertionError("a client or session was created at import time")

    monkeypatch.setattr(httpx, "Client", boom)
    monkeypatch.setattr("boto3.client", boom)
    monkeypatch.setattr("app.db.session.SessionLocal", boom)

    reloaded = importlib.reload(mcp_server)

    assert {t.name for t in run(reloaded.mcp.list_tools())} == {
        "stac_search",
        "compute_index",
        "rag_lookup",
    }
