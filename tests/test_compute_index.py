"""
Tests for the compute_index tool.

Fully offline, but not faked at the pixel level: each test writes real GeoTIFFs to a
tmp_path with known values and hands their paths to the tool as if they were asset
hrefs. rasterio opens a local path exactly as it opens a remote one, so the windowing,
masking and scaling code under test is the real thing - only the transport is missing.

The catalog lookup is monkeypatched, since resolving an item is stac_search's job and
is covered there.
"""

from inspect import Parameter, signature

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.tools import compute_index as module
from app.tools.compute_index import (
    COMPUTE_INDEX_TOOL,
    INDICES,
    IndexResult,
    Statistics,
    compute_index,
)

# A 20x20 raster at 10 m in UTM 33N, positioned so that its geographic bounds sit over
# central Italy - close enough to reality that the WGS84 -> UTM transform is exercised
# on realistic numbers rather than degenerate ones.
ORIGIN_X, ORIGIN_Y = 290000.0, 4640000.0
PIXEL = 10.0
SIZE = 20
EPSG = 32633


def write_raster(path, values, nodata=0):
    """One-band uint16 GeoTIFF holding `values`, broadcast to the full grid if scalar."""
    array = np.full((SIZE, SIZE), values, dtype="uint16") if np.isscalar(values) else values

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=SIZE,
        width=SIZE,
        count=1,
        dtype="uint16",
        crs=rasterio.crs.CRS.from_epsg(EPSG),
        transform=from_origin(ORIGIN_X, ORIGIN_Y, PIXEL, PIXEL),
        nodata=nodata,
    ) as dst:
        dst.write(array, 1)

    return str(path)


def raster_bbox():
    """The full extent of the test rasters, in WGS84, as the tool expects a bbox."""
    from rasterio.warp import transform_bounds

    return list(
        transform_bounds(
            rasterio.crs.CRS.from_epsg(EPSG),
            rasterio.crs.CRS.from_epsg(4326),
            ORIGIN_X,
            ORIGIN_Y - SIZE * PIXEL,
            ORIGIN_X + SIZE * PIXEL,
            ORIGIN_Y,
        )
    )


@pytest.fixture
def fake_item(monkeypatch):
    """Installs a STAC item whose assets point at local files."""

    def _install(assets, item_id="S2B_test_L2A", properties=None):
        item = {
            "id": item_id,
            "collection": "sentinel-2-l2a",
            "properties": properties or {"datetime": "2024-01-30T10:09:09Z", "eo:cloud_cover": 1.5},
            "assets": assets,
        }
        monkeypatch.setattr(module, "fetch_item", lambda _id: item)
        return item

    return _install


def asset(href, scale=None, offset=None, nodata=None):
    """An asset entry, optionally carrying the raster extension metadata."""
    entry = {"href": href, "roles": ["data", "reflectance"]}
    band = {k: v for k, v in (("scale", scale), ("offset", offset), ("nodata", nodata)) if v is not None}
    if band:
        entry["raster:bands"] = [band]
    return entry


# --- the computation --------------------------------------------------------


def test_ndvi_of_uniform_bands_is_the_expected_ratio(tmp_path, fake_item):
    """nir=3000, red=1000 -> (3000-1000)/(3000+1000) = 0.5, with no scaling involved."""
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    result = compute_index("S2B_test_L2A", raster_bbox())

    assert result.statistics.mean == pytest.approx(0.5)
    assert result.statistics.min == pytest.approx(0.5)
    assert result.statistics.max == pytest.approx(0.5)
    assert result.index == "ndvi"
    assert (result.bands.a, result.bands.b) == ("nir", "red")


def test_the_offset_changes_the_result_and_the_scale_does_not(tmp_path, fake_item):
    """
    The reason scaling is applied at all. A normalized difference is invariant to a
    common scale but not to a common offset, so Sentinel-2's -0.1 BOA offset biases
    every index computed without it.
    """
    nir, red = write_raster(tmp_path / "nir.tif", 3000), write_raster(tmp_path / "red.tif", 1000)

    fake_item({"nir": asset(nir, scale=0.0001), "red": asset(red, scale=0.0001)})
    scale_only = compute_index("i", raster_bbox())

    fake_item(
        {
            "nir": asset(nir, scale=0.0001, offset=-0.1),
            "red": asset(red, scale=0.0001, offset=-0.1),
        }
    )
    with_offset = compute_index("i", raster_bbox())

    assert scale_only.statistics.mean == pytest.approx(0.5)

    # (0.3 - 0.1) - (0.1 - 0.1) over (0.3 - 0.1) + (0.1 - 0.1) = 0.2 / 0.2 = 1.0
    assert with_offset.statistics.mean == pytest.approx(1.0)


def test_an_offset_the_pixels_contradict_is_dropped(tmp_path, fake_item):
    """
    The live bug. These are the DN medians measured over a Rome suburb on
    S2B_33TTG_20240130_0_L2A, an item Earth Search declares `offset: -0.1` on: applying
    it puts red at -0.0246 reflectance, which does not exist. The offset is judged not to
    describe the pixels and the index is computed from the scale alone.
    """
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 1598), scale=0.0001, offset=-0.1),
            "red": asset(write_raster(tmp_path / "red.tif", 754), scale=0.0001, offset=-0.1),
        }
    )

    result = compute_index("i", raster_bbox())

    assert result.reflectance.offset_declared == [-0.1, -0.1]
    assert result.reflectance.offset_applied is False

    # (0.1598 - 0.0754) / (0.1598 + 0.0754), the value the unshifted pixels support.
    assert result.statistics.mean == pytest.approx(0.0844 / 0.2352)


def test_one_band_contradicting_the_offset_drops_it_for_both(tmp_path, fake_item):
    """
    Per-band correction would build the index out of two different quantities. 0.875 is
    both bands unshifted; 0.818 would be nir shifted and red not.
    """
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000), scale=0.0001, offset=-0.1),
            "red": asset(write_raster(tmp_path / "red.tif", 200), scale=0.0001, offset=-0.1),
        }
    )

    result = compute_index("i", raster_bbox())

    assert result.reflectance.offset_applied is False
    assert result.statistics.mean == pytest.approx(0.28 / 0.32)


def test_an_offset_the_pixels_bear_out_is_kept_and_dark_pixels_dropped(tmp_path, fake_item):
    """
    The other side of the check: a genuinely shifted product. A handful of dark pixels
    below zero is what a real BOA offset produces over water or shadow, so it stays
    applied - those pixels are simply excluded rather than taken as evidence against it.
    """
    nir = np.full((SIZE, SIZE), 3000, dtype="uint16")
    nir[0, :10] = 500  # 10 of 400 pixels, 2.5%, below the 5% the check allows
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", nir), scale=0.0001, offset=-0.1),
            "red": asset(write_raster(tmp_path / "red.tif", 1200), scale=0.0001, offset=-0.1),
        }
    )

    result = compute_index("i", raster_bbox())

    assert result.reflectance.offset_applied is True
    assert result.pixels.valid == SIZE * SIZE - 10

    # (0.2 - 0.02) / (0.2 + 0.02) on the pixels that survive.
    assert result.statistics.mean == pytest.approx(0.18 / 0.22)


def test_the_index_cannot_leave_its_range(tmp_path, fake_item):
    """
    A normalized difference is confined to [-1, 1]. Before negative reflectance was
    excluded, a denominator crossing zero sent the live NDVI to -4.8e11 with percentiles
    of -1.66 and 3.22 - a number no reader could tell was wrong from its shape alone.
    """
    nir = np.tile(np.arange(SIZE, dtype="uint16") * 300, (SIZE, 1))
    red = np.tile(np.arange(SIZE, dtype="uint16")[::-1] * 300, (SIZE, 1))
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", nir), scale=0.0001, offset=-0.1),
            "red": asset(write_raster(tmp_path / "red.tif", red), scale=0.0001, offset=-0.1),
        }
    )

    stats = compute_index("i", raster_bbox()).statistics

    assert -1.0 <= stats.min <= stats.max <= 1.0


def test_ndwi_uses_green_and_nir(tmp_path, fake_item):
    fake_item(
        {
            "green": asset(write_raster(tmp_path / "green.tif", 2000)),
            "nir": asset(write_raster(tmp_path / "nir.tif", 1000)),
        }
    )

    result = compute_index("i", raster_bbox(), index="ndwi")

    assert (result.bands.a, result.bands.b) == ("green", "nir")
    assert result.statistics.mean == pytest.approx(1 / 3)


def test_statistics_describe_a_mixed_scene(tmp_path, fake_item):
    """Half the pixels vegetated, half bare: the mean alone would hide the split."""
    nir = np.full((SIZE, SIZE), 1000, dtype="uint16")
    nir[: SIZE // 2, :] = 3000  # top half: ndvi 0.5, bottom half: ndvi 0.0
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", nir)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    stats = compute_index("i", raster_bbox()).statistics

    assert stats.mean == pytest.approx(0.25)
    assert stats.min == pytest.approx(0.0)
    assert stats.max == pytest.approx(0.5)
    assert stats.p10 == pytest.approx(0.0)
    assert stats.p90 == pytest.approx(0.5)


def test_nodata_pixels_are_excluded(tmp_path, fake_item):
    """A quarter of the window is nodata; the statistics must not see zeros."""
    nir = np.full((SIZE, SIZE), 3000, dtype="uint16")
    nir[: SIZE // 2, : SIZE // 2] = 0
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", nir), nodata=0),
            "red": asset(write_raster(tmp_path / "red.tif", 1000), nodata=0),
        }
    )

    result = compute_index("i", raster_bbox())

    assert result.pixels.read == SIZE * SIZE
    assert result.pixels.valid == SIZE * SIZE * 3 // 4
    assert result.pixels.nodata_fraction == pytest.approx(0.25)
    assert result.statistics.mean == pytest.approx(0.5)


def test_metadata_of_the_item_is_carried_through(tmp_path, fake_item):
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        },
        item_id="S2A_33TTG_20240628_0_L2A",
    )

    result = compute_index("S2A_33TTG_20240628_0_L2A", raster_bbox())

    assert result.item_id == "S2A_33TTG_20240628_0_L2A"
    assert result.collection == "sentinel-2-l2a"
    assert result.datetime == "2024-01-30T10:09:09Z"
    assert result.cloud_cover == 1.5
    assert result.resolution_m == pytest.approx(PIXEL)
    assert "32633" in result.crs


def test_no_array_is_returned(tmp_path, fake_item):
    """
    Statistics, never pixels: an NDVI raster does not belong in a model's context.

    Checked over `model_dump()` rather than the object, because that dict is what
    `_run_tool` serializes into the tool_result - it is the thing that would actually
    carry a raster into the context if one ever leaked in.
    """
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    result = compute_index("i", raster_bbox())

    dumped = result.model_dump()
    assert not any(isinstance(v, (list, np.ndarray)) for k, v in dumped.items() if k != "bbox")
    # The per-band lists that do survive are two entries long, not two million.
    assert len(dumped["reflectance"]["scale"]) == 2


def test_the_result_is_a_model_not_a_dict(tmp_path, fake_item):
    """Step 7's structured output: every statistic is a declared float, not a loose key."""
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    result = compute_index("i", raster_bbox())

    assert isinstance(result, IndexResult)
    assert isinstance(result.statistics, Statistics)
    assert isinstance(result.statistics.mean, float)


# --- guardrails -------------------------------------------------------------


def test_a_window_past_the_pixel_cap_is_decimated(tmp_path, fake_item, monkeypatch):
    """Too large is handled by reading coarser, not by refusing and wasting a turn."""
    monkeypatch.setattr(module, "MAX_PIXELS", 25)  # 20x20 = 400 pixels, so decimate by 4
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    result = compute_index("i", raster_bbox())

    assert result.pixels.read == 25
    assert result.resolution_m == pytest.approx(PIXEL * 4)
    assert result.statistics.mean == pytest.approx(0.5)


def test_a_small_window_is_read_at_full_resolution(tmp_path, fake_item):
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    assert compute_index("i", raster_bbox()).resolution_m == pytest.approx(PIXEL)


# --- refusals ---------------------------------------------------------------


def test_unknown_index_is_rejected_with_the_available_ones(tmp_path, fake_item):
    with pytest.raises(ValueError, match="ndvi, ndwi"):
        compute_index("i", raster_bbox(), index="nbr")


def test_index_name_is_case_insensitive(tmp_path, fake_item):
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    assert compute_index("i", raster_bbox(), index="NDVI").index == "ndvi"


def test_a_malformed_bbox_is_rejected_before_anything_is_fetched(monkeypatch):
    def boom(_id):
        raise AssertionError("the catalog was queried despite an invalid bbox")

    monkeypatch.setattr(module, "fetch_item", boom)

    with pytest.raises(ValueError, match="below north"):
        compute_index("i", [12.0, 42.0, 13.0, 41.0])


def test_an_item_without_the_required_bands_says_which_it_has(tmp_path, fake_item):
    fake_item({"visual": asset(write_raster(tmp_path / "v.tif", 1000))})

    with pytest.raises(ValueError) as exc:
        compute_index("i", raster_bbox())

    assert "no nir" in str(exc.value)
    assert "visual" in str(exc.value)


def test_a_bbox_outside_the_footprint_is_reported_clearly(tmp_path, fake_item):
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 3000)),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    with pytest.raises(ValueError, match="does not overlap"):
        compute_index("i", [0.0, 0.0, 0.1, 0.1])


def test_a_fully_nodata_window_is_reported_rather_than_averaged(tmp_path, fake_item):
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 0), nodata=0),
            "red": asset(write_raster(tmp_path / "red.tif", 0), nodata=0),
        }
    )

    with pytest.raises(ValueError, match="entirely nodata"):
        compute_index("i", raster_bbox())


def test_a_zero_denominator_does_not_produce_a_statistic(tmp_path, fake_item):
    """nir == -red is degenerate; with no nodata declared it would divide by zero."""
    fake_item(
        {
            "nir": asset(write_raster(tmp_path / "nir.tif", 0, nodata=None)),
            "red": asset(write_raster(tmp_path / "red.tif", 0, nodata=None)),
        }
    )

    with pytest.raises(ValueError, match="entirely nodata"):
        compute_index("i", raster_bbox())


def test_an_unreadable_asset_becomes_a_runtime_error(tmp_path, fake_item):
    fake_item(
        {
            "nir": asset(str(tmp_path / "missing.tif")),
            "red": asset(write_raster(tmp_path / "red.tif", 1000)),
        }
    )

    with pytest.raises(RuntimeError, match="Could not read the rasters"):
        compute_index("i", raster_bbox())


# --- tool definition --------------------------------------------------------


def test_tool_schema_stays_in_sync_with_the_function():
    params = signature(compute_index).parameters
    schema = COMPUTE_INDEX_TOOL["input_schema"]

    assert set(schema["properties"]) <= set(params)

    mandatory = {name for name, p in params.items() if p.default is Parameter.empty}
    assert set(schema["required"]) == mandatory


def test_the_schema_enumerates_exactly_the_implemented_indices():
    assert COMPUTE_INDEX_TOOL["input_schema"]["properties"]["index"]["enum"] == sorted(INDICES)
