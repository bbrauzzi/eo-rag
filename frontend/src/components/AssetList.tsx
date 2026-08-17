import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

import type { StacAsset } from '../api/types'
import { assetsUrl, assetUrl } from '../api/types'

/**
 * The downloadable assets of one scene, as a popover anchored to its card.
 *
 * Rendered into a portal on `document.body` rather than inside the card, for two
 * reasons that would each break it on their own: the card strip is `overflow-x-auto`,
 * which clips anything a 168px card tries to overflow, and the card itself is a
 * `<button>` — a `<a>` or a second `<button>` nested inside one is invalid HTML, and
 * browsers resolve it by dropping the inner control's activation.
 *
 * The list is fetched when the popover opens, not with the turn: 38 assets per scene is
 * a catalog round trip each, for a panel most scenes never have opened.
 */

interface Props {
  itemId: string
  /** Viewport coordinates of the trigger, from getBoundingClientRect(). */
  anchor: { left: number; top: number; bottom: number }
  onClose: () => void
}

const WIDTH = 320
const MARGIN = 8
/** Room for ~9 rows. The list scrolls past that rather than growing. */
const PREFERRED_HEIGHT = 352

/** `image/tiff; application=geotiff; profile=cloud-optimized` is not a label. */
function shortType(type: string | null): string {
  if (!type) return '—'

  const [base] = type.split(';')
  const subtype = base.trim().split('/')[1] ?? base

  return subtype.replace(/^(x-|vnd\.)/, '').toUpperCase()
}

export function AssetList({ itemId, anchor, onClose }: Props) {
  const [assets, setAssets] = useState<StacAsset[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // Abandoned if the popover closes or the scene changes before the catalog answers,
    // so a slow response cannot land in a popover that is now showing another scene.
    const controller = new AbortController()

    setAssets(null)
    setError(null)

    fetch(assetsUrl(itemId), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
        return (await response.json()) as StacAsset[]
      })
      .then(setAssets)
      .catch((e: Error) => {
        if (e.name !== 'AbortError') setError(e.message)
      })

    return () => controller.abort()
  }, [itemId])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }

    // Anchored to a rect measured once, so anything that moves the card underneath it
    // closes the popover rather than leaving it pointing at empty space. Capture phase:
    // the card strip scrolls in its own container, and that scroll does not bubble.
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onClose, true)
    window.addEventListener('resize', onClose)

    return () => {
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onClose, true)
      window.removeEventListener('resize', onClose)
    }
  }, [onClose])

  // The height is taken from the room actually available on the side it opens towards,
  // rather than a fixed max against a separate flip threshold: the two disagreed, and a
  // popover that had cleared the threshold by 30px still ran 60px off the bottom. The
  // list scrolls, so a short popover is fine and an overflowing one is not.
  const spaceBelow = window.innerHeight - anchor.bottom - MARGIN * 2
  const spaceAbove = anchor.top - MARGIN * 2
  const below = spaceBelow >= spaceAbove

  // Never past the right edge either — the last card in the strip is within 320px of it.
  const left = Math.min(anchor.left, window.innerWidth - WIDTH - MARGIN)

  return createPortal(
    <>
      {/* Catches the click that dismisses it, so the click does not also land on
          whatever was underneath — a card, or the map. */}
      <div className="fixed inset-0 z-40" onClick={onClose} onContextMenu={onClose} />

      <div
        role="dialog"
        aria-label={`Assets of ${itemId}`}
        style={{
          left: Math.max(MARGIN, left),
          ...(below ? { top: anchor.bottom + 4 } : { bottom: window.innerHeight - anchor.top + 4 }),
          width: WIDTH,
          maxHeight: Math.min(PREFERRED_HEIGHT, below ? spaceBelow : spaceAbove),
        }}
        className="border-line bg-panel fixed z-50 overflow-y-auto rounded-md border shadow-xl"
      >
        <p className="border-line text-ink-faint sticky top-0 border-b bg-panel px-2.5 py-1.5 font-mono text-[10px] tracking-wide uppercase">
          assets · {assets ? `${assets.length}` : '…'}
        </p>

        {error && <p className="px-2.5 py-2 font-mono text-[10.5px] text-red-400">{error}</p>}

        {!assets && !error && (
          <p className="text-ink-faint px-2.5 py-2 font-mono text-[10.5px]">loading…</p>
        )}

        {assets?.length === 0 && (
          <p className="text-ink-faint px-2.5 py-2 font-mono text-[10.5px]">
            this scene publishes no assets
          </p>
        )}

        <ul>
          {assets?.map((asset) => (
            <li key={asset.key} className="border-line border-b last:border-b-0">
              <a
                href={assetUrl(itemId, asset.key)}
                // Same origin, so the browser honours this and names the file after the
                // scene. A cross-origin href would ignore it and navigate instead.
                download
                title={`Download ${asset.key}\n${asset.href}`}
                className="hover:bg-ground flex items-baseline gap-2 px-2.5 py-1.5 transition-colors"
              >
                <span className="text-ink shrink-0 font-mono text-[11px]">{asset.key}</span>
                <span className="text-ink-dim min-w-0 flex-1 truncate text-[10.5px]">
                  {asset.title ?? asset.roles.join(', ')}
                </span>
                <span className="text-ink-faint tnum shrink-0 font-mono text-[9.5px]">
                  {shortType(asset.type)}
                </span>
                <span className="text-accent shrink-0 text-[11px]">⤓</span>
              </a>
            </li>
          ))}
        </ul>
      </div>
    </>,
    document.body,
  )
}
