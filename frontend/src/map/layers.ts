import type { FeatureCollection, Geometry } from 'geojson'
import type { LngLatBoundsLike, Map as MapLibreMap } from 'maplibre-gl'

import type { StacCollection, StacFeature } from '../api/types'
import { previewUrl } from '../api/types'
import { IMAGERY_LAYER_ID, IMAGERY_SOURCE, IMAGERY_SOURCE_ID } from './style'

export const SOURCE_ID = 'stac'
export const FILL_LAYER = 'stac-fill'
export const LINE_LAYER = 'stac-line'
export const AOI_LAYER = 'stac-aoi'

const QUICKLOOK_PREFIX = 'quicklook:'

/** What MapLibre is handed: the same features with a spec-clean bbox (absent, not null). */
type DrawableCollection = FeatureCollection<Geometry, Record<string, unknown>>

const EMPTY: DrawableCollection = { type: 'FeatureCollection', features: [] }

/**
 * A tripwire, not a fix. The catalog's own GeoJSON is already [lon, lat] and so is the
 * backend's bbox fallback, so anything failing here means a swap was introduced upstream
 * - and the symptom, footprints in the Gulf of Guinea, is a wrong picture rather than an
 * error. Cheaper to catch at the boundary than to recognise on the screen.
 */
function isLonLat(feature: StacFeature): boolean {
  const coordinates = (feature.geometry as { coordinates?: unknown }).coordinates
  let node: unknown = coordinates

  while (Array.isArray(node) && Array.isArray(node[0])) node = node[0]
  if (!Array.isArray(node) || node.length < 2) return true

  const [lon, lat] = node as number[]
  return Math.abs(lon) <= 180 && Math.abs(lat) <= 90
}

function sanitise(collection: StacCollection | null): DrawableCollection {
  if (!collection) return EMPTY

  const features = collection.features
    .filter((feature) => {
      if (isLonLat(feature)) return true
      console.warn(`Dropping ${feature.id}: coordinates are not [lon, lat].`, feature.geometry)
      return false
    })
    .map(({ bbox, ...feature }) => ({
      ...feature,
      properties: feature.properties as unknown as Record<string, unknown>,
      ...(bbox ? { bbox } : {}),
    }))

  return { type: 'FeatureCollection', features }
}

/**
 * Create the source and the three layers, replacing them if they are already there.
 *
 * The guarded teardown - every layer removed before the source it depends on - is what
 * makes this safe to call again after a style change, which drops everything the style
 * did not declare. Steady-state updates do not come through here; they go to setData.
 */
export function ensureLayers(map: MapLibreMap, collection: StacCollection | null) {
  for (const id of [FILL_LAYER, LINE_LAYER, AOI_LAYER]) {
    if (map.getLayer(id)) map.removeLayer(id)
  }
  if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID)

  map.addSource(SOURCE_ID, {
    type: 'geojson',
    data: sanitise(collection),
    // Lifts the feature id out of properties so setFeatureState can address it, which is
    // what hovering a card highlights the polygon through.
    promoteId: 'id',
  })

  const footprints = ['==', ['get', 'kind'], 'footprint'] as never

  map.addLayer({
    id: FILL_LAYER,
    type: 'fill',
    source: SOURCE_ID,
    filter: footprints,
    paint: {
      'fill-color': '#4cc9f0',
      'fill-opacity': [
        'case',
        ['boolean', ['feature-state', 'selected'], false],
        0.18,
        ['boolean', ['feature-state', 'hover'], false],
        0.14,
        0.06,
      ],
    },
  })

  map.addLayer({
    id: LINE_LAYER,
    type: 'line',
    source: SOURCE_ID,
    filter: footprints,
    paint: {
      'line-color': '#4cc9f0',
      'line-width': [
        'case',
        ['boolean', ['feature-state', 'selected'], false],
        2.6,
        ['boolean', ['feature-state', 'hover'], false],
        2.2,
        1.2,
      ],
    },
  })

  map.addLayer({
    id: AOI_LAYER,
    type: 'line',
    source: SOURCE_ID,
    filter: ['==', ['get', 'kind'], 'aoi'] as never,
    paint: {
      'line-color': '#f4a261',
      'line-width': 2,
      'line-dasharray': [2, 2],
    },
  })
}

export function setFootprints(map: MapLibreMap, collection: StacCollection | null) {
  const source = map.getSource(SOURCE_ID)
  if (source && 'setData' in source) {
    ;(source as { setData: (data: DrawableCollection) => void }).setData(sanitise(collection))
  }
}

function boundsOf(features: StacFeature[]): LngLatBoundsLike | null {
  let west = 180
  let south = 90
  let east = -180
  let north = -90
  let seen = false

  for (const feature of features) {
    const bbox = feature.bbox
    if (!bbox) continue
    seen = true
    west = Math.min(west, bbox[0])
    south = Math.min(south, bbox[1])
    east = Math.max(east, bbox[2])
    north = Math.max(north, bbox[3])
  }

  return seen ? [west, south, east, north] : null
}

/**
 * Frame what the turn produced. If it measured an index, frame that area rather than the
 * scenes: an AOI is a few kilometres inside a 110 km tile, and fitting the tile would
 * hide the thing just measured.
 */
export function fitToFeatures(map: MapLibreMap, collection: StacCollection | null) {
  if (!collection || collection.features.length === 0) return

  const aoi = collection.features.filter((f) => f.properties.kind === 'aoi')
  const bounds = boundsOf(aoi.length > 0 ? aoi : collection.features)
  if (!bounds) return

  map.fitBounds(bounds, { padding: 50, maxZoom: 12, duration: 1500 })
}

export function fitToFeature(map: MapLibreMap, feature: StacFeature) {
  const bounds = boundsOf([feature])
  if (bounds) map.fitBounds(bounds, { padding: 50, maxZoom: 12, duration: 1500 })
}

/** Drive the paint expressions above from the ids the reducer holds. */
export function setHighlight(
  map: MapLibreMap,
  previous: { hovered: string | null; selected: string | null },
  next: { hovered: string | null; selected: string | null },
) {
  const apply = (id: string | null, key: 'hover' | 'selected', value: boolean) => {
    if (!id) return
    try {
      map.setFeatureState({ source: SOURCE_ID, id }, { [key]: value })
    } catch {
      // The feature is gone: a new search replaced the collection between renders.
    }
  }

  if (previous.hovered !== next.hovered) {
    apply(previous.hovered, 'hover', false)
    apply(next.hovered, 'hover', true)
  }
  if (previous.selected !== next.selected) {
    apply(previous.selected, 'selected', false)
    apply(next.selected, 'selected', true)
  }
}

/**
 * The four corners of a footprint, as MapLibre's image source wants them:
 * top-left, top-right, bottom-right, bottom-left.
 *
 * A Sentinel-2 footprint is a quadrilateral rotated a few degrees off north, and the
 * quicklook is a rendering of exactly that quad - so laying it on the corners fits it
 * properly, where the bbox would stretch it into the rotation. Anything that is not a
 * clean four-corner ring (a MultiPolygon from the antimeridian split, a swath outline)
 * falls back to the bbox, which is always axis-aligned and always right for one.
 */
type Corners = [[number, number], [number, number], [number, number], [number, number]]

export function imageCorners(feature: StacFeature): Corners | null {
  const geometry = feature.geometry

  if (geometry.type === 'Polygon') {
    const ring = geometry.coordinates[0] ?? []
    // GeoJSON rings repeat their first point to close; the corners are what is left.
    const corners = ring.slice(0, -1) as [number, number][]

    if (corners.length === 4) {
      // Sort north to south, then west to east within each pair. Exact for an
      // axis-aligned quad and correct for the small rotation of an MGRS tile.
      const [n1, n2, s1, s2] = [...corners].sort((a, b) => b[1] - a[1])
      const [nw, ne] = n1[0] <= n2[0] ? [n1, n2] : [n2, n1]
      const [sw, se] = s1[0] <= s2[0] ? [s1, s2] : [s2, s1]
      return [nw, ne, se, sw]
    }
  }

  const bbox = feature.bbox
  if (!bbox) return null

  const [west, south, east, north] = bbox
  return [
    [west, north],
    [east, north],
    [east, south],
    [west, south],
  ]
}

/**
 * Reconcile the quicklooks on the map with the ones asked for.
 *
 * Written as a reconciliation rather than an add/remove pair because the desired set and
 * the drawn set can drift apart on their own: a new search replaces the features under
 * a quicklook that is still switched on, and a style change drops every source we added.
 * Calling this whenever either changes is then always correct and always idempotent.
 */
export function syncQuicklooks(
  map: MapLibreMap,
  collection: StacCollection | null,
  wanted: readonly string[],
) {
  const available = new Map((collection?.features ?? []).map((f) => [f.id, f]))
  const desired = new Set(
    wanted.filter((id) => available.get(id)?.properties.kind === 'footprint'),
  )

  for (const layer of map.getStyle().layers ?? []) {
    if (!layer.id.startsWith(QUICKLOOK_PREFIX)) continue
    if (desired.has(layer.id.slice(QUICKLOOK_PREFIX.length))) continue

    map.removeLayer(layer.id)
    if (map.getSource(layer.id)) map.removeSource(layer.id)
  }

  for (const id of desired) {
    const layerId = `${QUICKLOOK_PREFIX}${id}`
    if (map.getLayer(layerId)) continue

    const feature = available.get(id)!
    const properties = feature.properties
    // thumbnail says a preview exists; previewUrl is where we are allowed to load it
    // from - our own origin, because this image ends up as a WebGL texture.
    const hasPreview = properties.kind === 'footprint' && properties.thumbnail
    const coordinates = imageCorners(feature)
    if (!hasPreview || !coordinates) continue

    map.addSource(layerId, { type: 'image', url: previewUrl(feature.id), coordinates })
    // Under the outlines, so the footprint stays legible on top of its own preview.
    map.addLayer(
      {
        id: layerId,
        type: 'raster',
        source: layerId,
        paint: { 'raster-opacity': 0.9, 'raster-fade-duration': 300 },
      },
      map.getLayer(FILL_LAYER) ? FILL_LAYER : undefined,
    )
  }
}

/** Which scenes currently have a quicklook drawn - the map's own answer, not the state's. */
export function drawnQuicklooks(map: MapLibreMap): string[] {
  return (map.getStyle().layers ?? [])
    .filter((layer) => layer.id.startsWith(QUICKLOOK_PREFIX))
    .map((layer) => layer.id.slice(QUICKLOOK_PREFIX.length))
}

/**
 * Add the imagery as a layer inside the current style rather than swapping styles:
 * setStyle would tear down our source and all three layers on every toggle.
 */
export function setBasemapImagery(map: MapLibreMap, visible: boolean) {
  if (!map.getSource(IMAGERY_SOURCE_ID)) {
    map.addSource(IMAGERY_SOURCE_ID, IMAGERY_SOURCE)
  }

  if (!map.getLayer(IMAGERY_LAYER_ID)) {
    // Under everything of ours, over the basemap's own ground.
    const first = map.getStyle().layers?.find((l) => l.id !== 'background')?.id
    map.addLayer(
      {
        id: IMAGERY_LAYER_ID,
        type: 'raster',
        source: IMAGERY_SOURCE_ID,
        layout: { visibility: 'none' },
      },
      first,
    )
  }

  map.setLayoutProperty(IMAGERY_LAYER_ID, 'visibility', visible ? 'visible' : 'none')
}
