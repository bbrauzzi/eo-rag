import { useCallback, useState } from 'react'

import type { AoiProperties, FootprintProperties, StacFeature } from '../api/types'
import { previewUrl } from '../api/types'
import { AssetList } from './AssetList'

interface Props {
  features: StacFeature[]
  selectedId: string | null
  hoveredId: string | null
  quicklooks: string[]
  onHover: (id: string | null) => void
  onSelect: (id: string | null) => void
  onToggleQuicklook: (id: string) => void
}

/** Which card's asset popover is open, and the rect it hangs off. */
interface OpenAssets {
  itemId: string
  anchor: { left: number; top: number; bottom: number }
}

/** Scene ids are long and end in the part that distinguishes them. */
function shortId(id: string): string {
  return id.length > 30 ? `${id.slice(0, 14)}…${id.slice(-13)}` : id
}

function day(value: string | null): string {
  return value ? value.slice(0, 10) : '—'
}

function CloudBar({ value }: { value: number | null }) {
  if (value === null || value === undefined) return <span className="text-ink-faint">—</span>

  return (
    <span className="flex items-center gap-1.5">
      <span className="bg-line h-1 w-10 overflow-hidden rounded-full">
        <span
          className="bg-accent-dim block h-full rounded-full"
          style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
        />
      </span>
      {/* One decimal under 10%: rounding 1.58 to "2%" throws away the distinction
          between a nearly clear scene and a merely good one. */}
      <span className="tnum text-ink-dim">{value.toFixed(value < 10 ? 1 : 0)}%</span>
    </span>
  )
}

function FootprintCard({
  properties,
  shown,
  onToggle,
  assetsOpen,
  onOpenAssets,
}: {
  properties: FootprintProperties
  shown: boolean
  onToggle: () => void
  assetsOpen: boolean
  onOpenAssets: (anchor: OpenAssets['anchor'] | null) => void
}) {
  return (
    <>
      <div className="bg-ground relative h-20 w-full overflow-hidden">
        {properties.thumbnail ? (
          <img
            // Through the API, exactly as the map loads it: one same-origin URL, one
            // cache entry, and no third party's CORS headers in the way.
            src={previewUrl(properties.id)}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover opacity-85"
          />
        ) : (
          <div className="text-ink-faint flex h-full items-center justify-center text-[10px]">
            no preview
          </div>
        )}

        {/* The same toggle as clicking the footprint on the map, where it is discoverable.
            stopPropagation so it does not also fire the card's select-and-zoom. */}
        {properties.thumbnail && (
          <span
            role="button"
            tabIndex={0}
            title={shown ? 'Hide the quicklook on the map' : 'Show the quicklook on the map'}
            onClick={(e) => {
              e.stopPropagation()
              onToggle()
            }}
            onKeyDown={(e) => {
              if (e.key !== 'Enter' && e.key !== ' ') return
              e.preventDefault()
              e.stopPropagation()
              onToggle()
            }}
            className={[
              'absolute top-1 right-1 rounded border px-1.5 py-0.5 text-[9.5px] tracking-wide uppercase backdrop-blur-sm transition-colors',
              shown
                ? 'border-accent bg-accent/25 text-accent'
                : 'border-line bg-ground/75 text-ink-faint hover:text-ink-dim',
            ].join(' ')}
          >
            {shown ? 'on map' : 'preview'}
          </span>
        )}
      </div>

      <div className="space-y-1 px-2.5 py-2">
        <p className="text-ink truncate font-mono text-[11px]" title={properties.id}>
          {shortId(properties.id)}
        </p>
        <p className="tnum text-ink-dim font-mono text-[10.5px]">{day(properties.datetime)}</p>
        <div className="flex items-center justify-between gap-2 font-mono text-[10.5px]">
          <CloudBar value={properties.cloud_cover} />

          {/* role="button" and not a <button>: the card itself is one, and nesting is
              invalid. stopPropagation so opening the list does not also select the
              scene and fly the map to it. */}
          <span
            role="button"
            tabIndex={0}
            title="List this scene's downloadable assets"
            onClick={(e) => {
              e.stopPropagation()
              onOpenAssets(assetsOpen ? null : e.currentTarget.getBoundingClientRect())
            }}
            onKeyDown={(e) => {
              if (e.key !== 'Enter' && e.key !== ' ') return
              e.preventDefault()
              e.stopPropagation()
              onOpenAssets(assetsOpen ? null : e.currentTarget.getBoundingClientRect())
            }}
            className={[
              'shrink-0 rounded border px-1.5 py-0.5 text-[9.5px] tracking-wide uppercase transition-colors',
              assetsOpen
                ? 'border-accent bg-accent/25 text-accent'
                : 'border-line text-ink-faint hover:text-ink-dim',
            ].join(' ')}
          >
            ⤓ assets
          </span>
        </div>
      </div>
    </>
  )
}

function AoiCard({ properties }: { properties: AoiProperties }) {
  const stats = properties.statistics ?? {}

  return (
    <div className="space-y-1.5 px-2.5 py-2.5">
      <p className="text-aoi font-mono text-[11px] tracking-wide uppercase">
        {properties.index} · area
      </p>
      <p className="text-ink-dim truncate font-mono text-[10.5px]" title={properties.item_id}>
        {shortId(properties.item_id)}
      </p>
      <dl className="tnum grid grid-cols-[auto_1fr] gap-x-2 font-mono text-[10.5px]">
        {(['mean', 'median', 'p10', 'p90'] as const)
          .filter((key) => typeof stats[key] === 'number')
          .map((key) => (
            <div key={key} className="contents">
              <dt className="text-ink-faint">{key}</dt>
              <dd className="text-ink text-right">{stats[key].toFixed(3)}</dd>
            </div>
          ))}
      </dl>
    </div>
  )
}

/**
 * The turn's footprints as cards, paired with the polygons on the map: hovering either
 * highlights both. Both directions write the same two pieces of state in the reducer, so
 * there is one source of truth and no feedback loop between them.
 */
export function ItemCards({
  features,
  selectedId,
  hoveredId,
  quicklooks,
  onHover,
  onSelect,
  onToggleQuicklook,
}: Props) {
  const [openAssets, setOpenAssets] = useState<OpenAssets | null>(null)

  // Stable, so AssetList's listener effect does not tear down and rebind on every
  // render of this list.
  const closeAssets = useCallback(() => setOpenAssets(null), [])

  if (features.length === 0) return null

  return (
    <div className="scrollbar-thin -mx-1 mt-2 flex gap-2 overflow-x-auto px-1 pb-1">
      {openAssets && (
        <AssetList
          itemId={openAssets.itemId}
          anchor={openAssets.anchor}
          onClose={closeAssets}
        />
      )}

      {features.map((feature) => {
        const active = feature.id === selectedId
        const warm = feature.id === hoveredId
        const aoi = feature.properties.kind === 'aoi'

        return (
          <button
            key={feature.id}
            type="button"
            data-feature-id={feature.id}
            onMouseEnter={() => onHover(feature.id)}
            onMouseLeave={() => onHover(null)}
            onClick={() => onSelect(active ? null : feature.id)}
            className={[
              'w-[168px] shrink-0 overflow-hidden rounded-md border bg-panel text-left transition-colors',
              active
                ? aoi
                  ? 'border-aoi'
                  : 'border-accent'
                : warm
                  ? 'border-ink-faint'
                  : 'border-line',
            ].join(' ')}
          >
            {feature.properties.kind === 'aoi' ? (
              <AoiCard properties={feature.properties} />
            ) : (
              <FootprintCard
                properties={feature.properties}
                shown={quicklooks.includes(feature.id)}
                onToggle={() => onToggleQuicklook(feature.id)}
                assetsOpen={openAssets?.itemId === feature.id}
                onOpenAssets={(anchor) =>
                  setOpenAssets(anchor && { itemId: feature.id, anchor })
                }
              />
            )}
          </button>
        )
      })}
    </div>
  )
}
