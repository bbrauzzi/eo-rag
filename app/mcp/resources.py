"""
The ingested documentation as MCP resources.

Resources are the read-only half of MCP: the client fetches them and decides what to do
with them, where a tool is something the model chooses to call. The corpus fits that
exactly - `rag_lookup` is the tool for "find me the passage about X", and these are for
"give me the section on Item fields".

SDK-free like `tools.py`, for the same reason.

## Three URIs, and why one of them is an index

    docs://sources                      the index
    docs://document/{source}            one document, reassembled
    docs://section/{source}/{section}   one section

MCP lists templated URIs under `resources/templates/list`, but it cannot enumerate their
*values* - a client is told the shape `docs://section/{source}/{section}` and nothing about
which sources or sections exist. Enumerating them at registration time is not an option
either: it would mean querying the database at import, which is the one thing nothing in
`app/` is allowed to do.

So `docs://sources` is the entry point. A client reads it, learns that `stac-spec-core`
exists and has a section called `Item fields`, and constructs the other two URIs. Its own
description says so, so a client author does not have to work it out.

The three authorities (`sources`, `document`, `section`) are distinct on purpose: a
`docs://{source}` template alongside a `docs://sources` static resource would match the
same URI, and which one wins is exactly the kind of thing that holds until an SDK version
changes it.

## Section names arrive decoded

`Item fields` has a space and travels as `Item%20fields`, and the SDK hands the template
parameter over already percent-decoded - verified against the installed version, and pinned
by a test, because doing it twice would corrupt any section name containing a `%`.
"""

import json

from app.mcp.tools import _session
from app.rag.documents import list_sources, read_document, read_section

SOURCES_URI = "docs://sources"
DOCUMENT_URI = "docs://document/{source}"
SECTION_URI = "docs://section/{source}/{section}"

SOURCES_DESCRIPTION = (
    "The index of ingested documentation, and the place to start: it lists every document "
    "by `source` with its `sections` in document order. The other two resources are "
    "templates and cannot be enumerated, so read this first and build their URIs from it - "
    f"`{DOCUMENT_URI}` for a whole document, `{SECTION_URI}` for one section."
)


def sources_index() -> str:
    """
    The corpus index as JSON: every document, its url, its section list and chunk count.

    JSON rather than prose because this one is machine-read - a client uses it to build the
    template URIs, where the document and section resources are read by a person or fed to
    a model.
    """
    with _session() as db:
        return json.dumps(
            [summary.model_dump() for summary in list_sources(db)],
            ensure_ascii=False,
            indent=2,
        )


def document_body(source: str) -> str:
    """One whole document, reassembled in document order."""
    with _session() as db:
        return read_document(db, source)


def section_body(source: str, section: str) -> str:
    """
    One section of one document.

    Worth preferring over the whole document: `stac-spec-core` is 65 KB and a section is a
    page, which is the difference between handing a model the answer and handing it the
    specification.
    """
    with _session() as db:
        return read_section(db, source, section)


# Registered by `app/mcp/server.py`. Declared here so the URI scheme is testable without
# the SDK installed, in the same spirit as `TOOLS` in `tools.py`.
RESOURCES = (
    (SOURCES_URI, sources_index, "application/json", SOURCES_DESCRIPTION),
    (
        DOCUMENT_URI,
        document_body,
        "text/markdown",
        "One ingested document, whole, reassembled from its chunks in document order.",
    ),
    (
        SECTION_URI,
        section_body,
        "text/markdown",
        (
            "One section of one ingested document. Section names come from the index at "
            f"`{SOURCES_URI}` and may contain spaces."
        ),
    ),
)
