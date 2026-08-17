import { useState } from 'react'

import type { ToolCall } from '../hooks/useConversation'

const LABEL: Record<string, string> = {
  rag_lookup: 'rag_lookup',
  stac_search: 'stac_search',
  compute_index: 'compute_index',
}

function bbox(value: unknown): string | null {
  if (!Array.isArray(value) || value.length !== 4) return null
  return value.map((n) => Number(n).toFixed(2)).join(', ')
}

/**
 * The arguments as a human would say them. The raw JSON is one click away, so this can
 * afford to drop what does not help - it is a summary, not a record.
 */
function summarise(name: string, input: Record<string, unknown>): string {
  const parts: string[] = []

  if (name === 'rag_lookup' && typeof input.query === 'string') {
    return `"${input.query}"`
  }

  if (name === 'compute_index') {
    if (typeof input.index === 'string') parts.push(input.index.toUpperCase())
    if (typeof input.item_id === 'string') parts.push(input.item_id)
  }

  if (Array.isArray(input.collections)) parts.push(input.collections.join(', '))
  if (typeof input.datetime === 'string') parts.push(input.datetime.replace('/', ' → '))
  if (typeof input.max_cloud_cover === 'number') parts.push(`≤ ${input.max_cloud_cover}% cloud`)

  const box = bbox(input.bbox)
  if (box) parts.push(box)

  return parts.join(' · ')
}

function Dot({ status }: { status: ToolCall['status'] }) {
  if (status === 'running') {
    return <span className="animate-dot text-accent" aria-label="running">●</span>
  }
  if (status === 'ok') {
    return <span className="text-ok" aria-label="succeeded">✓</span>
  }
  return <span className="text-bad" aria-label="failed">✗</span>
}

function Row({ tool }: { tool: ToolCall }) {
  const [open, setOpen] = useState(false)

  return (
    <li className="border-line-soft border-t first:border-t-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="hover:bg-raised/60 flex w-full items-baseline gap-2 px-2.5 py-1.5 text-left transition-colors"
      >
        <span className="w-3 shrink-0 text-[11px] leading-5">
          <Dot status={tool.status} />
        </span>
        <span className="font-mono text-[11.5px] shrink-0 text-ink">{LABEL[tool.name] ?? tool.name}</span>
        <span className="text-ink-dim truncate font-mono text-[11px]">
          {summarise(tool.name, tool.input)}
        </span>
        <span className="tnum text-ink-faint ml-auto shrink-0 pl-2 font-mono text-[10.5px]">
          {tool.ms !== undefined ? `${(tool.ms / 1000).toFixed(1)}s` : ''}
        </span>
      </button>

      {tool.status === 'error' && tool.detail && (
        <p className="text-bad px-2.5 pb-1.5 pl-[26px] font-mono text-[11px] leading-relaxed">
          {tool.detail}
        </p>
      )}

      {open && (
        <pre className="text-ink-dim scrollbar-thin mx-2.5 mb-2 overflow-x-auto rounded bg-ground px-2.5 py-2 font-mono text-[10.5px] leading-relaxed">
          {JSON.stringify(tool.input, null, 2)}
        </pre>
      )}
    </li>
  )
}

/**
 * What the agent did, while it does it. Worth its own component because a failed call is
 * not an error the caller ever sees otherwise: the graph hands it back to the model as an
 * errored tool_result and the answer explains around it.
 */
export function ToolTrace({ tools }: { tools: ToolCall[] }) {
  if (tools.length === 0) return null

  return (
    <ul className="border-line bg-panel/70 my-2 overflow-hidden rounded-md border">
      {tools.map((tool) => (
        <Row key={tool.id} tool={tool} />
      ))}
    </ul>
  )
}
