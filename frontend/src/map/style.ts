import type { RasterSourceSpecification, StyleSpecification } from 'maplibre-gl'

/**
 * Basemaps, chosen under one constraint: no API key, so the app stays as self-contained
 * as the rest of the project and nothing has to be provisioned to run it.
 *
 * OpenFreeMap is the default - a full vector style, unmetered, OSM data, attribution
 * shipped inside the style. Esri World Imagery is the optional backdrop, because real
 * pixels under a footprint is the right context for EO work; it is off by default since
 * the open endpoint is tolerated rather than licensed, and its attribution is required
 * whenever it is visible.
 *
 * Plain OSM raster tiles were rejected: the OSMF tile usage policy forbids systematic
 * application use, and a bright roads-first basemap swallows the footprints anyway.
 */
export const VECTOR_STYLE = 'https://tiles.openfreemap.org/styles/dark'

export const IMAGERY_SOURCE_ID = 'esri-imagery'
export const IMAGERY_LAYER_ID = 'esri-imagery-layer'

export const IMAGERY_ATTRIBUTION = 'Esri, Maxar, Earthstar Geographics'

export const IMAGERY_SOURCE: RasterSourceSpecification = {
  type: 'raster',
  tiles: [
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  ],
  tileSize: 256,
  maxzoom: 19,
  attribution: IMAGERY_ATTRIBUTION,
}

export const INITIAL_VIEW = {
  center: [12.5, 41.9] as [number, number],
  zoom: 4.2,
}

/**
 * A style object the map can be created with immediately, so it renders before the
 * remote style has been fetched. Swapping to the real one afterwards is a single
 * setStyle at startup, well before any source of ours exists.
 */
export const BOOTSTRAP_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [
    {
      id: 'background',
      type: 'background',
      paint: { 'background-color': '#0b0e11' },
    },
  ],
}
