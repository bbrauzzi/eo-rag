/**
 * The wire contract of POST /ask/stream, mirroring the events yielded by
 * `stream_answer` in app/agents/graph.py. Keep the two in step: the backend is the
 * definition, this is the transcription.
 */

import type { Geometry } from 'geojson'

export type Basemap = 'vector' | 'imagery'

export interface FootprintProperties {
  kind: 'footprint'
  id: string
  collection: string | null
  datetime: string | null
  cloud_cover: number | null
  platform: string | null
  /**
   * The catalog's own href. Not what is displayed - use `previewUrl` below, which goes
   * through our API - but it is how we know a preview exists at all.
   */
  thumbnail: string | null
}

/**
 * Where the browser actually loads a scene preview from. Same origin, so no CORS is
 * involved: the map needs the image as a WebGL texture, and a catalog is under no
 * obligation to send the headers that would allow.
 */
export const previewUrl = (id: string) => `/preview/${encodeURIComponent(id)}`

/** One downloadable asset of a scene, as GET /items/{id}/assets lists them. */
export interface StacAsset {
  key: string
  title: string | null
  type: string | null
  roles: string[]
  /**
   * The catalog's own href. Shown so it can be copied into GDAL as `/vsicurl/…` rather
   * than downloaded — not where the browser fetches from, which is `assetUrl` below.
   */
  href: string
}

export const assetsUrl = (id: string) => `/items/${encodeURIComponent(id)}/assets`

/**
 * Where an asset is downloaded from: our API, never the catalog. It is what turns
 * Sentinel-1's `s3://` hrefs into something a browser can follow at all, and what puts
 * the scene id in the filename — every Sentinel-2 red band is otherwise `B04.tif`.
 */
export const assetUrl = (id: string, key: string) =>
  `${assetsUrl(id)}/${encodeURIComponent(key)}`

export interface AoiProperties {
  kind: 'aoi'
  id: string
  item_id: string
  collection: string | null
  datetime: string | null
  index: string
  resolution_m: number | null
  statistics: Record<string, number>
}

export type FeatureProperties = FootprintProperties | AoiProperties

export interface StacFeature {
  type: 'Feature'
  id: string
  bbox: [number, number, number, number] | null
  geometry: Geometry
  properties: FeatureProperties
}

export interface StacCollection {
  type: 'FeatureCollection'
  features: StacFeature[]
}

export type StreamEvent =
  | { type: 'start'; conversation_id: string }
  | { type: 'token'; text: string }
  | { type: 'tool_start'; id: string; name: string; input: Record<string, unknown> }
  | { type: 'tool_end'; id: string; name: string; ok: boolean; ms: number; detail: string | null }
  | { type: 'features'; collection: StacCollection }
  | { type: 'done'; answer: string; sources: string[]; steps: number }
  | { type: 'error'; message: string }

export interface AskRequest {
  question: string
  conversation_id?: string | null
}
