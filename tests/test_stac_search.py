"""Tests for the stac_search tool: fake httpx client, no network and no credentials."""

import importlib
import json
from inspect import Parameter, signature
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.tools import stac_search as stac

# Live Earth Search v1 response, captured and trimmed (two sentinel-2-l2a scenes over
# Rome, January 2024). See the _comment inside the file for what was cut.
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "earth_search_search.json").read_text(encoding="utf-8")
)

ROME_BBOX = [12.35, 41.75, 12.65, 42.0]


def make_response(payload=None, status: int = 200, text: str | None = None) -> httpx.Response:
    """A real httpx.Response, so raise_for_status() and .json() behave as in production."""
    request = httpx.Request("POST", "https://example.test/v1/search")
    if text is not None:
        return httpx.Response(status, text=text, request=request)
    return httpx.Response(status, json=FIXTURE if payload is None else payload, request=request)


class FakeClient:
    """Fake httpx client: records the posts and replays a canned response, or raises."""

    def __init__(self, result: httpx.Response | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, json=None):  # signature mirrors httpx.Client.post
        self.calls.append({"url": url, "body": json})
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else make_response()


@pytest.fixture
def fake_client(monkeypatch):
    """Installs a FakeClient in place of the cached httpx client and returns it."""

    def _install(**kwargs):
        client = FakeClient(**kwargs)
        monkeypatch.setattr(stac, "_client", lambda: client)
        return client

    return _install


# --- request building -------------------------------------------------------


def test_posts_to_the_search_path_with_bbox_and_limit(fake_client):
    client = fake_client()
    stac.stac_search(ROME_BBOX)

    call = client.calls[0]
    assert call["url"] == "/search"
    assert call["body"]["bbox"] == ROME_BBOX
    assert call["body"]["limit"] == stac.DEFAULT_LIMIT


def test_bbox_is_coerced_to_floats(fake_client):
    """Integer degrees are legal input; the catalog gets numbers either way."""
    client = fake_client()
    stac.stac_search([12, 41, 13, 42])

    assert client.calls[0]["body"]["bbox"] == [12.0, 41.0, 13.0, 42.0]


def test_optional_filters_are_omitted_when_not_given(fake_client):
    client = fake_client()
    stac.stac_search(ROME_BBOX)

    assert set(client.calls[0]["body"]) == {"bbox", "limit"}


def test_datetime_and_collections_are_forwarded(fake_client):
    client = fake_client()
    stac.stac_search(
        ROME_BBOX,
        datetime="2024-01-01/2024-01-31",
        collections=["sentinel-2-l2a"],
    )

    body = client.calls[0]["body"]
    assert body["datetime"] == "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"
    assert body["collections"] == ["sentinel-2-l2a"]


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        # Bare dates: rejected by the catalog with a 400 unless completed.
        ("2024-01-01/2024-01-31", "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"),
        ("2024-01-01/..", "2024-01-01T00:00:00Z/.."),
        ("../2024-01-31", "../2024-01-31T23:59:59Z"),
        ("2024-01-01/", "2024-01-01T00:00:00Z/"),
        # A lone day means the whole day, not the instant of midnight.
        ("2024-01-15", "2024-01-15T00:00:00Z/2024-01-15T23:59:59Z"),
        # Already complete: passed through untouched.
        (
            "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z",
            "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z",
        ),
        ("2024-01-15T10:06:19Z", "2024-01-15T10:06:19Z"),
        ("2024-01-15T10:06:19+02:00", "2024-01-15T10:06:19+02:00"),
    ],
)
def test_datetime_is_completed_to_rfc3339(fake_client, given, sent):
    client = fake_client()
    stac.stac_search(ROME_BBOX, datetime=given)

    assert client.calls[0]["body"]["datetime"] == sent


def test_max_cloud_cover_becomes_a_query_extension_filter(fake_client):
    client = fake_client()
    stac.stac_search(ROME_BBOX, max_cloud_cover=20)

    assert client.calls[0]["body"]["query"] == {"eo:cloud_cover": {"lt": 20}}


def test_zero_cloud_cover_is_a_filter_not_a_missing_value(fake_client):
    """0 is falsy but meaningful: 'only cloud-free scenes'."""
    client = fake_client()
    stac.stac_search(ROME_BBOX, max_cloud_cover=0)

    assert client.calls[0]["body"]["query"] == {"eo:cloud_cover": {"lt": 0}}


def test_limit_is_clamped_to_the_maximum(fake_client):
    client = fake_client()
    stac.stac_search(ROME_BBOX, limit=10_000)

    assert client.calls[0]["body"]["limit"] == stac.MAX_LIMIT


def test_limit_never_drops_below_one(fake_client):
    client = fake_client()
    stac.stac_search(ROME_BBOX, limit=0)

    assert client.calls[0]["body"]["limit"] == 1


# --- bbox validation --------------------------------------------------------


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        ([12.0, 41.0, 13.0], "got 3 values"),
        ([12.0, 41.0, 13.0, 42.0, 0.0], "got 5 values"),
        (["west", 41.0, 13.0, 42.0], "four numbers"),
        ([200.0, 41.0, 13.0, 42.0], "longitudes"),
        ([12.0, 41.0, -181.0, 42.0], "longitudes"),
        ([12.0, -91.0, 13.0, 42.0], "latitudes"),
        ([12.0, 41.0, 13.0, 95.0], "latitudes"),
        ([12.0, 42.0, 13.0, 41.0], "below north"),
        ([12.0, 41.0, 13.0, 41.0], "below north"),
    ],
)
def test_malformed_bbox_is_rejected(bbox, expected):
    with pytest.raises(ValueError, match=expected):
        stac.stac_search(bbox)


def test_bbox_crossing_the_antimeridian_is_accepted(fake_client):
    """West greater than east is how a bbox spanning 180 degrees is written."""
    client = fake_client()
    stac.stac_search([170.0, -20.0, -170.0, -10.0])

    assert client.calls[0]["body"]["bbox"] == [170.0, -20.0, -170.0, -10.0]


def test_invalid_bbox_spends_no_request(fake_client):
    client = fake_client()

    with pytest.raises(ValueError):
        stac.stac_search([12.0, 41.0])

    assert client.calls == []


# --- datetime validation ----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["not-a-date", "2024-13-01", "2024-01-01/nonsense", "2024-01-32"],
)
def test_unparseable_datetime_is_rejected_locally(fake_client, value):
    """
    The catalog would answer an opaque HTTP 400 the model can only guess at; the message
    raised here names the formats that work, which is what lets it correct itself.
    """
    client = fake_client()

    with pytest.raises(ValueError, match="RFC 3339"):
        stac.stac_search(ROME_BBOX, datetime=value)

    assert client.calls == []


def test_a_backwards_interval_is_rejected(fake_client):
    """
    Earth Search accepts this one and quietly matches nothing - the same silent
    zero-results failure as a bare date read as an instant, and just as misleading.
    """
    client = fake_client()

    with pytest.raises(ValueError, match="ends before it starts"):
        stac.stac_search(ROME_BBOX, datetime="2024-06-01/2024-01-01")

    assert client.calls == []


def test_an_open_ended_interval_has_nothing_to_order(fake_client):
    """'..' is not a datetime, so the ordering check must not try to compare it."""
    client = fake_client()
    stac.stac_search(ROME_BBOX, datetime="../2024-01-31")

    assert client.calls[0]["body"]["datetime"] == "../2024-01-31T23:59:59Z"


# --- collection validation --------------------------------------------------


def test_an_unknown_collection_is_rejected_before_the_request(fake_client):
    """
    The failure this prevents is a silent one: the catalog answers a typo'd collection id
    with an empty result set, not an error, so the model is told "no scenes match" and
    reports that as fact.
    """
    client = fake_client()

    with pytest.raises(ValueError) as exc:
        stac.stac_search(ROME_BBOX, collections=["sentinel2"])

    # Naming what *is* available is the half that lets the model retry.
    assert "sentinel2" in str(exc.value)
    assert "sentinel-2-l2a" in str(exc.value)
    assert client.calls == []


def test_every_unknown_collection_is_named(fake_client):
    fake_client()

    with pytest.raises(ValueError) as exc:
        stac.stac_search(ROME_BBOX, collections=["sentinel-2-l2a", "nope", "also-nope"])

    assert "nope" in str(exc.value)
    assert "also-nope" in str(exc.value)


def test_allowed_collections_pass_through(fake_client):
    client = fake_client()
    stac.stac_search(ROME_BBOX, collections=["sentinel-2-l2a", "landsat-c2-l2"])

    assert client.calls[0]["body"]["collections"] == ["sentinel-2-l2a", "landsat-c2-l2"]


def test_an_empty_allowlist_turns_the_check_off(fake_client, monkeypatch):
    """What a catalog whose ids have not been listed yet needs, rather than a hard stop."""
    monkeypatch.setattr(settings, "allowed_collections", [])
    client = fake_client()

    stac.stac_search(ROME_BBOX, collections=["whatever-this-catalog-calls-it"])

    assert client.calls[0]["body"]["collections"] == ["whatever-this-catalog-calls-it"]


def test_the_schema_tells_the_model_which_collections_exist():
    """
    Enforcing the allowlist without publishing it costs a round trip to learn it, and the
    ids are not guessable from the name of a satellite.
    """
    described = stac.STAC_SEARCH_TOOL["input_schema"]["properties"]["collections"]["description"]

    assert all(name in described for name in settings.allowed_collections)


# --- cloud cover validation -------------------------------------------------


@pytest.mark.parametrize("value", [-1, 101, 250.0])
def test_cloud_cover_outside_a_percentage_is_rejected(fake_client, value):
    client = fake_client()

    with pytest.raises(ValueError, match="percentage"):
        stac.stac_search(ROME_BBOX, max_cloud_cover=value)

    assert client.calls == []


# --- response projection ----------------------------------------------------


def test_summary_keeps_the_fields_worth_reasoning_about(fake_client):
    fake_client()
    result = stac.stac_search(ROME_BBOX)

    assert result.count == 2
    first = result.items[0]
    assert first.id == "S2B_33TTG_20240130_0_L2A"
    assert first.collection == "sentinel-2-l2a"
    assert first.datetime == "2024-01-30T10:09:09.601000Z"
    assert first.cloud_cover == 1.578528
    assert first.platform == "sentinel-2b"
    assert first.bbox == pytest.approx(
        [11.354966880048131, 41.4077355525307, 12.723021156801464, 42.429351628099674]
    )


def test_the_result_is_a_model_not_a_dict(fake_client):
    """
    Step 7's structured output: the tool's contract is a declared shape, so a field that
    goes missing or changes type fails here rather than somewhere downstream.
    """
    fake_client()
    result = stac.stac_search(ROME_BBOX)

    assert isinstance(result, stac.SearchResult)
    assert all(isinstance(item, stac.ItemSummary) for item in result.items)


def test_items_keep_the_catalog_order(fake_client):
    fake_client()
    result = stac.stac_search(ROME_BBOX)

    assert [i.id for i in result.items] == [f["id"] for f in FIXTURE["features"]]


def test_links_and_properties_are_dropped(fake_client):
    """The projection contract: what would flood the context does not come back."""
    fake_client()
    item = stac.stac_search(ROME_BBOX).items[0]

    assert not hasattr(item, "links")
    assert not hasattr(item, "properties")


def test_the_geometry_comes_back_for_the_map(fake_client):
    """
    The one part of the raw feature that survives the projection. It is stripped again
    by model_view before the model sees it - the map is the only consumer.
    """
    fake_client()
    item = stac.stac_search(ROME_BBOX).items[0]

    assert item.geometry["type"] == "Polygon"

    lon, lat = item.geometry["coordinates"][0][0]
    # Rome, in [lon, lat] order. Reading 41.x, 11.x here would be the inversion that
    # puts every footprint in the Gulf of Guinea without anything failing.
    assert 11 < lon < 13
    assert 41 < lat < 43


def test_the_model_view_drops_the_geometry(fake_client):
    """What _run_tool hands to the model: the projection minus the coordinates."""
    fake_client()
    result = stac.stac_search(ROME_BBOX)

    view = stac.model_view(result)

    assert all("geometry" not in item for item in view["items"])
    assert [i["id"] for i in view["items"]] == [i.id for i in result.items]
    assert view["count"] == result.count
    # Non-destructive: the caller still holds the footprints it needs for the map.
    assert all(item.geometry for item in result.items)


def test_asset_keys_are_listed_without_their_hrefs(fake_client):
    """A real Sentinel-2 L2A scene carries 35 assets: every band as both COG and JP2."""
    fake_client()
    item = stac.stac_search(ROME_BBOX).items[0]

    assert len(item.asset_keys) == 35
    assert {"red", "red-jp2", "nir", "nir08", "scl", "visual", "thumbnail"} <= set(
        item.asset_keys
    )
    assert item.asset_keys == sorted(item.asset_keys)


def test_default_assets_are_previews_only(fake_client):
    """Thirty-five hrefs per scene is exactly what we are not returning by default."""
    fake_client()
    item = stac.stac_search(ROME_BBOX).items[0]

    assert set(item.assets) == {"thumbnail"}
    assert item.assets["thumbnail"].endswith("thumbnail.jpg")


def test_the_projection_is_an_order_of_magnitude_smaller_than_the_raw_response(fake_client):
    """
    The reason this module exists: measured on the recorded live response.

    Against `model_view` and not the raw projection, because the model is what the
    saving is for and the footprints never reach it.
    """
    fake_client()
    projected = json.dumps(stac.model_view(stac.stac_search(ROME_BBOX)))

    assert len(projected) * 10 < len(json.dumps(FIXTURE))


def test_requested_asset_keys_return_their_hrefs(fake_client):
    """How compute_index will ask for the bands it needs."""
    fake_client()
    item = stac.stac_search(ROME_BBOX, asset_keys=["red", "nir"]).items[0]

    assert set(item.assets) == {"red", "nir"}
    assert item.assets["red"].endswith("B04.tif")
    assert item.assets["nir"].endswith("B08.tif")


def test_asset_keys_missing_from_an_item_are_skipped(fake_client):
    """The second scene has no scl asset: absence is not an error."""
    fake_client()
    items = stac.stac_search(ROME_BBOX, asset_keys=["red", "scl"]).items

    assert set(items[0].assets) == {"red", "scl"}
    assert set(items[1].assets) == {"red"}


def test_empty_result_is_not_an_error(fake_client):
    fake_client(result=make_response({"type": "FeatureCollection", "features": []}))
    result = stac.stac_search(ROME_BBOX)

    assert result.count == 0
    assert result.limit == stac.DEFAULT_LIMIT
    assert result.items == []


def test_missing_features_key_is_treated_as_no_results(fake_client):
    fake_client(result=make_response({"type": "FeatureCollection"}))

    assert stac.stac_search(ROME_BBOX).count == 0


def test_item_without_assets_or_properties_does_not_blow_up(fake_client):
    fake_client(result=make_response({"features": [{"id": "bare", "collection": "c"}]}))
    item = stac.stac_search(ROME_BBOX).items[0]

    assert item.id == "bare"
    assert item.datetime is None
    assert item.cloud_cover is None
    assert item.asset_keys == []
    assert item.assets == {}


def test_datetime_falls_back_to_start_datetime(fake_client):
    """The spec allows a null datetime on items covering an interval."""
    feature = {
        "id": "ranged",
        "properties": {"datetime": None, "start_datetime": "2024-01-01T00:00:00Z"},
    }
    fake_client(result=make_response({"features": [feature]}))

    assert stac.stac_search(ROME_BBOX).items[0].datetime == "2024-01-01T00:00:00Z"


# --- footprints -------------------------------------------------------------


def test_footprint_carries_the_catalog_geometry(fake_client):
    fake_client()
    item = stac.stac_search(ROME_BBOX).items[0]

    feature = stac.item_footprint(item)

    assert feature["type"] == "Feature"
    assert feature["geometry"] == item.geometry
    assert feature["bbox"] == item.bbox
    # Repeated at the top level for MapLibre's promoteId, which is what makes
    # feature-state hovering work at all.
    assert feature["id"] == item.id == feature["properties"]["id"]


def test_footprint_properties_carry_what_the_map_labels_with(fake_client):
    """And nothing else: 35 asset_keys per item would triple the FeatureCollection."""
    fake_client()
    properties = stac.item_footprint(stac.stac_search(ROME_BBOX).items[0])["properties"]

    assert set(properties) == {
        "kind",
        "id",
        "collection",
        "datetime",
        "cloud_cover",
        "platform",
        "thumbnail",
    }
    assert properties["kind"] == "footprint"
    assert properties["thumbnail"].endswith("thumbnail.jpg")


def test_footprint_falls_back_to_the_bbox(fake_client):
    """A catalog is allowed to return an item with a null geometry."""
    feature = {"id": "no-geom", "bbox": [12.0, 41.0, 13.0, 42.0], "geometry": None}
    fake_client(result=make_response({"features": [feature]}))

    geometry = stac.item_footprint(stac.stac_search(ROME_BBOX).items[0])["geometry"]

    assert geometry["type"] == "Polygon"
    ring = geometry["coordinates"][0]
    assert ring[0] == ring[-1] == [12.0, 41.0]
    assert ring == [[12.0, 41.0], [13.0, 41.0], [13.0, 42.0], [12.0, 42.0], [12.0, 41.0]]


def test_footprint_splits_a_bbox_crossing_the_antimeridian():
    """
    west > east is a valid bbox, and _validate_bbox lets it through on purpose. Closing
    that ring naively would draw a polygon the long way round the globe.
    """
    geometry = stac._polygon_from_bbox([170.0, -10.0, -170.0, 10.0])

    assert geometry["type"] == "MultiPolygon"
    east_half, west_half = geometry["coordinates"]
    assert [c[0] for c in east_half[0]] == [170.0, 180.0, 180.0, 170.0, 170.0]
    assert [c[0] for c in west_half[0]] == [-180.0, -170.0, -170.0, -180.0, -180.0]


def test_footprint_is_none_without_a_geometry_or_a_bbox(fake_client):
    fake_client(result=make_response({"features": [{"id": "bare"}]}))

    assert stac.item_footprint(stac.stac_search(ROME_BBOX).items[0]) is None


# --- error handling ---------------------------------------------------------


def test_http_error_becomes_runtime_error_carrying_the_body(fake_client):
    fake_client(result=make_response(status=400, text="bad datetime interval"))

    with pytest.raises(RuntimeError) as exc:
        stac.stac_search(ROME_BBOX)

    message = str(exc.value)
    assert "400" in message
    assert "bad datetime interval" in message


def test_timeout_becomes_runtime_error_with_a_hint(fake_client):
    fake_client(error=httpx.ReadTimeout("read timed out"))

    with pytest.raises(RuntimeError, match="timed out"):
        stac.stac_search(ROME_BBOX)


def test_connection_failure_becomes_runtime_error(fake_client):
    fake_client(error=httpx.ConnectError("connection refused"))

    with pytest.raises(RuntimeError) as exc:
        stac.stac_search(ROME_BBOX)

    assert "unreachable" in str(exc.value)
    assert "STAC_API_URL" in str(exc.value)


def test_non_json_body_becomes_runtime_error(fake_client):
    fake_client(result=make_response(text="<html>gateway error</html>"))

    with pytest.raises(RuntimeError, match="non-JSON"):
        stac.stac_search(ROME_BBOX)


# --- fetch_item -------------------------------------------------------------


def test_fetch_item_asks_the_catalog_by_id(fake_client):
    client = fake_client()
    stac.fetch_item("S2B_33TTG_20240130_0_L2A")

    assert client.calls[0]["body"] == {"ids": ["S2B_33TTG_20240130_0_L2A"], "limit": 1}


def test_fetch_item_returns_the_whole_feature_not_a_summary(fake_client):
    """compute_index needs the raster:bands scale and offset the summary drops."""
    fake_client()
    item = stac.fetch_item("S2B_33TTG_20240130_0_L2A")

    assert item["assets"]["red"]["href"].endswith("B04.tif")
    assert item["assets"]["red"]["raster:bands"][0]["scale"] == 0.0001
    assert item["assets"]["red"]["raster:bands"][0]["offset"] == -0.1
    assert "geometry" in item


def test_fetch_item_rejects_an_id_the_catalog_does_not_know(fake_client):
    fake_client(result=make_response({"type": "FeatureCollection", "features": []}))

    with pytest.raises(ValueError, match="No STAC item found"):
        stac.fetch_item("does-not-exist")


def test_fetch_item_surfaces_catalog_errors_like_search_does(fake_client):
    fake_client(error=httpx.ConnectError("connection refused"))

    with pytest.raises(RuntimeError, match="unreachable"):
        stac.fetch_item("whatever")


# --- tool definition --------------------------------------------------------


def test_tool_schema_stays_in_sync_with_the_function():
    """Drift guard: the model must not be offered arguments the function has not got."""
    params = signature(stac.stac_search).parameters
    schema = stac.STAC_SEARCH_TOOL["input_schema"]

    assert set(schema["properties"]) <= set(params)

    mandatory = {name for name, p in params.items() if p.default is Parameter.empty}
    assert set(schema["required"]) == mandatory


def test_asset_keys_is_not_exposed_to_the_model():
    """It is a caller-side knob for compute_index, not something Claude should pick."""
    assert "asset_keys" not in stac.STAC_SEARCH_TOOL["input_schema"]["properties"]


# --- import purity ----------------------------------------------------------


def test_import_does_not_build_an_http_client(monkeypatch):
    """Importing the module must open no connection (lazy client, zero network)."""

    def boom(*args, **kwargs):
        raise AssertionError("httpx.Client built at import time")

    monkeypatch.setattr(httpx, "Client", boom)

    importlib.reload(stac)

    assert stac._cached_client is None
