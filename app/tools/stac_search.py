"""
STAC search tool: finds satellite imagery in a STAC catalog (Earth Search v1 by
default) given an area, a time range and a set of collections.

The single place that talks to a STAC API, on the same principle as
`app/rag/embeddings.py`: the httpx client is built lazily and cached, so importing
the module opens no connection.

The catalog response is deliberately *not* passed through as-is. A raw
FeatureCollection carries the full geometry and every asset of every item - for
Sentinel-2 that is roughly twenty band assets per scene, tens of kilobytes for a
handful of results - which would flood the model's context and bury the few fields
worth reasoning about. `_summarize_item` is that projection point.

The projection has two consumers wanting different halves of it. A map needs the
footprint polygon; the model needs everything else and is only made worse by a few
hundred bytes of coordinates it cannot reason about. So `_summarize_item` keeps the
geometry, `model_view` strips it back out for the model, and `item_footprint` turns
what is left into the GeoJSON the map draws.
"""

import datetime as _dt
import json
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app.config import settings

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
TIMEOUT_SECONDS = 30.0

DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Assets returned when the caller does not name the ones it wants: enough to show a
# scene to a human, not the whole band stack. compute_index (step 5) asks for the
# bands it needs by key instead.
PREVIEW_ROLES = frozenset({"thumbnail", "overview"})

_cached_client = None


def _client() -> httpx.Client:
    """httpx client built and cached on first use (no side effects at import time)."""
    global _cached_client
    if _cached_client is None:
        _cached_client = httpx.Client(
            base_url=settings.stac_api_url.rstrip("/"),
            timeout=TIMEOUT_SECONDS,
            headers={"Accept": "application/geo+json"},
        )
    return _cached_client


def _validate_bbox(bbox: Sequence[float]) -> list[float]:
    """Check the bbox before spending a request on it. Returns it coerced to floats."""
    if len(bbox) != 4:
        raise ValueError(f"bbox must be [west, south, east, north], got {len(bbox)} values")

    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as e:
        raise ValueError(f"bbox must hold four numbers, got {bbox!r}") from e

    if not all(-180 <= lon <= 180 for lon in (west, east)):
        raise ValueError(f"bbox longitudes must be within [-180, 180], got {west} and {east}")
    if not all(-90 <= lat <= 90 for lat in (south, north)):
        raise ValueError(f"bbox latitudes must be within [-90, 90], got {south} and {north}")
    if south >= north:
        raise ValueError(f"bbox south ({south}) must be below north ({north})")

    # No west < east check on purpose: a bbox crossing the antimeridian is written
    # with west greater than east, and that is valid.
    return [west, south, east, north]


def _validate_collections(collections: Sequence[str] | None) -> list[str] | None:
    """
    Check the requested collections against the configured allowlist.

    Locally, and before the request: an unknown collection id comes back from Earth
    Search as an empty result set, not an error, so without this the model is told
    "no scenes match" for what is really a typo - and then reports that as fact. The
    message names what *is* available, which is what lets it retry with a real id.

    An empty `settings.allowed_collections` turns the check off, for a catalog whose
    ids have not been listed yet.
    """
    if collections is None:
        return None

    allowed = set(settings.allowed_collections)
    if not allowed:
        return list(collections)

    unknown = [c for c in collections if c not in allowed]
    if unknown:
        raise ValueError(
            f"Unknown collection(s) {', '.join(repr(c) for c in unknown)}. "
            f"Available: {', '.join(sorted(allowed))}"
        )

    return list(collections)


def _bound(value: str, suffix: str) -> str:
    """Complete one end of an interval if it is a bare date; leave '..' and '' alone."""
    value = value.strip()
    return f"{value}{suffix}" if DATE_ONLY.match(value) else value


def _instant(value: str) -> _dt.datetime | None:
    """One end of an interval as a datetime, or None for an open end ('..' or '')."""
    if value in ("", ".."):
        return None

    try:
        # fromisoformat handles the 'Z' suffix from Python 3.11 on, which is the form
        # `_bound` produces and the form the catalog wants back.
        return _dt.datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(
            f"{value!r} is not a valid RFC 3339 date or time. Use 'YYYY-MM-DD', "
            "'YYYY-MM-DDTHH:MM:SSZ', or an interval joining two of those with '/'."
        ) from e


def _normalize_datetime(value: str) -> str:
    """
    Expand bare calendar dates into the full RFC 3339 the catalog demands.

    Two failures this avoids, both found against the live Earth Search: '2024-01-01'
    is rejected outright with a 400, and a lone date read as an *instant* would match
    only scenes acquired exactly at midnight - zero results, silently. A single day
    therefore becomes the whole day, and the end of an interval is inclusive, which is
    what someone asking for "January" means.

    The result is then *checked*, which the catalog used to do for us: a malformed date
    came back as an opaque HTTP 400 the model could only guess at, and a backwards
    interval ('2024-06-01/2024-01-01') is accepted by Earth Search and quietly matches
    nothing at all - the same silent-zero-results failure as the midnight instant.
    """
    value = value.strip()

    if "/" not in value:
        expanded = f"{value}T00:00:00Z/{value}T23:59:59Z" if DATE_ONLY.match(value) else value
    else:
        start, _, end = value.partition("/")
        expanded = f"{_bound(start, 'T00:00:00Z')}/{_bound(end, 'T23:59:59Z')}"

    # An instant has no "/", so `end_at` is "" and `_instant` returns None for it -
    # the ordering check below then simply does not apply.
    start_at, _, end_at = expanded.partition("/")
    first, last = _instant(start_at), _instant(end_at)

    if first and last and first > last:
        raise ValueError(
            f"The datetime interval {value!r} ends before it starts. "
            "Write it as start/end."
        )

    return expanded


def fetchable_href(href: str) -> str:
    """
    A catalog href turned into something an HTTP client can actually GET.

    Lives here, with the rest of what this module knows about a catalog's own hrefs, and
    is used by both proxies in `app/api` - the preview and the asset download.

    Not every catalog publishes over HTTP. Sentinel-1 GRD on Earth Search gives both its
    quick-look and its measurement bands as `s3://sentinel-s1-l1c/GRD/...`, with no https
    alternate anywhere on the asset, and httpx refuses that scheme outright
    (`UnsupportedProtocol`) - so every S1 card rendered a broken image while Sentinel-2,
    whose assets are already https, worked. Translating the URI keeps the containment
    intact: bucket and key still come from the item the catalog returned.

    Virtual hosted-style rather than `s3.amazonaws.com/{bucket}/...` because path style
    answers 301 for anything outside us-east-1, and the redirect it sends carries no
    `Location`. The regionless host resolves to whichever region holds the bucket.
    (A bucket name containing dots would not match the `*.s3.amazonaws.com` certificate,
    but such names cannot be created any more and no EO catalog uses one.)
    """
    parts = urlsplit(href)

    if parts.scheme != "s3":
        return href

    return f"https://{parts.netloc}.s3.amazonaws.com/{parts.path.lstrip('/')}"


def _pick_assets(assets: dict, keys: Sequence[str] | None) -> dict[str, str]:
    """Asset name -> href, restricted to the requested keys or to the preview roles."""
    if keys is not None:
        return {
            name: assets[name]["href"]
            for name in keys
            if name in assets and assets[name].get("href")
        }

    return {
        name: asset["href"]
        for name, asset in assets.items()
        if asset.get("href") and PREVIEW_ROLES & set(asset.get("roles") or ())
    }


class ItemSummary(BaseModel):
    """
    One catalog item, projected down to what a caller reasons about.

    A model rather than a dict because this projection is the tool's contract with two
    different consumers - the map and Claude - and each field below is a decision about
    what one of them needs. Validation is the smaller half of the benefit: the larger
    one is that `model_view` can now name the field it strips instead of filtering keys
    by string.
    """

    id: str | None = None
    collection: str | None = None
    datetime: str | None = None
    cloud_cover: float | None = None
    platform: str | None = None
    bbox: list[float] | None = None
    # The map's half of the projection, and the one field here the model never sees:
    # `model_view` excludes it before the result is handed back as a tool_result. A
    # footprint is a few hundred bytes of coordinates the model cannot reason about and
    # the bbox above already summarizes for it.
    geometry: dict | None = None
    # Names only: tells the caller what could be fetched without paying for the hrefs
    # of twenty bands it did not ask for.
    asset_keys: list[str] = []
    assets: dict[str, str] = {}


class SearchResult(BaseModel):
    """What `stac_search` returns: the projected items and how the search was bounded."""

    count: int
    limit: int
    items: list[ItemSummary]


def _summarize_item(feature: dict, asset_keys: Sequence[str] | None) -> ItemSummary:
    """Compact view of one STAC item: what a caller reasons about, nothing else."""
    properties = feature.get("properties") or {}
    assets = feature.get("assets") or {}

    return ItemSummary(
        id=feature.get("id"),
        collection=feature.get("collection"),
        # start_datetime is the fallback for items covering an interval, where the
        # spec allows datetime to be null.
        datetime=properties.get("datetime") or properties.get("start_datetime"),
        cloud_cover=properties.get("eo:cloud_cover"),
        platform=properties.get("platform"),
        bbox=feature.get("bbox"),
        geometry=feature.get("geometry"),
        asset_keys=sorted(assets),
        assets=_pick_assets(assets, asset_keys),
    )


def model_view(result: SearchResult) -> dict:
    """
    A search result as the model sees it: every item minus its footprint.

    The two consumers of `stac_search` want different halves of the same projection.
    The map needs the polygon; the model needs everything else and is only made worse
    by the coordinates. Splitting here rather than at the two call sites keeps a single
    definition of what the model is shown - and since `ItemSummary` declares the field,
    the exclusion below names it rather than filtering keys by string.
    """
    return result.model_dump(exclude={"items": {"__all__": {"geometry"}}})


def _polygon_from_bbox(bbox: Sequence[float]) -> dict:
    """
    A bbox as GeoJSON, for items the catalog returns without a geometry.

    Lives here, next to `_validate_bbox`, because this is the module that owns
    [west, south, east, north]: a bbox crossing the antimeridian is written with west
    *greater* than east, and closing that ring naively draws a polygon the long way
    round the globe. Splitting it at the dateline into a MultiPolygon means the map
    only ever receives one shape and never has to know the rule.
    """
    west, south, east, north = (float(v) for v in bbox)

    def ring(w: float, e: float) -> list[list[float]]:
        # Counterclockwise, closed, [lon, lat] throughout - RFC 7946 for the winding,
        # and the order MapLibre expects.
        return [[w, south], [e, south], [e, north], [w, north], [w, south]]

    if west > east:
        return {
            "type": "MultiPolygon",
            "coordinates": [[ring(west, 180.0)], [ring(-180.0, east)]],
        }

    return {"type": "Polygon", "coordinates": [ring(west, east)]}


def item_footprint(item: ItemSummary) -> dict | None:
    """
    One summarized item as a GeoJSON Feature for the map, or None if it carries
    neither a geometry nor a bbox to fall back on.

    `properties` holds what a card labels the scene with, and nothing else: the 35
    `asset_keys` of a Sentinel-2 L2A item would triple the FeatureCollection for
    strings the map never renders. The identifier is repeated at the top level for
    MapLibre's `promoteId`, which is what makes feature-state hovering work.
    """
    geometry = item.geometry

    if not geometry:
        if not item.bbox:
            return None
        geometry = _polygon_from_bbox(item.bbox)

    return {
        "type": "Feature",
        "id": item.id,
        "bbox": item.bbox,
        "geometry": geometry,
        "properties": {
            "kind": "footprint",
            "id": item.id,
            "collection": item.collection,
            "datetime": item.datetime,
            "cloud_cover": item.cloud_cover,
            "platform": item.platform,
            # Free: _pick_assets already returns the preview hrefs by default.
            "thumbnail": item.assets.get("thumbnail"),
        },
    }


def _post_search(payload: dict) -> list[dict]:
    """POST /search and return the features, turning every failure into a RuntimeError."""
    try:
        response = _client().post("/search", json=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = " ".join(e.response.text.split())[:200]
        raise RuntimeError(
            f"STAC search rejected by {settings.stac_api_url} "
            f"(HTTP {e.response.status_code}): {detail}"
        ) from e
    except httpx.TimeoutException as e:
        raise RuntimeError(
            f"STAC search timed out after {TIMEOUT_SECONDS:.0f}s "
            f"({settings.stac_api_url}). Narrow the bbox or the time range."
        ) from e
    except httpx.RequestError as e:
        raise RuntimeError(
            f"STAC API unreachable ({settings.stac_api_url}): {e}. "
            "Check STAC_API_URL and network access."
        ) from e

    try:
        return response.json().get("features") or []
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"STAC API returned a non-JSON body ({settings.stac_api_url}): {e}"
        ) from e


def fetch_item(item_id: str) -> dict:
    """
    Fetch one item by identifier, whole - assets included, with their `raster:bands`
    metadata. `compute_index` needs the scale, offset and nodata of the bands it reads,
    which the summary returned by `stac_search` deliberately drops.
    """
    features = _post_search({"ids": [item_id], "limit": 1})
    if not features:
        raise ValueError(f"No STAC item found with id {item_id!r}")

    return features[0]


def stac_search(
    bbox: Sequence[float],
    datetime: str | None = None,
    collections: Sequence[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    max_cloud_cover: float | None = None,
    asset_keys: Sequence[str] | None = None,
) -> SearchResult:
    """
    Search the configured STAC API and return a summary of the matching items.

    `datetime` is an RFC 3339 instant or interval ("2024-01-01/2024-01-31", open
    ended with "..."). `asset_keys` is not exposed to the model: it is how
    compute_index will ask for the hrefs of specific bands.

    Every argument is checked here, before the request: a malformed bbox, an
    unparseable or backwards datetime, a collection the catalog does not have, and a
    cloud cover outside 0-100. All of them raise ValueError, which the graph hands back
    to the model as an errored tool_result naming what was wrong - the point being that
    it can correct itself, where an empty result set or an opaque HTTP 400 leaves it
    reporting "no scenes match" for what was really a typo.

    RuntimeError is reserved for the catalog being unreachable or answering with an
    error, which is not something the model can fix.
    """
    if max_cloud_cover is not None and not 0 <= max_cloud_cover <= 100:
        raise ValueError(f"max_cloud_cover is a percentage in 0-100, got {max_cloud_cover}")

    payload: dict = {
        "bbox": _validate_bbox(bbox),
        "limit": max(1, min(limit, MAX_LIMIT)),
    }
    if datetime:
        payload["datetime"] = _normalize_datetime(datetime)
    if collections:
        payload["collections"] = _validate_collections(collections)
    if max_cloud_cover is not None:
        # Query extension, supported by Earth Search v1.
        payload["query"] = {"eo:cloud_cover": {"lt": max_cloud_cover}}

    features = _post_search(payload)

    return SearchResult(
        count=len(features),
        limit=payload["limit"],
        items=[_summarize_item(f, asset_keys) for f in features],
    )


# Tool definition handed to Claude. The description is what makes the model pick this
# over rag_lookup, so it says what the tool returns and what it is *not* for.
STAC_SEARCH_TOOL = {
    "name": "stac_search",
    "description": (
        "Search a STAC catalog for satellite imagery covering an area and a time range. "
        "Returns a summary of the matching scenes - identifier, collection, acquisition "
        "datetime, cloud cover, available asset names - not the pixels themselves. "
        "Use it to find out which data exists for a place and period. Do not use it for "
        "questions about how STAC or its specification works: those are answered from "
        "the documentation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bbox": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": (
                    "Area of interest as [west, south, east, north] in decimal degrees "
                    "(WGS84)."
                ),
            },
            "datetime": {
                "type": "string",
                "description": (
                    "Time of interest, as an interval 'start/end' or a single day. Bare "
                    "calendar dates are fine and cover the whole day: '2024-01-01/2024-01-31', "
                    "'2024-01-15', or '2024-01-01/..' for open ended. Omit to search "
                    "every date."
                ),
            },
            "collections": {
                "type": "array",
                "items": {"type": "string"},
                # The allowlist is named here, not just enforced in `_validate_collections`:
                # a rejected call costs a whole round trip to learn what was available, and
                # the ids are not guessable ("sentinel-2-l2a", not "sentinel2" or "S2").
                # `enum` is deliberately not used - an empty allowlist has to mean "no
                # constraint", and an empty enum would forbid every value instead.
                "description": (
                    "Collections to search, e.g. ['sentinel-2-l2a']. Omit to search all "
                    "the collections the catalog offers."
                    + (
                        " Available: " + ", ".join(sorted(settings.allowed_collections)) + "."
                        if settings.allowed_collections
                        else ""
                    )
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Maximum number of scenes to return (default {DEFAULT_LIMIT}, "
                    f"capped at {MAX_LIMIT})."
                ),
            },
            "max_cloud_cover": {
                "type": "number",
                "description": "Keep only scenes below this cloud cover percentage (0-100).",
            },
        },
        "required": ["bbox"],
    },
}
