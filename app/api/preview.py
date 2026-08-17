"""
Scene previews, served from our own origin.

The frontend cannot load a catalog's thumbnail directly. The image becomes a WebGL
texture in the map, which makes it a CORS request, and that depends on headers the
catalog is under no obligation to send - Earth Search does, many STAC catalogs do not.
Worse, a response fetched *without* CORS comes back with no `Vary: Origin`, so the
browser caches it as reusable for any later request to that URL and the CORS one is then
refused from cache. Reproduced: clean cache 200, one non-CORS load, blocked, hard reload
200 again.

Proxying removes the whole class of problem: same origin, no preflight, no cache entry
anyone else can poison, and a catalog that sends no CORS headers at all still works.

**The endpoint takes an item id, never a URL.** That is what keeps it from being an open
redirector into the network: the only hrefs this module ever fetches are the ones the
configured catalog returned for that id.
"""

import httpx

from app.tools.stac_search import PREVIEW_ROLES, fetch_item, fetchable_href

TIMEOUT_SECONDS = 20.0

# What a browser can actually paint. An `overview` asset carries a preview role too, but
# on Earth Search it is a cloud-optimized GeoTIFF - which would proxy happily and then
# render as a broken image.
BROWSER_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})

# A thumbnail is tens of kilobytes. The cap is here so that a catalog mislabelling a full
# scene as a preview cannot pull hundreds of megabytes through the API.
MAX_BYTES = 8 * 1024 * 1024

# item id -> (href, media type). Resolving an href costs a catalog round trip, and the
# same item is asked for at least twice: once for the card, once when its quicklook goes
# on the map. Item assets do not change, so this only ever saves work.
_MAX_CACHED_HREFS = 512
_cached_hrefs: dict[str, tuple[str, str]] = {}

_cached_client = None


def _client() -> httpx.Client:
    """httpx client built and cached on first use (no side effects at import time)."""
    global _cached_client
    if _cached_client is None:
        _cached_client = httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    return _cached_client


def _preview_asset(item: dict) -> tuple[str, str]:
    """The href and media type of the item's preview image."""
    assets = item.get("assets") or {}

    # Sorted so an asset actually named "thumbnail" wins over any other preview-roled one.
    for name, asset in sorted(assets.items(), key=lambda kv: kv[0] != "thumbnail"):
        media_type = (asset.get("type") or "").split(";")[0].strip().lower()
        roles = set(asset.get("roles") or ())

        if asset.get("href") and PREVIEW_ROLES & roles and media_type in BROWSER_IMAGE_TYPES:
            return fetchable_href(asset["href"]), media_type

    raise ValueError(f"Item {item.get('id')!r} carries no preview image")


def _resolve(item_id: str) -> tuple[str, str]:
    if item_id not in _cached_hrefs:
        if len(_cached_hrefs) >= _MAX_CACHED_HREFS:
            _cached_hrefs.clear()
        _cached_hrefs[item_id] = _preview_asset(fetch_item(item_id))

    return _cached_hrefs[item_id]


def fetch_preview(item_id: str) -> tuple[bytes, str]:
    """
    The preview image of one STAC item, as bytes and its media type.

    Raises ValueError when the catalog has no such item or it carries no preview, and
    RuntimeError when the catalog or the asset host cannot be reached - the same split
    the rest of `app/tools` uses.
    """
    href, media_type = _resolve(item_id)

    try:
        response = _client().get(href)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Preview for {item_id} rejected by its host (HTTP {e.response.status_code})"
        ) from e
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Preview for {item_id} timed out after {TIMEOUT_SECONDS:.0f}s") from e
    except httpx.RequestError as e:
        raise RuntimeError(f"Preview for {item_id} unreachable: {e}") from e

    if len(response.content) > MAX_BYTES:
        raise ValueError(
            f"Preview for {item_id} is {len(response.content)} bytes, over the "
            f"{MAX_BYTES} the API will proxy"
        )

    return response.content, media_type
