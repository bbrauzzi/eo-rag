"""
The MCP server object, and the stdio entry point.

The only module in the project that imports `mcp`. Everything it registers is defined in
`tools.py` and `resources.py`, which know nothing about the SDK - so this file is almost
entirely registration, and the interesting decisions are next door.

## Two transports, one definition

    python -m app.mcp.server        stdio, for Claude Desktop / Claude Code
    eo-rag-mcp                      the same thing, as a console script
    docker compose up               streamable HTTP at /mcp, mounted by app/main.py

`main()` below is the first; `app/main.py` mounts `streamable_http_app()` for the second.
The tools are registered once and neither transport knows about the other.

## stdout belongs to the protocol

Under stdio, **stdout is the JSON-RPC channel**. `configure_logging()` defaults to a stdout
handler - correct for a web server, fatal here: one trace line and the client dies with a
parse error that names nothing useful. `main()` therefore passes `sys.stderr`.

For the same reason `main()` must never import `app.main`, which calls `configure_logging()`
at *import* and would install the stdout handler before this file got a say.
"""

import sys

from mcp.server import MCPServer

from app.mcp.resources import RESOURCES
from app.mcp.tools import TOOLS
from app.obs.tracing import configure_logging

INSTRUCTIONS = (
    "Tools and documentation for Earth Observation work with SpatioTemporal Asset Catalogs "
    "(STAC).\n\n"
    "Use `stac_search` to find out which satellite scenes exist for a place and period, "
    "`compute_index` to measure NDVI or NDWI over an area of one scene, and `rag_lookup` "
    "to search the indexed STAC specification for how something is defined.\n\n"
    "The documentation is also readable directly: start at the `docs://sources` resource, "
    "which lists each document and its sections, then read `docs://section/{source}/"
    "{section}` for the part you want. Prefer a section over a whole document - the STAC "
    "core specification is 65 KB."
)

mcp = MCPServer(
    "eo-rag",
    instructions=INSTRUCTIONS,
    # Building the server registers callables; it opens nothing. The import-purity test in
    # tests/test_mcp_server.py is what keeps that true.
    version="0.1.0",
)

# Registered from the mappings next door rather than with decorators here, so that the
# names an MCP client sees are declared in the same SDK-free module the drift guards read -
# and those names are the same three Claude uses.
for _name, _fn in TOOLS.items():
    mcp.tool(name=_name)(_fn)

for _uri, _resource_fn, _mime, _description in RESOURCES:
    mcp.resource(_uri, mime_type=_mime, description=_description)(_resource_fn)


def main() -> None:
    """Run the server over stdio. The console script and `python -m` both land here."""
    # stderr, not stdout: see the module docstring.
    configure_logging(stream=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
