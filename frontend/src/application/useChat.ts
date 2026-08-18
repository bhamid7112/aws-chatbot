import { useCallback, useEffect, useRef, useState } from 'react'

import type { ChatGateway } from '../domain/chatGateway'
import { PromptRejectedError } from '../domain/errors'
import type { Message, Role } from '../domain/message'

/**
 * The chat use case, expressed as a hook.
 *
 * It owns the transcript and the state of the reply in flight, and it depends on
 * the {@link ChatGateway} **port** rather than on any implementation (DIP) — the
 * gateway is injected, so this hook can be driven by a fake with no network at
 * all. Nothing here knows about HTTP, SSE, or the DOM.
 *
 * React is the one framework import the layer allows, since a hook is the shape
 * a use case takes in a React application.
 */

export type EntryStatus = 'complete' | 'streaming' | 'interrupted' | 'rejected'

/** One row of the transcript: a message plus the display state it carries. */
export interface ChatEntry {
  readonly id: string
  readonly role: Role
  readonly content: string
  /** Epoch milliseconds, formatted for display by the presentation layer. */
  readonly at: number
  readonly status: EntryStatus
}

export type WireStatus = 'standby' | 'opening' | 'receiving' | 'closed' | 'failed'

/**
 * What the transport is doing, in terms the domain already has.
 *
 * `frames` counts reply chunks actually received — a domain quantity, not an
 * HTTP one — so reporting it on screen commits the UI to nothing about the
 * protocol underneath.
 */
export interface WireState {
  readonly status: WireStatus
  readonly frames: number
  readonly elapsedMs: number
}

export interface ChatController {
  readonly entries: readonly ChatEntry[]
  readonly wire: WireState
  readonly isStreaming: boolean
  /** A message the person needs to act on, shown beside the composer. */
  readonly notice: string | null
  send: (text: string) => void
  stop: () => void
}

const STANDBY: WireState = { status: 'standby', frames: 0, elapsedMs: 0 }

export function useChat(gateway: ChatGateway): ChatController {
  const [entries, setEntries] = useState<readonly ChatEntry[]>([])
  const [wire, setWire] = useState<WireState>(STANDBY)
  const [notice, setNotice] = useState<string | null>(null)
  const [isStreaming, setIsStreaming] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const nextIdRef = useRef(0)

  // The transcript as of the last commit, so `send` can read it without being
  // rebuilt on every chunk that arrives.
  const entriesRef = useRef(entries)
  useEffect(() => {
    entriesRef.current = entries
  }, [entries])

  // A reply still arriving when the screen goes away has nobody to arrive for.
  useEffect(() => () => abortRef.current?.abort(), [])

  const patch = useCallback(
    (id: string, change: (entry: ChatEntry) => ChatEntry) => {
      setEntries((prev) =>
        prev.map((entry) => (entry.id === id ? change(entry) : entry)),
      )
    },
    [],
  )

  const send = useCallback(
    (text: string) => {
      const prompt = text.trim()
      // One reply at a time: a second request would race the first for the same
      // transcript row.
      if (prompt === '' || abortRef.current !== null) return

      const askedAt = Date.now()
      const question: ChatEntry = {
        id: `e${String((nextIdRef.current += 1))}`,
        role: 'user',
        content: prompt,
        at: askedAt,
        status: 'complete',
      }
      const reply: ChatEntry = {
        id: `e${String((nextIdRef.current += 1))}`,
        role: 'assistant',
        content: '',
        at: askedAt,
        status: 'streaming',
      }

      const history = historyOf(entriesRef.current)
      const controller = new AbortController()
      abortRef.current = controller

      setNotice(null)
      setEntries((prev) => [...prev, question, reply])
      setIsStreaming(true)
      setWire({ status: 'opening', frames: 0, elapsedMs: 0 })

      void (async () => {
        let frames = 0
        try {
          for await (const chunk of gateway.send(prompt, history, controller.signal)) {
            frames += 1
            patch(reply.id, (entry) => ({
              ...entry,
              content: entry.content + chunk.text,
            }))
            setWire({
              status: 'receiving',
              frames,
              elapsedMs: Date.now() - askedAt,
            })
          }
          settle(frames, askedAt)
        } catch (error) {
          // Stopping on purpose is not a failure: whatever arrived is the reply.
          if (controller.signal.aborted) {
            settle(frames, askedAt)
            return
          }

          if (error instanceof PromptRejectedError) {
            // Nothing was generated, so the empty reply row goes. The question
            // stays, because the person wrote it and losing their text would be
            // worse — but it is marked as never delivered rather than left
            // sitting there looking sent. `historyOf` skips it for the same
            // reason: the assistant never saw it.
            setEntries((prev) =>
              prev
                .filter((entry) => entry.id !== reply.id)
                .map((entry) =>
                  entry.id === question.id ? { ...entry, status: 'rejected' } : entry,
                ),
            )
            setNotice(error.message)
            setWire(STANDBY)
            return
          }

          patch(reply.id, (entry) => ({ ...entry, status: 'interrupted' }))
          setWire({ status: 'failed', frames, elapsedMs: Date.now() - askedAt })
        } finally {
          abortRef.current = null
          setIsStreaming(false)
        }
      })()

      function settle(frames: number, startedAt: number): void {
        patch(reply.id, (entry) => ({ ...entry, status: 'complete' }))
        setWire({ status: 'closed', frames, elapsedMs: Date.now() - startedAt })
      }
    },
    [gateway, patch],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return { entries, wire, isStreaming, notice, send, stop }
}

/**
 * The context sent with the next message.
 *
 * Interrupted replies are left out on purpose: a half-finished sentence is not
 * something the assistant said, and feeding it back as context would make the
 * conversation a record of a failure rather than of an exchange.
 */
function historyOf(entries: readonly ChatEntry[]): readonly Message[] {
  return entries
    .filter((entry) => entry.status === 'complete' && entry.content !== '')
    .map(({ role, content }) => ({ role, content }))
}
