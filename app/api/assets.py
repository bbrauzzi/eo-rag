"""
STAC asset downloads, proxied on the same containment principle as `app/api/preview.py`:
the caller names an **item id and an asset key**, never a URL. The only hrefs this module
ever fetches are the ones the configured catalog returned under that id, so the endpoint
cannot be turned into a fetcher for arbitrary addresses.

Why proxy at all, when a preview's CORS reasons do not apply to a download:

- Sentinel-1 GRD publishes its bands as `s3://sentinel-s1-l1c/...`. A browser cannot
  follow that at all, so a direct link is not merely slower, it is nothing.
- The filename. Every Sentinel-2 scene's red band is called `B04.tif`; ten of them in a
  downloads folder are `B04.tif`, `B04 (1).tif`, and so on, with nothing left to say
  which scene each came from. `Content-Disposition` puts the item id back in front.
- A catalog that blocks hotlinking, or serves over a scheme or an auth the browser has
  no way to use, keeps working here without the frontend learning anything about it.

The cost is real and is the reason this streams rather than buffers: a Sentinel-1 GRD
band is most of a gigabyte, and `fetch_preview`'s read-it-all-then-return shape would
hold every byte in memory. Chunks go out as they arrive, so memory stays flat and the
browser gets a progress bar from the upstream `Content-Length`.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from app.tools.stac_search import fetch_item, fetchable_href

# Generous where a full scene needs it and tight where a hang would otherwise be
# invisible: `read` bounds the wait for *one* chunk, not the whole transfer, so a slow
# 800 MB download is fine and a stalled connection still gives up.
TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0)

CHUNK_BYTES = 256 * 1024

_cached_client = None


def _client() -> httpx.Client:
    """httpx client built and cached on first use (no side effects at import time)."""
    global _cached_client
    if _cached_client is None:
        _cached_client = httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    return _cached_client


@dataclass(frozen=True)
class Asset:
    """One downloadable asset of an item, as the frontend lists it."""

    key: str
    title: str | None
    type: str | None
    roles: list[str]
    # The catalog's own href, shown so it can be copied into GDAL (`/vsicurl/...`)
    # rather than downloaded. Not where the browser fetches from - that is this API.
    href: str


def list_assets(item_id: str) -> list[Asset]:
    """
    Every asset of one item that carries an href, by key.

    Unfiltered on purpose. `stac_search` projects the band stack away because 35 assets
    would bury the model's context, but this is the endpoint a *person* uses to get at
    exactly those bands, and which of them is worth downloading is their call - the
    metadata XML as much as the COG.

    Raises ValueError when the catalog has no such item, RuntimeError when it cannot be
    reached: the same split the rest of `app/tools` uses.
    """
    assets = fetch_item(item_id).get("assets") or {}

    return [
        Asset(
            key=key,
            title=asset.get("title"),
            type=asset.get("type"),
            roles=list(asset.get("roles") or ()),
            href=asset["href"],
        )
        for key, asset in sorted(assets.items())
        if asset.get("href")
    ]


def _asset_href(item_id: str, asset_key: str) -> str:
    asset = (fetch_item(item_id).get("assets") or {}).get(asset_key)

    if not asset or not asset.get("href"):
        raise ValueError(f"Item {item_id!r} has no asset {asset_key!r}")

    return fetchable_href(asset["href"])


def _filename(item_id: str, asset_key: str, href: str) -> str:
    """
    `<item id>_<asset key>` plus whatever extension the catalog's href carried.

    The extension comes from the href rather than the media type because that is what
    the tools downstream read: GDAL opens `.tif`, and a `Content-Type` of
    `image/tiff; application=geotiff; profile=cloud-optimized` maps to no single one.
    """
    suffix = PurePosixPath(urlsplit(href).path).suffix

    # Only the suffix is untrusted enough to matter - item id and asset key are already
    # constrained by the catalog - but a header injection here would be free otherwise.
    if not suffix.isascii() or any(c in suffix for c in '"\\/\r\n'):
        suffix = ""

    return f"{item_id}_{asset_key}{suffix}"


@dataclass(frozen=True)
class AssetDownload:
    """A download in progress: the headers are known, the body is still arriving."""

    chunks: Iterator[bytes]
    media_type: str
    filename: str
    # None when the host does not say, which is what leaves the browser without a
    # progress bar rather than showing a wrong one.
    size: int | None


def open_asset(item_id: str, asset_key: str) -> AssetDownload:
    """
    Start downloading one asset, returning once its headers are in.

    The upstream request is made **eagerly** rather than inside the body generator, so a
    404 or a timeout is still an exception here and the route can answer with a status.
    Once the first chunk has gone out to the client there is no status line left to
    change - the same corner `ask_stream` is in.

    Raises ValueError when the item or the asset does not exist, RuntimeError when the
    catalog or the asset host cannot be reached.
    """
    href = _asset_href(item_id, asset_key)
    client = _client()

    try:
        response = client.send(client.build_request("GET", href), stream=True)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        e.response.close()
        raise RuntimeError(
            f"Asset {asset_key!r} of {item_id} rejected by its host "
            f"(HTTP {e.response.status_code})"
        ) from e
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Asset {asset_key!r} of {item_id} timed out") from e
    except httpx.RequestError as e:
        raise RuntimeError(f"Asset {asset_key!r} of {item_id} unreachable: {e}") from e

    def chunks() -> Iterator[bytes]:
        # The connection goes back to the pool even if the client walks away mid-download
        # and Starlette closes this generator - without which a few abandoned downloads
        # exhaust the pool and every later one blocks.
        try:
            yield from response.iter_bytes(CHUNK_BYTES)
        finally:
            response.close()

    length = response.headers.get("content-length")

    return AssetDownload(
        chunks=chunks(),
        media_type=response.headers.get("content-type") or "application/octet-stream",
        filename=_filename(item_id, asset_key, href),
        size=int(length) if length and length.isdigit() else None,
    )
