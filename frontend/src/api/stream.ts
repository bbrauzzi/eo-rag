import type { AskRequest, StreamEvent } from './types'

/**
 * SSE frame parser.
 *
 * Kept apart from the transport so it can be tested without a server: everything that
 * can go wrong here is about chunk boundaries, and a network never reproduces those on
 * demand. Feed it decoded text, get whole events out; hold the returned buffer and pass
 * it back in on the next call.
 */
export function parseFrames(buffer: string): { events: StreamEvent[]; rest: string } {
  const events: StreamEvent[] = []
  const separator = /\r?\n\r?\n/
  let rest = buffer

  for (;;) {
    const match = separator.exec(rest)
    if (!match) break

    const frame = rest.slice(0, match.index)
    rest = rest.slice(match.index + match[0].length)

    const data = frame
      .split(/\r?\n/)
      // Anything else is a field the backend does not send, or a `:` comment line -
      // which is what a keep-alive ping would look like if one is ever added.
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')

    if (data) events.push(JSON.parse(data) as StreamEvent)
  }

  return { events, rest }
}

/**
 * Ask a question and yield the events as they arrive.
 *
 * fetch rather than EventSource: EventSource cannot POST, cannot set headers and cannot
 * be aborted, and the alternative - a POST that parks the question server-side for a GET
 * to pick up - would put per-stream state in a backend that deliberately keeps
 * dependencies out of its state. An AbortSignal gives the Stop button something real.
 */
export async function* askStream(
  body: AskRequest,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch('/ask/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })

  if (!response.ok || !response.body) {
    // Read the body before falling back to the status code. A 429 from the conversation
    // budget is the case that matters: its detail names the limit that was reached and
    // says to start a new conversation, which is the only part the user can act on -
    // "HTTP 429" alone tells them to try again, which is exactly what will not work.
    const detail = await response
      .json()
      .then((body: { detail?: string }) => body?.detail)
      .catch(() => undefined)

    throw new Error(detail ?? `The server refused the question (HTTP ${response.status}).`)
  }

  // TextDecoderStream, not a TextDecoder per chunk: a multi-byte character can be split
  // across two network reads, and the answers are arbitrary prose.
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break

      buffer += value
      const parsed = parseFrames(buffer)
      buffer = parsed.rest
      yield* parsed.events
    }
  } finally {
    await reader.cancel().catch(() => undefined)
  }
}
