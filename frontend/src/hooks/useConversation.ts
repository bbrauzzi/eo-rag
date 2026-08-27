import { useCallback, useEffect, useReducer, useRef } from 'react'

import { askStream } from '../api/stream'
import type { StacCollection, StreamEvent } from '../api/types'

export interface ToolCall {
  id: string
  name: string
  input: Record<string, unknown>
  status: 'running' | 'ok' | 'error'
  ms?: number
  detail?: string | null
}

export interface Turn {
  id: number
  question: string
  /** Every token of the turn, from every agent round - see `answer` below. */
  text: string
  tools: ToolCall[]
  /** The last agent turn alone, as /ask would have returned it. Used only as a fallback. */
  answer?: string
  sources: string[]
  steps?: number
  status: 'streaming' | 'done' | 'stopped' | 'error'
  error?: string
  /**
   * The footprints this turn produced, or nothing if it ran no tool that returned any.
   * Held per turn and not read off the conversation's `features` below, which is kept
   * across turns for the map: the cards under an answer have to describe *that* answer,
   * or the previous search's scenes end up sitting under "what is a STAC feature?".
   */
  features?: StacCollection
}

export interface ConversationState {
  conversationId: string | null
  turns: Turn[]
  /**
   * The last non-empty collection, kept across turns, and what the map draws. A turn
   * that runs no tool sends no features event, so nothing here changes - which is right:
   * "which of those has the least cloud?" is about the footprints already on screen.
   * The chat's cards read `Turn.features` instead, which does not carry over.
   */
  features: StacCollection | null
  selectedId: string | null
  hoveredId: string | null
  /**
   * The scenes whose quicklook is laid over the map. A set rather than one at a time:
   * overlapping tiles are the normal case here, and comparing two of them is the point.
   */
  quicklooks: string[]
}

type Action =
  | { kind: 'ask'; question: string }
  | { kind: 'event'; event: StreamEvent }
  | { kind: 'tokens'; text: string }
  | { kind: 'fail'; message: string }
  | { kind: 'stop' }
  | { kind: 'select'; id: string | null }
  | { kind: 'hover'; id: string | null }
  | { kind: 'quicklook'; id: string }
  | { kind: 'reset' }

const EMPTY: ConversationState = {
  conversationId: null,
  turns: [],
  features: null,
  selectedId: null,
  hoveredId: null,
  quicklooks: [],
}

/** Apply `patch` to the turn in flight, leaving every earlier turn's identity alone. */
function patchLast(state: ConversationState, patch: Partial<Turn>): ConversationState {
  if (state.turns.length === 0) return state
  const turns = state.turns.slice()
  turns[turns.length - 1] = { ...turns[turns.length - 1], ...patch }
  return { ...state, turns }
}

function reduce(state: ConversationState, action: Action): ConversationState {
  switch (action.kind) {
    case 'ask':
      return {
        ...state,
        turns: [
          ...state.turns,
          {
            id: state.turns.length,
            question: action.question,
            text: '',
            tools: [],
            sources: [],
            status: 'streaming',
          },
        ],
      }

    case 'tokens': {
      const last = state.turns.at(-1)
      return last ? patchLast(state, { text: last.text + action.text }) : state
    }

    case 'event': {
      const event = action.event
      const last = state.turns.at(-1)

      switch (event.type) {
        case 'start':
          return { ...state, conversationId: event.conversation_id }

        case 'tool_start':
          return patchLast(state, {
            tools: [
              ...(last?.tools ?? []),
              { id: event.id, name: event.name, input: event.input, status: 'running' },
            ],
          })

        case 'tool_end':
          return patchLast(state, {
            tools: (last?.tools ?? []).map((tool) =>
              tool.id === event.id
                ? {
                    ...tool,
                    status: event.ok ? 'ok' : 'error',
                    ms: event.ms,
                    detail: event.detail,
                  }
                : tool,
            ),
          })

        case 'features':
          // The only action that replaces this reference, which is what lets the map
          // pane skip re-rendering for every token. The same collection also lands on
          // the turn that produced it, where the cards read it from.
          return {
            ...patchLast(state, { features: event.collection }),
            features: event.collection,
          }

        case 'done':
          return patchLast(state, {
            answer: event.answer,
            sources: event.sources,
            steps: event.steps,
            status: 'done',
          })

        case 'error':
          return patchLast(state, { status: 'error', error: event.message })

        default:
          return state
      }
    }

    case 'fail':
      return patchLast(state, { status: 'error', error: action.message })

    // Its own status rather than an error: the answer is unfinished because it was
    // asked to be, and whatever had already arrived is still worth reading.
    case 'stop':
      return patchLast(state, { status: 'stopped' })

    case 'select':
      return { ...state, selectedId: action.id }

    case 'hover':
      return { ...state, hoveredId: action.id }

    case 'quicklook':
      return {
        ...state,
        quicklooks: state.quicklooks.includes(action.id)
          ? state.quicklooks.filter((id) => id !== action.id)
          : [...state.quicklooks, action.id],
      }

    case 'reset':
      return EMPTY
  }
}

export function useConversation() {
  const [state, dispatch] = useReducer(reduce, EMPTY)
  const abort = useRef<AbortController | null>(null)
  // Tokens land here first and are flushed on a frame. A fast stream is several hundred
  // deltas a second; dispatching each one re-renders the whole chat pane that often.
  const pending = useRef('')
  const frame = useRef<number | null>(null)

  const flush = useCallback(() => {
    frame.current = null
    if (!pending.current) return
    dispatch({ kind: 'tokens', text: pending.current })
    pending.current = ''
  }, [])

  /** Throw the buffer away instead of landing it - for text that now belongs to nothing. */
  const discard = useCallback(() => {
    if (frame.current !== null) cancelAnimationFrame(frame.current)
    frame.current = null
    pending.current = ''
  }, [])

  useEffect(
    () => () => {
      abort.current?.abort()
      if (frame.current !== null) cancelAnimationFrame(frame.current)
    },
    [],
  )

  const ask = useCallback(
    async (question: string) => {
      const trimmed = question.trim()
      if (!trimmed || abort.current) return

      const controller = new AbortController()
      abort.current = controller
      dispatch({ kind: 'ask', question: trimmed })

      try {
        for await (const event of askStream(
          { question: trimmed, conversation_id: state.conversationId },
          controller.signal,
        )) {
          if (event.type === 'token') {
            pending.current += event.text
            frame.current ??= requestAnimationFrame(flush)
            continue
          }
          // Anything else describes what the tokens so far meant, so the buffer has to
          // land before it: a tool chip must not appear above the text preceding it.
          flush()
          dispatch({ kind: 'event', event })
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          dispatch({ kind: 'fail', message: (error as Error).message })
        }
      } finally {
        // Only if this turn is still the current one. A reset, a stop or a newer
        // question may already have taken over, and then both of these do damage:
        // flushing would append this turn's leftover tokens to whatever is on screen
        // now, and nulling the controller would disarm the Stop button of a turn we
        // no longer own - which also lets a second ask start alongside it.
        if (abort.current === controller) {
          flush()
          abort.current = null
        }
      }
    },
    [state.conversationId, flush],
  )

  const stop = useCallback(() => {
    abort.current?.abort()
    abort.current = null
    // Flushed, not discarded: text that already arrived belongs to the turn being
    // stopped, and keeping the partial answer is the point of stopping rather than
    // starting over.
    flush()
    dispatch({ kind: 'stop' })
  }, [flush])

  const select = useCallback((id: string | null) => dispatch({ kind: 'select', id }), [])
  const hover = useCallback((id: string | null) => dispatch({ kind: 'hover', id }), [])
  const toggleQuicklook = useCallback((id: string) => dispatch({ kind: 'quicklook', id }), [])
  const reset = useCallback(() => {
    abort.current?.abort()
    abort.current = null
    // Discarded rather than flushed: the turn these tokens belong to is being thrown
    // away, and left in the buffer they would surface at the top of the next answer.
    discard()
    dispatch({ kind: 'reset' })
  }, [discard])

  const streaming = state.turns.at(-1)?.status === 'streaming'

  return { state, ask, stop, select, hover, toggleQuicklook, reset, streaming }
}
