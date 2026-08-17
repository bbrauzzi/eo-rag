import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { memo, useEffect, useRef, useState } from 'react'

import type { Basemap, StacCollection } from '../api/types'
import {
  AOI_LAYER,
  FILL_LAYER,
  ensureLayers,
  fitToFeature,
  fitToFeatures,
  setBasemapImagery,
  setFootprints,
  setHighlight,
  syncQuicklooks,
} from '../map/layers'
import { INITIAL_VIEW, VECTOR_STYLE } from '../map/style'

interface Props {
  features: StacCollection | null
  selectedId: string | null
  hoveredId: string | null
  quicklooks: string[]
  basemap: Basemap
  onHover: (id: string | null) => void
  onSelect: (id: string | null) => void
  onToggleQuicklook: (id: string) => void
}

/**
 * The map.
 *
 * Memoised over exactly the props that can change what it draws. That matters more than
 * it looks: a streaming answer dispatches a token several times a second, and without the
 * memo boundary React would walk into this component on every one of them.
 *
 * The instance itself is created once, in an effect with no dependencies and guarded by a
 * ref - which is also what survives StrictMode's double mount, the usual way a second
 * canvas ends up stacked on the first.
 */
export const MapPane = memo(function MapPane({
  features,
  selectedId,
  hoveredId,
  quicklooks,
  basemap,
  onHover,
  onSelect,
  onToggleQuicklook,
}: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<maplibregl.Map | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)

  // Read inside map event handlers, which are bound once and would otherwise close over
  // the first render's props forever.
  const callbacks = useRef({ onHover, onSelect, onToggleQuicklook })
  callbacks.current = { onHover, onSelect, onToggleQuicklook }

  const highlight = useRef({ hovered: null as string | null, selected: null as string | null })

  useEffect(() => {
    if (map.current || !container.current) return

    const instance = new maplibregl.Map({
      container: container.current,
      style: VECTOR_STYLE,
      center: INITIAL_VIEW.center,
      zoom: INITIAL_VIEW.zoom,
      attributionControl: false,
    })
    map.current = instance

    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
    instance.addControl(new maplibregl.AttributionControl({ compact: false }), 'bottom-right')

    // Without this a dead style CDN is indistinguishable from an empty map.
    instance.on('error', (e) => {
      if (!instance.isStyleLoaded()) setFailed(true)
      console.warn('MapLibre:', e.error?.message ?? e)
    })

    // style.load, not load: it fires again after a style change, which is when the
    // source and layers have to be rebuilt.
    instance.on('style.load', () => {
      ensureLayers(instance, null)
      setReady(true)
    })

    for (const layer of [FILL_LAYER, AOI_LAYER]) {
      instance.on('mousemove', layer, (e) => {
        instance.getCanvas().style.cursor = 'pointer'
        const id = e.features?.[0]?.id
        if (id !== undefined) callbacks.current.onHover(String(id))
      })
      instance.on('mouseleave', layer, () => {
        instance.getCanvas().style.cursor = ''
        callbacks.current.onHover(null)
      })
      instance.on('click', layer, (e) => {
        const id = e.features?.[0]?.id
        if (id === undefined) return
        // Clicking a scene selects it and flips its quicklook on or off. The two
        // together are what "click the footprint to see the scene" has to mean: framing
        // it without showing it, or showing it without framing it, are both half a step.
        callbacks.current.onSelect(String(id))
        if (layer === FILL_LAYER) callbacks.current.onToggleQuicklook(String(id))
      })
    }

    return () => {
      instance.remove()
      map.current = null
      setReady(false)
    }
  }, [])

  // New footprints: update the data in place and frame them. No layer churn, no flicker.
  useEffect(() => {
    if (!ready || !map.current) return
    setFootprints(map.current, features)
    fitToFeatures(map.current, features)
  }, [ready, features])

  useEffect(() => {
    if (!ready || !map.current) return
    setHighlight(map.current, highlight.current, { hovered: hoveredId, selected: selectedId })
    highlight.current = { hovered: hoveredId, selected: selectedId }
  }, [ready, hoveredId, selectedId])

  // Selecting a card frames that scene alone.
  useEffect(() => {
    if (!ready || !map.current || !selectedId) return
    const feature = features?.features.find((f) => f.id === selectedId)
    if (feature) fitToFeature(map.current, feature)
  }, [ready, selectedId, features])

  // Reconciled rather than toggled, so it is also correct after a search replaced the
  // features under a quicklook that is still switched on.
  useEffect(() => {
    if (!ready || !map.current) return
    syncQuicklooks(map.current, features, quicklooks)
  }, [ready, features, quicklooks])

  useEffect(() => {
    if (!ready || !map.current) return
    setBasemapImagery(map.current, basemap === 'imagery')
  }, [ready, basemap])

  return (
    <div className="relative h-full w-full">
      <div ref={container} className="h-full w-full" />

      {failed && (
        <p className="border-bad/40 bg-ground/90 text-bad absolute top-3 left-3 rounded border px-2.5 py-1.5 text-[11.5px]">
          The basemap could not be loaded. Footprints are still drawn.
        </p>
      )}

      {/* No attribution rendered here: the imagery source declares its own, and
          MapLibre's AttributionControl shows it exactly while the layer is visible. */}

      {features && features.features.length > 0 && (
        <div className="border-line bg-panel/90 text-ink-dim absolute top-3 left-3 rounded border px-2.5 py-1 font-mono text-[11px]">
          {features.features.filter((f) => f.properties.kind === 'footprint').length} footprints
          <span className="text-ink-faint">
            {quicklooks.length > 0
              ? ` · ${quicklooks.length} quicklook${quicklooks.length > 1 ? 's' : ''}`
              : ' · click one for its quicklook'}
          </span>
        </div>
      )}
    </div>
  )
})
