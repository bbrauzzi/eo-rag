"""
Tests for the asset download proxy.

Offline: the catalog lookup and the asset host are both faked. What matters here is the
containment (only hrefs the catalog gave for that id are ever fetched), the filename the
browser ends up with, and that the body is handed over as chunks rather than one buffer -
the whole reason this module exists separately from `app/api/preview.py`.
"""

import importlib
from types import SimpleNamespace

import httpx
import pytest

from app.api import assets

TIFF = b"II*\x00 not really a geotiff"

S2_ASSETS = {
    "red": {
        "href": "https://cogs.test/scene/B04.tif",
        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
        "title": "Red - 10m",
        "roles": ["data", "reflectance"],
    },
    "thumbnail": {
        "href": "https://cogs.test/scene/preview.jpg",
        "type": "image/jpeg",
        "roles": ["thumbnail"],
    },
    "no_href": {"type": "application/xml", "roles": ["metadata"]},
}


@pytest.fixture
def fake_catalog(monkeypatch):
    def _install(item_assets=None, error=None):
        def _fetch_item(item_id):
            if error is not None:
                raise error
            return {
                "id": item_id,
                "assets": S2_ASSETS if item_assets is None else item_assets,
            }

        monkeypatch.setattr(assets, "fetch_item", _fetch_item)

    return _install


@pytest.fixture
def fake_host(monkeypatch):
    """Stands in for whatever host the catalog's href points at."""

    def _install(content=TIFF, status=200, headers=None, error=None):
        fetched: list[str] = []
        closed: list[bool] = []

        def _send(request, stream=False):
            fetched.append(str(request.url))
            if error is not None:
                raise error

            response = httpx.Response(
                status_code=status, content=content, request=request
            )

            # httpx sets content-length from the body it was handed, which is realistic
            # by default. An explicit `headers` means "the host said exactly this" -
            # including saying nothing about the length, which is a case to cover.
            if headers is not None:
                response.headers.update(headers)
                if "content-length" not in headers:
                    del response.headers["content-length"]
            original_close = response.close
            response.close = lambda: (closed.append(True), original_close())[1]
            return response

        client = SimpleNamespace(
            build_request=lambda method, url: httpx.Request(method, url),
            send=_send,
        )
        monkeypatch.setattr(assets, "_client", lambda: client)
        return SimpleNamespace(fetched=fetched, closed=closed)

    return _install


# --- listing -----------------------------------------------------------------


def test_every_asset_with_an_href_is_listed(fake_catalog):
    """Unfiltered on purpose: which band is worth downloading is the person's call."""
    fake_catalog()

    listed = assets.list_assets("S2B_test")

    assert [a.key for a in listed] == ["red", "thumbnail"]
    assert listed[0].title == "Red - 10m"
    assert listed[0].roles == ["data", "reflectance"]
    assert listed[0].href == "https://cogs.test/scene/B04.tif"


def test_an_asset_without_an_href_is_not_offered(fake_catalog):
    """Nothing to download, so listing it only produces a link that 404s."""
    fake_catalog()

    assert "no_href" not in {a.key for a in assets.list_assets("S2B_test")}


def test_listing_an_unknown_item_stays_a_value_error(fake_catalog):
    """Which is what the route turns into a 404 rather than a 502."""
    fake_catalog(error=ValueError("No STAC item found with id 'nope'"))

    with pytest.raises(ValueError, match="No STAC item found"):
        assets.list_assets("nope")


def test_listing_an_unreachable_catalog_stays_a_runtime_error(fake_catalog):
    fake_catalog(error=RuntimeError("STAC API unreachable"))

    with pytest.raises(RuntimeError, match="unreachable"):
        assets.list_assets("S2B_test")


# --- downloading -------------------------------------------------------------


def test_it_fetches_the_href_the_catalog_gave_for_that_key(fake_catalog, fake_host):
    """The containment: an item id and an asset key can only ever name the catalog's."""
    fake_catalog()
    host = fake_host()

    download = assets.open_asset("S2B_test", "red")

    assert b"".join(download.chunks) == TIFF
    assert host.fetched == ["https://cogs.test/scene/B04.tif"]


def test_an_s3_asset_is_fetched_over_https(fake_catalog, fake_host):
    """Sentinel-1 GRD publishes its bands as s3://, which no HTTP client can GET."""
    fake_catalog(
        item_assets={
            "vv": {
                "href": "s3://sentinel-s1-l1c/GRD/2023/1/27/IW/DV/S1A/measurement/iw-vv.tiff",
                "type": "image/tiff",
                "roles": ["data"],
            }
        }
    )
    host = fake_host()

    assets.open_asset("S1A_test", "vv")

    assert host.fetched == [
        (
            "https://sentinel-s1-l1c.s3.amazonaws.com/"
            "GRD/2023/1/27/IW/DV/S1A/measurement/iw-vv.tiff"
        )
    ]


def test_the_filename_names_the_scene_it_came_from(fake_catalog, fake_host):
    """Every Sentinel-2 red band is B04.tif; ten of them are indistinguishable."""
    fake_catalog()
    fake_host()

    assert assets.open_asset("S2B_test", "red").filename == "S2B_test_red.tif"


def test_a_filename_falls_back_to_no_extension(fake_catalog, fake_host):
    """An href with nothing to take a suffix from must not produce a broken header."""
    fake_catalog(
        item_assets={"data": {"href": "https://cogs.test/download", "roles": ["data"]}}
    )
    fake_host()

    assert assets.open_asset("S2B_test", "data").filename == "S2B_test_data"


def test_a_quote_in_the_href_cannot_break_the_header(fake_catalog, fake_host):
    """Content-Disposition is quoted; a suffix is the one part the catalog controls."""
    fake_catalog(
        item_assets={"data": {"href": 'https://cogs.test/x.ti"f', "roles": ["data"]}},
    )
    fake_host()

    assert '"' not in assets.open_asset("S2B_test", "data").filename


def test_the_body_arrives_in_chunks(fake_catalog, fake_host):
    """The point of this module: a Sentinel-1 band never exists whole in memory."""
    fake_catalog()
    body = b"x" * (assets.CHUNK_BYTES * 2 + 7)
    fake_host(content=body)

    chunks = list(assets.open_asset("S2B_test", "red").chunks)

    assert len(chunks) == 3
    assert b"".join(chunks) == body


def test_the_size_is_passed_through_when_the_host_states_it(fake_catalog, fake_host):
    """What the browser turns into a progress bar rather than an endless spinner."""
    fake_catalog()
    fake_host(headers={"content-type": "image/tiff", "content-length": str(len(TIFF))})

    assert assets.open_asset("S2B_test", "red").size == len(TIFF)


def test_a_missing_size_is_none_rather_than_a_guess(fake_catalog, fake_host):
    fake_catalog()
    fake_host(headers={"content-type": "image/tiff"})

    assert assets.open_asset("S2B_test", "red").size is None


def test_an_abandoned_download_releases_its_connection(fake_catalog, fake_host):
    """
    A client walking away mid-download must not leak the pooled connection: a handful
    of those and every later download blocks waiting for a slot.

    One chunk is consumed first because that is the shape of the real case - Starlette
    closes the generator it has been iterating - and because a generator abandoned
    before it ever started does not run its `finally` at all.
    """
    fake_catalog()
    host = fake_host(content=b"x" * (assets.CHUNK_BYTES * 3))

    download = assets.open_asset("S2B_test", "red")
    next(download.chunks)
    download.chunks.close()

    assert host.closed == [True]


def test_a_finished_download_releases_its_connection(fake_catalog, fake_host):
    fake_catalog()
    host = fake_host()

    list(assets.open_asset("S2B_test", "red").chunks)

    assert host.closed == [True]


def test_an_unknown_asset_key_is_a_value_error(fake_catalog, fake_host):
    fake_catalog()
    fake_host()

    with pytest.raises(ValueError, match="no asset 'nir'"):
        assets.open_asset("S2B_test", "nir")


def test_a_rejected_asset_carries_its_status(fake_catalog, fake_host):
    """Eagerly, before the first chunk: after that there is no status line left."""
    fake_catalog()
    fake_host(status=403)

    with pytest.raises(RuntimeError, match="HTTP 403"):
        assets.open_asset("S2B_test", "red")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.TimeoutException("slow"), "timed out"),
        (httpx.ConnectError("no route"), "unreachable"),
    ],
)
def test_a_failing_asset_host_becomes_a_runtime_error(
    fake_catalog, fake_host, error, expected
):
    fake_catalog()
    fake_host(error=error)

    with pytest.raises(RuntimeError, match=expected):
        assets.open_asset("S2B_test", "red")


def test_import_does_not_build_an_http_client(monkeypatch):
    """Same bar as the rest of app/: importing acquires no connection."""

    def boom(*args, **kwargs):
        raise AssertionError("httpx client built at import time")

    monkeypatch.setattr(httpx, "Client", boom)

    importlib.reload(assets)

    assert assets._cached_client is None
