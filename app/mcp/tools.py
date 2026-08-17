"""
The three tools as an MCP client sees them.

Thin adapters over `app/tools/*`, and deliberately **free of any `mcp` import** so that
these and their drift guards run in the default dev environment, where the optional extra
is not installed.

## Why adapters rather than decorating the originals

Under MCP the type hints *are* the schema - there is no hand-written `input_schema` to
disagree with the signature. That makes three things the wrapper has to supply:

- **Types the originals do not have.** `compute_index(item_id, bbox, index)` has `bbox`
  entirely untyped, and `stac_search` takes `Sequence[float]`, which is not something to
  generate a JSON schema from. Here they are `list[float]`.
- **The enum.** `COMPUTE_INDEX_TOOL` hand-writes `"enum": sorted(INDICES)`;
  `Literal["ndvi", "ndwi"]` is how the SDK is told the same thing, and a test ties it back
  to `INDICES`.
- **The descriptions**, which were tuned against the live catalog and are not decoration:
  the collection allowlist named in the text is what stops the model inventing `sentinel2`,
  and the bare-date guidance is what stops it sending an instant that matches nothing. They
  are carried over verbatim rather than rewritten.

The caller-side knobs stay hidden exactly as they are from Claude: `asset_keys` on
`stac_search` (it is how `compute_index` asks for bands), `top_k` on `rag_lookup`.

## Sessions without a request scope

`rag_lookup` needs a `Session` and there is no FastAPI dependency to supply one, so
`_session()` opens and closes one per call, mirroring `app/rag/ingest.py`. The SDK
dispatches sync tool functions to a worker thread, so each call gets its own `Session` off
the shared process-wide `engine` - which is what makes it safe, since a `Session` is not
thread-safe but an `Engine` is.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import SessionLocal
from app.tools.compute_index import INDICES, IndexResult, compute_index
from app.tools.rag_lookup import rag_lookup
from app.tools.stac_search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SearchResult,
    model_view,
    stac_search,
)

# The enum `COMPUTE_INDEX_TOOL` writes by hand, expressed the way the SDK reads it.
# `tests/test_mcp_tools.py` asserts these are the same set as `INDICES`.
IndexName = Literal["ndvi", "ndwi"]

# Built the same way `STAC_SEARCH_TOOL` builds it, so the two cannot say different things
# about which collections exist.
_COLLECTIONS_HELP = (
    "Collections to search, e.g. ['sentinel-2-l2a']. Omit to search all the collections "
    "the catalog offers."
    + (
        " Available: " + ", ".join(sorted(settings.allowed_collections)) + "."
        if settings.allowed_collections
        else ""
    )
)


@contextmanager
def _session() -> Iterator[Session]:
    """
    A Session for a call that has no request to hang one on.

    Same shape as `app/rag/ingest.py`: opened here, closed in a `finally`, never shared.
    Runs inside the SDK's worker thread, so one per call is also one per thread.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def mcp_stac_search(
    bbox: Annotated[
        list[float],
        Field(
            description=(
                "Area of interest as [west, south, east, north] in decimal degrees (WGS84)."
            )
        ),
    ],
    datetime: Annotated[
        str | None,
        Field(
            description=(
                "Time of interest, as an interval 'start/end' or a single day. Bare "
                "calendar dates are fine and cover the whole day: "
                "'2024-01-01/2024-01-31', '2024-01-15', or '2024-01-01/..' for open "
                "ended. Omit to search every date."
            )
        ),
    ] = None,
    collections: Annotated[list[str] | None, Field(description=_COLLECTIONS_HELP)] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                f"Maximum number of scenes to return (default {DEFAULT_LIMIT}, capped at "
                f"{MAX_LIMIT})."
            )
        ),
    ] = DEFAULT_LIMIT,
    max_cloud_cover: Annotated[
        float | None,
        Field(description="Keep only scenes below this cloud cover percentage (0-100)."),
    ] = None,
    include_geometry: Annotated[
        bool,
        Field(
            description=(
                "Include each scene's footprint polygon. Off by default because the "
                "coordinates are large and most callers only need the bbox; turn it on to "
                "draw the scenes on a map."
            )
        ),
    ] = False,
) -> SearchResult:
    """Search a STAC catalog for satellite imagery covering an area and a time range.

    Returns a summary of the matching scenes - identifier, collection, acquisition
    datetime, cloud cover, available asset names - not the pixels themselves. Use it to
    find out which data exists for a place and period. Do not use it for questions about
    how STAC or its specification works: those are answered from the documentation.
    """
    result = stac_search(
        bbox=bbox,
        datetime=datetime,
        collections=collections,
        limit=limit,
        max_cloud_cover=max_cloud_cover,
    )

    if include_geometry:
        return result

    # Routed back through `model_view` rather than re-deciding what to strip, so that it
    # stays the single definition of what a model is shown. The round trip costs a
    # `"geometry": null` per item - eighteen bytes against several hundred for a ring - and
    # keeps the declared output schema a SearchResult either way.
    #
    # Not optional, and not a per-consumer choice: the SDK puts a returned model into both
    # `structured_content` *and* the JSON text `content`, so there is no way to give the
    # footprints to a map and withhold them from the model. Measured on a live three-scene
    # search: 69,727 bytes raw against 2,516 projected.
    return SearchResult.model_validate(model_view(result))


def mcp_compute_index(
    item_id: Annotated[
        str,
        Field(
            description=(
                "Identifier of the STAC item to read, as returned by stac_search (for "
                "example 'S2B_33TTG_20240130_0_L2A')."
            )
        ),
    ],
    bbox: Annotated[
        list[float],
        Field(
            description=(
                "Area to compute over, as [west, south, east, north] in decimal degrees "
                "(WGS84). Must fall inside the item's footprint. Keep it tight: a smaller "
                "area is read faster and at full resolution."
            )
        ),
    ],
    index: Annotated[IndexName, Field(description="Which index to compute.")] = "ndvi",
) -> IndexResult:
    """Compute a spectral index over an area from one satellite scene.

    Returns its statistics (mean, median, percentiles, range) - not an image. NDVI measures
    vegetation vigour, NDWI measures open water. Find the scene with stac_search first:
    this needs the identifier of a specific item. Reading pixels is slow compared to a
    catalog search, so use it when the question is about the state of the ground, not about
    which data exists.
    """
    return compute_index(item_id=item_id, bbox=bbox, index=index)


def mcp_rag_lookup(
    query: Annotated[
        str,
        Field(
            description=(
                "What to look up. A focused phrasing of the concept retrieves better than "
                "the user's question copied verbatim."
            )
        ),
    ],
) -> str:
    """Search the indexed technical documentation and return the best-matching passages.

    Covers the STAC specification and related specs, and each passage carries its own
    source label. Use it for questions about how something is defined, structured or
    supposed to work. It knows nothing about which satellite data actually exists.
    """
    with _session() as db:
        # A `str` and not the `LookupResult`, for two reasons that agree. The model carries
        # `scored: list[tuple[DocChunk, float]]`, and `DocChunk` is a SQLAlchemy model that
        # pydantic cannot produce a JSON schema for - returning it fails outright. And the
        # prose is the right answer anyway: `_run_tool` already treats this as the one tool
        # whose result is not JSON-encoded, because the passages carry `[Source: ...]`
        # labels and JSON would only add escaping to text meant to be read.
        return rag_lookup(db, query=query).context


# What `app/mcp/server.py` registers, and the names it registers them under. Kept here
# rather than there so the drift guards can read it without importing the SDK, and matching
# the three `*_TOOL["name"]` constants so that an MCP client and Claude call the same three
# things by the same three names.
TOOLS = {
    "stac_search": mcp_stac_search,
    "compute_index": mcp_compute_index,
    "rag_lookup": mcp_rag_lookup,
}

__all__ = ["INDICES", "TOOLS", "IndexName", "mcp_compute_index", "mcp_rag_lookup", "mcp_stac_search"]
