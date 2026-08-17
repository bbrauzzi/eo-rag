from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.ratelimit import RateLimitMiddleware
from app.api.routes import router
from app.config import settings
from app.mcp import load_mcp_server
from app.obs.tracing import configure_logging

# Before the app exists, so nothing that runs at startup traces into a dropped record.
# Uvicorn leaves the root logger without a handler, so without this the per-turn trace is
# written and then discarded - which is indistinguishable from not being written at all.
configure_logging()

# None when the optional `mcp` extra is not installed, or when the switch is off. Resolved
# here rather than inside the lifespan because the mount below needs it at import time.
_MCP = load_mcp_server() if settings.mcp_http_enabled else None


def _transport_security():
    """
    Host and origin validation for the MCP HTTP transport, or None to keep the default.

    The SDK refuses any Host but localhost unless told otherwise - DNS-rebinding protection,
    and exactly right for a laptop. Behind a real hostname it refuses everything, and the
    symptom reads as a broken server rather than as a policy, which is why the settings
    exist and why this returns None when they are empty: not configuring anything must
    leave the safe default in place rather than replacing it with an empty allowlist.
    """
    if not (settings.mcp_allowed_hosts or settings.mcp_allowed_origins):
        return None

    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=settings.mcp_allowed_origins,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Run the MCP session manager for as long as the app is up.

    Without this every request to /mcp fails with `Task group is not initialized`, and
    nothing in that message points at a missing lifespan. The session manager is created
    lazily by `streamable_http_app()`, so this may only touch it *after* the mount below
    has run - which it does, since the mount happens at import and this at startup.
    """
    if _MCP is None:
        yield
        return

    async with _MCP.session_manager.run():
        yield


app = FastAPI(
    title="EO-RAG",
    description="Conversational assistant over EO/STAC technical documentation, with RAG and tool calling",
    version="0.1.0",
    lifespan=lifespan,
)

# Outermost, so an over-rate request is refused before the router resolves it, before the
# DB session dependency opens a connection for it, and before any of it reaches a model.
# `add_middleware` passes the class, not an instance: Starlette builds it when the app
# starts, which is what keeps the window out of import time like every client here.
app.add_middleware(RateLimitMiddleware)

app.include_router(router)

if _MCP is not None:
    # A bare `/mcp` sent to the slashed form, and this is not cosmetic. Starlette's
    # `Mount("/mcp")` compiles to `^/mcp(?P<path>/.*)$`, which does **not** match `/mcp`
    # itself - only `/mcp/...`. With nothing else mounted, the router's own
    # `redirect_slashes` quietly covers that up; with the built UI mounted at `/`,
    # StaticFiles matches `/mcp` first and answers **405 Method Not Allowed**, because it
    # serves GET and HEAD and nothing else.
    #
    # So this only breaks in an image that has a frontend build - which is every deployed
    # one, and no test run. Measured: `POST /mcp` was 405 in the container while `/mcp/`
    # was 200. The redirect is registered before both mounts so it wins outright.
    async def _mcp_root(_request):
        # 307 keeps the method and the body, which a JSON-RPC POST needs.
        return RedirectResponse("/mcp/", status_code=307)

    # Starlette's router rather than a FastAPI decorator: this is transport plumbing, not
    # part of the documented API, and it has to answer the three methods the streamable
    # transport uses - POST for requests, GET for the event stream, DELETE to end a session.
    app.router.add_route("/mcp", _mcp_root, methods=["GET", "POST", "DELETE"], include_in_schema=False)

    # `streamable_http_path="/"` and not the default: `streamable_http_app()` already
    # serves the endpoint at /mcp within its own app, so mounting that at /mcp would put
    # the real endpoint at **/mcp/mcp** and leave /mcp answering 404 with nothing to say
    # why. Serving it at the sub-app's root makes the mount point the whole address.
    app.mount(
        "/mcp",
        _MCP.streamable_http_app(
            streamable_http_path="/", transport_security=_transport_security()
        ),
    )

# The built frontend, when there is one. Mounted after the router so /health, /ask and
# /ask/stream keep their paths, and after /mcp for the same reason: at "/" StaticFiles
# claims every path the router did not, so anything mounted later is unreachable and
# presents as a missing file. Only if the directory exists: `npm run build` runs in the
# image's node stage, so a checkout that has never been built - which is every test run,
# and the local `uvicorn app.main:app --reload` workflow - must still start.
_UI = Path(__file__).resolve().parent.parent / "frontend_dist"

if _UI.is_dir():
    app.mount("/", StaticFiles(directory=_UI, html=True), name="ui")
