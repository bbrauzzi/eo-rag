import { useCallback, useEffect, useRef, useState } from 'react'

import type { Basemap } from './api/types'
import { ChatPane } from './components/ChatPane'
import { MapPane } from './components/MapPane'
import { useConversation } from './hooks/useConversation'

const MIN_CHAT = 360
const MAX_CHAT = 720
const STORED_WIDTH = 'eo-rag:chat-width'

function Header({
  conversationId,
  basemap,
  onBasemap,
  onReset,
}: {
  conversationId: string | null
  basemap: Basemap
  onBasemap: (value: Basemap) => void
  onReset: () => void
}) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    if (!conversationId) return
    void navigator.clipboard.writeText(conversationId)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <header className="border-line bg-panel flex h-11 shrink-0 items-center gap-3 border-b px-3">
      <span className="text-ink text-[13px] font-semibold tracking-tight">
        EO<span className="text-accent">Rag</span>
      </span>

      {conversationId && (
        <button
          type="button"
          onClick={copy}
          title="Copy the conversation id"
          className="border-line text-ink-faint hover:text-ink-dim hidden rounded border px-1.5 py-0.5 font-mono text-[10.5px] transition-colors sm:block"
        >
          {copied ? 'copied' : conversationId.slice(0, 8)}
        </button>
      )}

      <div className="ml-auto flex items-center gap-2">
        <div className="border-line flex overflow-hidden rounded border">
          {(['vector', 'imagery'] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => onBasemap(value)}
              className={[
                'px-2 py-1 text-[11px] transition-colors',
                basemap === value
                  ? 'bg-raised text-ink'
                  : 'text-ink-faint hover:text-ink-dim',
              ].join(' ')}
            >
              {value === 'vector' ? 'Map' : 'Imagery'}
            </button>
          ))}
        </div>

        {/* "New chat", not "New": next to the basemap toggle, one word reads as though
            it belonged to that group and could mean a new map. */}
        <button
          type="button"
          onClick={onReset}
          title="Start a new conversation"
          className="border-line text-ink-faint hover:text-ink-dim rounded border px-2 py-1 text-[11px] whitespace-nowrap transition-colors"
        >
          New chat
        </button>
      </div>
    </header>
  )
}

export function App() {
  const { state, ask, stop, select, hover, toggleQuicklook, reset, streaming } =
    useConversation()
  const [basemap, setBasemap] = useState<Basemap>('vector')
  const [chatWidth, setChatWidth] = useState(() =>
    Number(localStorage.getItem(STORED_WIDTH)) || 460,
  )
  const dragging = useRef(false)

  useEffect(() => {
    const move = (e: MouseEvent) => {
      if (!dragging.current) return
      setChatWidth(Math.min(MAX_CHAT, Math.max(MIN_CHAT, e.clientX)))
    }
    const up = () => {
      if (!dragging.current) return
      dragging.current = false
      document.body.style.cursor = ''
    }

    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
  }, [])

  useEffect(() => {
    localStorage.setItem(STORED_WIDTH, String(chatWidth))
  }, [chatWidth])

  const startDrag = useCallback(() => {
    dragging.current = true
    document.body.style.cursor = 'col-resize'
  }, [])

  return (
    <div className="flex h-full flex-col">
      <Header
        conversationId={state.conversationId}
        basemap={basemap}
        onBasemap={setBasemap}
        onReset={reset}
      />

      <main className="flex min-h-0 flex-1 flex-col md:flex-row">
        {/* The width goes through a custom property so one ChatPane serves both layouts:
            a second instance would carry its own composer draft and scroll position. */}
        <div
          style={{ '--chat-w': `${chatWidth}px` } as React.CSSProperties}
          className="order-2 min-h-0 flex-1 md:order-1 md:w-[var(--chat-w)] md:flex-none"
        >
          <ChatPane
            turns={state.turns}
            selectedId={state.selectedId}
            hoveredId={state.hoveredId}
            quicklooks={state.quicklooks}
            streaming={streaming}
            onAsk={ask}
            onStop={stop}
            onHover={hover}
            onSelect={select}
            onToggleQuicklook={toggleQuicklook}
          />
        </div>

        <div
          onMouseDown={startDrag}
          className="bg-line hover:bg-accent-dim order-1 hidden w-px cursor-col-resize transition-colors md:order-2 md:block"
        />

        <div className="order-1 h-[40vh] min-h-0 md:order-3 md:h-auto md:flex-1">
          <MapPane
            features={state.features}
            selectedId={state.selectedId}
            hoveredId={state.hoveredId}
            quicklooks={state.quicklooks}
            basemap={basemap}
            onHover={hover}
            onSelect={select}
            onToggleQuicklook={toggleQuicklook}
          />
        </div>
      </main>
    </div>
  )
}
