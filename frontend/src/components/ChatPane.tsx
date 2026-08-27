import { useEffect, useRef } from 'react'

import type { Turn as TurnData } from '../hooks/useConversation'
import { Composer } from './Composer'
import { Turn } from './Turn'

interface Props {
  turns: TurnData[]
  selectedId: string | null
  hoveredId: string | null
  quicklooks: string[]
  streaming: boolean
  onAsk: (question: string) => void
  onStop: () => void
  onHover: (id: string | null) => void
  onSelect: (id: string | null) => void
  onToggleQuicklook: (id: string) => void
}

function Empty() {
  return (
    <div className="flex h-full flex-col items-center justify-center px-8 text-center">
      <p className="text-ink text-[15px] font-medium">Ask about Earth observation data.</p>
      <p className="text-ink-dim mt-2 max-w-sm text-[13px] leading-relaxed">
        The assistant reads the indexed STAC documentation, searches a live catalog and can
        measure a spectral index over the pixels of a scene. Whatever it finds is drawn on
        the map.
      </p>
    </div>
  )
}

export function ChatPane({
  turns,
  selectedId,
  hoveredId,
  quicklooks,
  streaming,
  onAsk,
  onStop,
  onHover,
  onSelect,
  onToggleQuicklook,
}: Props) {
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  // Follow the stream only while the reader is already at the bottom: scrolling up to
  // re-read an earlier answer must not be undone by the next token.
  const lastText = turns.at(-1)?.text
  useEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [turns.length, lastText])

  return (
    <section className="bg-panel flex h-full min-h-0 flex-col">
      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
        }}
        className="scrollbar-thin min-h-0 flex-1 overflow-y-auto"
      >
        {turns.length === 0 ? (
          <Empty />
        ) : (
          turns.map((turn) => (
            <Turn
              key={turn.id}
              turn={turn}
              selectedId={selectedId}
              hoveredId={hoveredId}
              quicklooks={quicklooks}
              onHover={onHover}
              onSelect={onSelect}
              onToggleQuicklook={onToggleQuicklook}
            />
          ))
        )}
      </div>

      <Composer onAsk={onAsk} onStop={onStop} streaming={streaming} />
    </section>
  )
}
