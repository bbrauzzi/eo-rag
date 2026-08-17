"""
Spectral index tool: reads two bands of a STAC item over a bbox and returns the
statistics of their normalized difference.

The only module that reads pixels. It resolves the item through the catalog
(`stac_search.fetch_item`), opens the two COG assets over HTTP with GDAL's /vsicurl/,
and reads **only the window** covering the requested bbox - a full Sentinel-2 tile is
10980x10980 pixels per band and has no business being pulled down synchronously.

What comes back is statistics, never an array: an NDVI raster is the wrong thing to put
in a model's context, and the questions worth asking of it ("how green is this field in
July") are answered by the summary.

## Reflectance scaling is not optional, and neither is checking it

A normalized difference looks scale-invariant, and for `scale` alone it is:

    (a*s - b*s) / (a*s + b*s) == (a - b) / (a + b)

The **offset does not cancel**:

    (a*s + o - b*s - o) / (a*s + o + b*s + o) == s(a - b) / (s(a + b) + 2o)

Sentinel-2 L2A products from baseline 04.00 onwards apply a BOA offset of -1000 DN, so
`raster:bands` advertises `offset: -0.1` next to `scale: 0.0001`. Skipping that
conversion biases every index computed from such a product, which is why the values are
read from the asset metadata rather than hardcoded.

The metadata can also be wrong, and on the default catalog it is. Earth Search declares
`offset: -0.1` on every sentinel-2-l2a item, but the sentinel-cogs COGs it points at hold
**unshifted** DNs: measured over a Rome suburb, 68% of `red` and 18% of `nir` pixels sit
below DN 1000, with minima down to DN 1. Under a genuinely applied +1000 shift no valid
pixel can be below 1000, since that is reflectance zero. Applying the declared offset
there turns most of the scene negatively reflective and the index diverges - a measured
mean NDVI of -4.8e11, with percentiles far outside the [-1, 1] the quantity is confined
to. Same result on baselines 05.00, 05.09, 05.10 and 05.12, so it is not a
baseline-specific quirk.

So the offset is applied only when the pixels bear it out: negative reflectance is
physically impossible, and a band that produces a lot of it is telling us the offset does
not describe it (`_offsets_fit_the_pixels`). Deciding this from the data rather than
special-casing Earth Search keeps the tool correct for catalogs whose pixels *are*
shifted - Planetary Computer and the Copernicus Data Space serve the same collection the
other way round.

`STAC_API_URL` is configuration, so hardcoding either convention would be a bug waiting
for someone to repoint it.
"""

import numpy as np
import rasterio
from pydantic import BaseModel
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

from app.tools.stac_search import _polygon_from_bbox, _validate_bbox, fetch_item

# Normalized difference indices as the pair of asset keys (a, b) in (a - b) / (a + b).
INDICES = {
    "ndvi": ("nir", "red"),
    "ndwi": ("green", "nir"),
}

# A read is capped at this many pixels per band. Beyond it the window is decimated
# rather than refused: statistics over a bbox do not need full resolution, and telling
# the model "too big, try again" wastes a turn on something we can just handle.
MAX_PIXELS = 4_194_304  # 2048 x 2048

# How much negative reflectance a declared offset may produce before it is judged not to
# describe the pixels. A legitimately applied BOA offset does leave a few dark pixels
# slightly below zero - deep water, deep shadow - but only a few: the measured
# contradiction on Earth Search is 18-68% of the band, two orders of magnitude away from
# the threshold, so nothing here rests on where exactly it sits between the two.
MAX_NEGATIVE_FRACTION = 0.05

WGS84 = CRS.from_epsg(4326)

# GDAL settings for reading COGs over HTTP. Without DISABLE_READDIR_ON_OPEN, GDAL lists
# the whole bucket prefix before every open, which on sentinel-cogs is slow enough to
# dominate the request.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "AWS_NO_SIGN_REQUEST": "YES",
    "VSI_CACHE": "TRUE",
}


def _band_scaling(asset: dict) -> tuple[float, float, float | None]:
    """`scale`, `offset` and `nodata` from the raster extension, with neutral defaults."""
    bands = asset.get("raster:bands") or [{}]
    band = bands[0] if isinstance(bands[0], dict) else {}

    return (
        float(band.get("scale", 1.0)),
        float(band.get("offset", 0.0)),
        band.get("nodata"),
    )


def _offsets_fit_the_pixels(scaled: list[np.ndarray], offsets: list[float]) -> bool:
    """
    Whether the declared BOA offsets describe these pixels.

    Decided for the pair rather than per band, because the convention belongs to the
    product: correcting one band and not the other would build the index out of two
    different quantities, which is worse than being consistently wrong.
    """
    if not any(offsets):
        return False

    for values, offset in zip(scaled, offsets):
        valid = np.ma.count(values)
        if valid and (values + offset < 0).sum() / valid > MAX_NEGATIVE_FRACTION:
            return False

    return True


def _read_window(href: str, bbox: list[float]) -> tuple[np.ndarray, dict]:
    """
    Read the bbox out of one COG as a masked float array, decimating past MAX_PIXELS.

    Returns the array and what it took to get it, so the caller can report the
    resolution the statistics actually describe.
    """
    with rasterio.open(href) as src:
        window = from_bounds(*transform_bounds(WGS84, src.crs, *bbox), transform=src.transform)
        full = rasterio.windows.Window(0, 0, src.width, src.height)

        # intersection() raises rather than returning an empty window, and WindowError is
        # a RasterioError - left alone it would surface as "the asset is unreachable",
        # which is the wrong thing to tell anyone.
        try:
            window = window.intersection(full)
        except rasterio.errors.WindowError as e:
            raise ValueError(
                "The requested bbox does not overlap this item's footprint - "
                "check the coordinates, or pick an item that covers the area."
            ) from e

        if not round(window.width) or not round(window.height):
            raise ValueError(
                "The requested bbox overlaps this item by less than a pixel - "
                "widen it, or pick an item at a finer resolution."
            )

        height, width = round(window.height), round(window.width)
        decimation = max(1, int(np.ceil(np.sqrt(height * width / MAX_PIXELS))))
        out_shape = (max(1, height // decimation), max(1, width // decimation))

        data = src.read(
            1,
            window=window,
            out_shape=out_shape,
            resampling=Resampling.average if decimation > 1 else Resampling.nearest,
            masked=True,
        )
        resolution = abs(src.transform.a) * decimation

        return data.astype("float64"), {
            "crs": src.crs.to_string(),
            "resolution_m": resolution,
            "decimation": decimation,
            "shape": list(out_shape),
        }


class Statistics(BaseModel):
    """The distribution of the index over the window. Never the array itself."""

    mean: float
    std: float
    min: float
    p10: float
    median: float
    p90: float
    max: float


class Bands(BaseModel):
    """Which asset keys went into (a - b) / (a + b), so the result is reproducible."""

    a: str
    b: str


class Reflectance(BaseModel):
    """
    How the DNs were converted, per band, in the order of `Bands`.

    `offset_applied` False with a non-zero `offset_declared` is the interesting case: the
    metadata was overridden because the pixels contradicted it (`_offsets_fit_the_pixels`).
    It is reported rather than decided silently, because it changes the numbers.
    """

    scale: list[float]
    offset_declared: list[float]
    offset_applied: bool


class PixelCounts(BaseModel):
    """How much of the window survived masking - the caveat that qualifies the statistics."""

    read: int
    valid: int
    nodata_fraction: float


class IndexResult(BaseModel):
    """
    What `compute_index` returns.

    A model rather than a dict for the same reason as `ItemSummary`: two consumers want
    different parts of it. The model gets the whole thing as JSON; `index_footprint` reads
    the bbox and the statistics to draw the AOI. Every field here is a measurement or the
    provenance of one - there is deliberately no raster.
    """

    index: str
    bands: Bands
    item_id: str | None = None
    collection: str | None = None
    datetime: str | None = None
    cloud_cover: float | None = None
    bbox: list[float]
    crs: str
    resolution_m: float
    reflectance: Reflectance
    pixels: PixelCounts
    statistics: Statistics


def _statistics(values: np.ndarray) -> Statistics:
    """Percentiles included: the mean of an index over mixed land cover hides a lot."""
    p10, median, p90 = (float(v) for v in np.percentile(values, [10, 50, 90]))

    return Statistics(
        mean=float(values.mean()),
        std=float(values.std()),
        min=float(values.min()),
        p10=p10,
        median=median,
        p90=p90,
        max=float(values.max()),
    )


def index_footprint(result: IndexResult) -> dict:
    """
    The area a `compute_index` result was measured over, as a GeoJSON Feature.

    The counterpart of `item_footprint`: same shape, different `kind`, so the map can
    hold both in one source and style them apart. The AOI is usually a few kilometres
    inside a 110 km tile, which is why it is worth drawing separately from the scene
    footprint it sits on.
    """
    return {
        "type": "Feature",
        "id": f"{result.item_id}:aoi",
        "bbox": result.bbox,
        "geometry": _polygon_from_bbox(result.bbox),
        "properties": {
            "kind": "aoi",
            "id": f"{result.item_id}:aoi",
            "item_id": result.item_id,
            "collection": result.collection,
            "datetime": result.datetime,
            "index": result.index,
            "resolution_m": result.resolution_m,
            # Dumped rather than passed as the model: this Feature is handed to the
            # frontend as JSON, and a pydantic object inside it would not serialize.
            "statistics": result.statistics.model_dump(),
        },
    }


def compute_index(item_id: str, bbox, index: str = "ndvi") -> IndexResult:
    """
    Compute a normalized difference index over `bbox` for one STAC item.

    Raises ValueError for an unknown index, a malformed bbox, an item that does not
    exist or does not carry the required bands, and a bbox that misses its footprint.
    RuntimeError comes from the catalog or from reading the rasters.
    """
    index = index.lower()
    if index not in INDICES:
        raise ValueError(f"Unknown index {index!r}. Available: {', '.join(sorted(INDICES))}")

    bbox = _validate_bbox(bbox)
    item = fetch_item(item_id)
    assets = item.get("assets") or {}

    first_key, second_key = INDICES[index]
    missing = [k for k in (first_key, second_key) if k not in assets]
    if missing:
        raise ValueError(
            f"Item {item_id!r} has no {' and no '.join(missing)} asset, so {index} "
            f"cannot be computed from it. Available assets: {', '.join(sorted(assets))}"
        )

    try:
        with rasterio.Env(**GDAL_ENV):
            scaled, reads, scales, offsets = [], [], [], []
            for key in (first_key, second_key):
                asset = assets[key]
                data, read_info = _read_window(asset["href"], bbox)
                scale, offset, nodata = _band_scaling(asset)

                if nodata is not None:
                    data = np.ma.masked_equal(data, nodata)
                # Scale now, offset once both bands are in: the decision is joint.
                scaled.append(data * scale)
                reads.append(read_info)
                scales.append(scale)
                offsets.append(offset)
    except rasterio.errors.RasterioError as e:
        raise RuntimeError(
            f"Could not read the rasters of {item_id!r}: {e}. "
            "The asset may be unreachable, or GDAL may lack HTTP access."
        ) from e

    offset_applied = _offsets_fit_the_pixels(scaled, offsets)
    first, second = [v + o for v, o in zip(scaled, offsets)] if offset_applied else scaled

    if first.shape != second.shape:
        # Bands at different ground sample distances land on different grids; the
        # indices here pair 10 m bands, so a mismatch means something unexpected.
        raise RuntimeError(
            f"Band windows do not line up for {index}: {first.shape} against "
            f"{second.shape}. The two assets are probably at different resolutions."
        )

    total = first.size
    # Negative reflectance does not exist, so a pixel showing it is unusable whatever
    # produced it - sensor noise in a dark pixel, or an offset that survived the check on
    # the few percent it is allowed. Dropping those also makes the denominator a sum of
    # non-negative terms, which is what confines the result to [-1, 1] by construction
    # rather than by clipping after the fact.
    first, second = np.ma.masked_less(first, 0), np.ma.masked_less(second, 0)

    denominator = first + second
    # A zero denominator is degenerate, not just masked input: keep it out of the stats.
    values = np.ma.masked_equal(denominator, 0)
    values = ((first - second) / values).compressed()

    if not values.size:
        raise ValueError(
            f"No valid pixels for {index} over this bbox on {item_id!r} - the window is "
            "entirely nodata. It may fall outside the acquired swath, or be fully masked."
        )

    return IndexResult(
        index=index,
        bands=Bands(a=first_key, b=second_key),
        item_id=item.get("id"),
        collection=item.get("collection"),
        datetime=(item.get("properties") or {}).get("datetime"),
        cloud_cover=(item.get("properties") or {}).get("eo:cloud_cover"),
        bbox=bbox,
        crs=reads[0]["crs"],
        resolution_m=reads[0]["resolution_m"],
        reflectance=Reflectance(
            scale=scales,
            offset_declared=offsets,
            offset_applied=offset_applied,
        ),
        pixels=PixelCounts(
            read=total,
            valid=int(values.size),
            nodata_fraction=round(1 - values.size / total, 4),
        ),
        statistics=_statistics(values),
    )


COMPUTE_INDEX_TOOL = {
    "name": "compute_index",
    "description": (
        "Compute a spectral index over an area from one satellite scene, and return its "
        "statistics (mean, median, percentiles, range) - not an image. NDVI measures "
        "vegetation vigour, NDWI measures open water. Find the scene with stac_search "
        "first: this needs the identifier of a specific item. Reading pixels is slow "
        "compared to a catalog search, so use it when the question is about the state of "
        "the ground, not about which data exists."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "string",
                "description": (
                    "Identifier of the STAC item to read, as returned by stac_search "
                    "(for example 'S2B_33TTG_20240130_0_L2A')."
                ),
            },
            "bbox": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": (
                    "Area to compute over, as [west, south, east, north] in decimal "
                    "degrees (WGS84). Must fall inside the item's footprint. Keep it "
                    "tight: a smaller area is read faster and at full resolution."
                ),
            },
            "index": {
                "type": "string",
                "enum": sorted(INDICES),
                "description": "Which index to compute. Defaults to ndvi.",
            },
        },
        "required": ["item_id", "bbox"],
    },
}
