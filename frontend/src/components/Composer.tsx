import { useEffect, useRef, useState } from 'react'

interface Props {
  onAsk: (question: string) => void
  onStop: () => void
  streaming: boolean
}

const SUGGESTIONS = [
  'What are STAC Items?',
  'Which Sentinel-2 scenes cover Rome in January 2024 with less than 20% cloud?',
  'What is the mean NDVI over the centre of Rome in that scene?',
]

export function Composer({ onAsk, onStop, streaming }: Props) {
  const [value, setValue] = useState('')
  const box = useRef<HTMLTextAreaElement>(null)

  // Grow with the question, up to a point: a bbox pasted in should not swallow the pane.
  useEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  const submit = () => {
    if (streaming || !value.trim()) return
    onAsk(value)
    setValue('')
  }

  return (
    <div className="border-line bg-panel border-t px-3 py-3">
      <div className="focus-within:border-accent-dim border-line bg-ground flex items-end gap-2 rounded-lg border px-3 py-2 transition-colors">
        <textarea
          ref={box}
          rows={1}
          value={value}
          disabled={streaming}
          placeholder="Ask about EO data, the STAC spec, or a place and a period…"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submit()
            }
          }}
          className="text-ink placeholder:text-ink-faint max-h-40 flex-1 resize-none bg-transparent text-[14px] leading-relaxed outline-none disabled:opacity-50"
        />

        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            className="border-line text-ink-dim hover:border-bad hover:text-bad shrink-0 rounded-md border px-2.5 py-1 text-[12px] transition-colors"
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={submit}
            disabled={!value.trim()}
            className="bg-accent text-ground shrink-0 rounded-md px-3 py-1 text-[12px] font-medium transition-opacity disabled:opacity-25"
          >
            Ask
          </button>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-1.5">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            disabled={streaming}
            onClick={() => setValue(s)}
            className="border-line text-ink-faint hover:border-ink-faint hover:text-ink-dim max-w-full truncate rounded-full border px-2.5 py-1 text-[11px] transition-colors disabled:opacity-40"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}
