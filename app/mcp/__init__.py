"""
The MCP server: this project's tools, reachable by something other than its own agent.

Four modules, and only one of them imports the `mcp` package:

    tools.py      the three tools as plain functions, SDK-free
    resources.py  the documentation as plain functions, SDK-free
    server.py     the registration, and the stdio entry point. Imports `mcp`.
    __init__.py   this file: the guarded import the FastAPI app uses

The split is the same one as `app/tools/*` against `app/agents/graph.py` - the tool is a
function, the framework binding lives elsewhere - and here it also pays for itself directly.
`mcp` is an optional extra, so keeping the adapters SDK-free means their tests, including
the schema-drift guards, run in the **default** dev environment; only the registration tests
need the extra installed.

## This package is called `mcp` and so is the SDK

That is safe: Python 3 has no implicit relative imports, so `from mcp.server import
MCPServer` inside `app/mcp/server.py` resolves to the installed distribution, not to a
sibling. Two rules keep it safe:

- **Never create `app/mcp/mcp.py`.** That one would shadow the SDK for this package.
- Prefer `python -m app.mcp.server` or the `eo-rag-mcp` console script over
  `mcp dev app/mcp/server.py`, which puts `app/mcp/` on `sys.path[0]` and makes the first
  rule matter.
"""

import logging

logger = logging.getLogger("eo_rag.mcp")


def load_mcp_server():
    """
    The MCP server object, or None when the optional extra is not installed.

    Same two-state shape as `langfuse_client()`, minus its "configured but not installed"
    warning: there, keys without a package is a half-finished configuration worth
    complaining about; here, an image built without the extra is a legitimate build, and
    the only consequence is that `/mcp` is not mounted.

    The import is inside the function for the reason every client in this project is lazy -
    importing `app.mcp` must not drag in an optional dependency, and `app/main.py` imports
    it unconditionally.
    """
    try:
        from app.mcp.server import mcp
    except ImportError:
        logger.info(
            "the `mcp` extra is not installed, so /mcp is not mounted; "
            "install it with: uv sync --extra mcp"
        )
        return None

    return mcp
