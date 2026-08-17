import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import type { StacCollection } from '../api/types'
import type { Turn as TurnData } from '../hooks/useConversation'
import { ItemCards } from './ItemCards'
import { ToolTrace } from './ToolTrace'

interface Props {
  turn: TurnData
  features: StacCollection | null
  isLast: boolean
  selectedId: string | null
  hoveredId: string | null
  quicklooks: string[]
  onHover: (id: string | null) => void
  onSelect: (id: string | null) => void
  onToggleQuicklook: (id: string) => void
}

/** A documentation source is a filename; a catalog one is a URL. Show each as itself. */
function Source({ value }: { value: string }) {
  const isUrl = value.startsWith('http')
  const href = isUrl ? value.split(' ')[0] : null

  const body = (
    <span className="border-line bg-raised text-ink-dim inline-block rounded border px-1.5 py-0.5 font-mono text-[10.5px]">
      {value}
    </span>
  )

  return href ? (
    <a href={href} target="_blank" rel="noreferrer" className="hover:opacity-80">
      {body}
    </a>
  ) : (
    body
  )
}

export function Turn({
  turn,
  features,
  isLast,
  selectedId,
  hoveredId,
  quicklooks,
  onHover,
  onSelect,
  onToggleQuicklook,
}: Props) {
  // The streamed text carries every agent round, including the "let me check" the model
  // writes next to a tool call; `answer` is only the last one. Rendering the stream is
  // both more informative and what the user already watched arrive - `answer` is the
  // fallback for the case where no token ever showed up.
  const body = turn.text || turn.answer || ''

  return (
    <article className="border-line-soft border-b px-4 py-4 last:border-b-0">
      <p className="text-ink mb-3 text-[14.5px] leading-snug font-medium">{turn.question}</p>

      <ToolTrace tools={turn.tools} />

      {body && (
        <div className="prose-answer text-ink-dim text-[14.5px] leading-[1.65]">
          <Markdown remarkPlugins={[remarkGfm]}>{body}</Markdown>
        </div>
      )}

      {turn.status === 'streaming' && !body && turn.tools.length === 0 && (
        <p className="text-ink-faint text-[13px] italic">Thinking…</p>
      )}

      {/* Quiet, and not the red box: the answer is unfinished because it was asked to
          be, and what arrived before that is still on screen and still useful. */}
      {turn.status === 'stopped' && (
        <p className="text-ink-faint mt-2 text-[12.5px] italic">Stopped.</p>
      )}

      {turn.status === 'error' && (
        <p className="border-bad/40 bg-bad/10 text-bad mt-2 rounded border px-2.5 py-1.5 text-[12.5px]">
          {turn.error}
        </p>
      )}

      {/* Cards belong to the turn that produced the footprints, which is the last one to
          have sent a features event - so only the newest turn shows them. */}
      {isLast && features && (
        <ItemCards
          features={features.features}
          selectedId={selectedId}
          hoveredId={hoveredId}
          quicklooks={quicklooks}
          onHover={onHover}
          onSelect={onSelect}
          onToggleQuicklook={onToggleQuicklook}
        />
      )}

      {turn.sources.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-ink-faint text-[10px] tracking-[0.08em] uppercase">Sources</span>
          {turn.sources.map((source) => (
            <Source key={source} value={source} />
          ))}
        </div>
      )}
    </article>
  )
}
