"""
Tests for the preview proxy.

Offline: both the catalog lookup and the asset fetch are faked. What matters here is
which asset gets chosen and what happens when the choice cannot be made - the CORS
reasons for proxying at all are a live check (VERIFY.md step 10d).
"""

import importlib
from types import SimpleNamespace

import httpx
import pytest

from app.api import preview

JPEG = b"\xff\xd8\xff\xe0 not really a jpeg"


def item(assets: dict, item_id: str = "S2B_test") -> dict:
    return {"id": item_id, "assets": assets}


THUMBNAIL = {"thumbnail": {"href": "https://cogs.test/thumb.jpg", "type": "image/jpeg", "roles": ["thumbnail"]}}


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    """The href cache is module level; a test must not see the previous one's item."""
    monkeypatch.setattr(preview, "_cached_hrefs", {})


@pytest.fixture
def fake_catalog(monkeypatch):
    def _install(result=None, error=None):
        calls: list[str] = []

        def _fetch_item(item_id):
            calls.append(item_id)
            if error is not None:
                raise error
            return result if result is not None else item(THUMBNAIL, item_id)

        monkeypatch.setattr(preview, "fetch_item", _fetch_item)
        return calls

    return _install


@pytest.fixture
def fake_host(monkeypatch):
    """Stands in for whatever host the catalog's href points at."""

    def _install(content=JPEG, status=200, error=None):
        calls: list[str] = []

        def _get(url):
            calls.append(url)
            if error is not None:
                raise error
            request = httpx.Request("GET", url)
            return httpx.Response(status_code=status, content=content, request=request)

        monkeypatch.setattr(preview, "_client", lambda: SimpleNamespace(get=_get))
        return calls

    return _install


def test_it_fetches_the_href_the_catalog_gave_for_that_id(fake_catalog, fake_host):
    """The containment: the only URLs this ever fetches come back from the catalog."""
    fake_catalog()
    fetched = fake_host()

    body, media_type = preview.fetch_preview("S2B_test")

    assert (body, media_type) == (JPEG, "image/jpeg")
    assert fetched == ["https://cogs.test/thumb.jpg"]


def test_a_geotiff_overview_is_not_offered_to_the_browser(fake_catalog, fake_host):
    """Earth Search's `overview` carries a preview role and is a COG: unrenderable."""
    fake_catalog(
        result=item(
            {
                "overview": {
                    "href": "https://cogs.test/overview.tif",
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "roles": ["overview"],
                },
                **THUMBNAIL,
            }
        )
    )
    fetched = fake_host()

    preview.fetch_preview("S2B_test")

    assert fetched == ["https://cogs.test/thumb.jpg"]


def test_an_s3_thumbnail_is_fetched_over_https(fake_catalog, fake_host):
    """Sentinel-1 GRD publishes its quick-look as an s3:// URI, which httpx cannot GET."""
    fake_catalog(
        result=item(
            {
                "thumbnail": {
                    "href": "s3://sentinel-s1-l1c/GRD/2023/1/27/IW/DV/S1A/preview/quick-look.png",
                    "type": "image/png",
                    "roles": ["thumbnail"],
                }
            },
            item_id="S1A_test",
        )
    )
    fetched = fake_host(content=b"\x89PNG not really a png")

    _, media_type = preview.fetch_preview("S1A_test")

    assert media_type == "image/png"
    assert fetched == [
        "https://sentinel-s1-l1c.s3.amazonaws.com/GRD/2023/1/27/IW/DV/S1A/preview/quick-look.png"
    ]


def test_an_http_href_is_left_alone(fake_catalog, fake_host):
    """Only s3:// is rewritten; a catalog's own https href is fetched verbatim."""
    fake_catalog()
    fetched = fake_host()

    preview.fetch_preview("S2B_test")

    assert fetched == ["https://cogs.test/thumb.jpg"]


def test_a_band_is_not_a_preview(fake_catalog, fake_host):
    """No preview role, so not something to show even though it is an image."""
    fake_catalog(result=item({"red": {"href": "https://cogs.test/B04.jpg", "type": "image/jpeg"}}))
    fake_host()

    with pytest.raises(ValueError, match="no preview image"):
        preview.fetch_preview("S2B_test")


def test_an_item_without_a_preview_is_reported_as_such(fake_catalog, fake_host):
    fake_catalog(result=item({}))
    fake_host()

    with pytest.raises(ValueError, match="no preview image"):
        preview.fetch_preview("S2B_test")


def test_an_unknown_item_stays_a_value_error(fake_catalog, fake_host):
    """Which is what the route turns into a 404 rather than a 502."""
    fake_catalog(error=ValueError("No STAC item found with id 'nope'"))
    fake_host()

    with pytest.raises(ValueError, match="No STAC item found"):
        preview.fetch_preview("nope")


def test_an_unreachable_catalog_stays_a_runtime_error(fake_catalog, fake_host):
    fake_catalog(error=RuntimeError("STAC API unreachable"))
    fake_host()

    with pytest.raises(RuntimeError, match="unreachable"):
        preview.fetch_preview("S2B_test")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.TimeoutException("slow"), "timed out"),
        (httpx.ConnectError("no route"), "unreachable"),
    ],
)
def test_a_failing_asset_host_becomes_a_runtime_error(fake_catalog, fake_host, error, expected):
    fake_catalog()
    fake_host(error=error)

    with pytest.raises(RuntimeError, match=expected):
        preview.fetch_preview("S2B_test")


def test_a_rejected_asset_carries_its_status(fake_catalog, fake_host):
    fake_catalog()
    fake_host(status=403)

    with pytest.raises(RuntimeError, match="HTTP 403"):
        preview.fetch_preview("S2B_test")


def test_an_oversized_asset_is_refused_rather_than_proxied(fake_catalog, fake_host):
    """A catalog mislabelling a whole scene as a preview must not pull it through us."""
    fake_catalog()
    fake_host(content=b"x" * (preview.MAX_BYTES + 1))

    with pytest.raises(ValueError, match="over the"):
        preview.fetch_preview("S2B_test")


def test_the_href_is_resolved_once_per_item(fake_catalog, fake_host):
    """The card and the quicklook ask for the same scene; the catalog is asked once."""
    looked_up = fake_catalog()
    fetched = fake_host()

    preview.fetch_preview("S2B_test")
    preview.fetch_preview("S2B_test")

    assert looked_up == ["S2B_test"]
    assert len(fetched) == 2


def test_the_href_cache_is_bounded(fake_catalog, fake_host):
    """Module-level and unbounded is a leak; this is the cheapest thing that is not."""
    fake_catalog()
    fake_host()

    for i in range(preview._MAX_CACHED_HREFS + 2):
        preview.fetch_preview(f"item-{i}")

    assert len(preview._cached_hrefs) <= preview._MAX_CACHED_HREFS


def test_import_does_not_build_an_http_client(monkeypatch):
    """Same bar as the rest of app/: importing acquires no connection."""

    def boom(*args, **kwargs):
        raise AssertionError("httpx client built at import time")

    monkeypatch.setattr(httpx, "Client", boom)

    importlib.reload(preview)

    assert preview._cached_client is None
