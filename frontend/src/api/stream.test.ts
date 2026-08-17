import { describe, expect, it } from 'vitest'

import { parseFrames } from './stream'
import type { StreamEvent } from './types'

const frame = (event: unknown) => `data: ${JSON.stringify(event)}\n\n`

const token = (text: string): StreamEvent => ({ type: 'token', text })

describe('parseFrames', () => {
  it('reads whole frames and keeps nothing back', () => {
    const { events, rest } = parseFrames(frame(token('one ')) + frame(token('two')))

    expect(events).toEqual([token('one '), token('two')])
    expect(rest).toBe('')
  })

  it('holds a partial frame until the rest of it arrives', () => {
    // The common case: a read ends mid-JSON. Emitting there would throw on parse.
    const whole = frame(token('Sentinel-2 covers Rome'))
    const first = parseFrames(whole.slice(0, 20))

    expect(first.events).toEqual([])

    const second = parseFrames(first.rest + whole.slice(20))

    expect(second.events).toEqual([token('Sentinel-2 covers Rome')])
    expect(second.rest).toBe('')
  })

  it('accepts CRLF line endings', () => {
    const { events } = parseFrames(`data: ${JSON.stringify(token('a'))}\r\n\r\n`)

    expect(events).toEqual([token('a')])
  })

  it('ignores comment lines, which is what a keep-alive ping would be', () => {
    const { events } = parseFrames(`: ping\n\n${frame(token('a'))}`)

    expect(events).toEqual([token('a')])
  })

  it('keeps newlines inside an event intact', () => {
    // The backend escapes them into the JSON, so a frame stays one line either way.
    const event = token('First line.\n\n- a bullet\n')

    expect(parseFrames(frame(event)).events).toEqual([event])
  })

  it('reads a whole conversation turn in order', () => {
    const script: StreamEvent[] = [
      { type: 'start', conversation_id: 'c1' },
      { type: 'tool_start', id: 'tu_1', name: 'stac_search', input: { limit: 2 } },
      { type: 'tool_end', id: 'tu_1', name: 'stac_search', ok: true, ms: 812, detail: null },
      { type: 'features', collection: { type: 'FeatureCollection', features: [] } },
      token('Two scenes.'),
      { type: 'done', answer: 'Two scenes.', sources: ['a.md'], steps: 2 },
    ]

    const { events } = parseFrames(script.map(frame).join(''))

    expect(events).toEqual(script)
  })
})
