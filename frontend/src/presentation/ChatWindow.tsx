import { useEffect, useRef } from 'react'

import type { ChatEntry, WireState } from '../application/useChat'

import { ChatInput } from './ChatInput'
import { MessageBubble } from './MessageBubble'
import { TypingIndicator } from './TypingIndicator'
import { WireReadout } from './WireReadout'

import './ChatWindow.css'

/**
 * The screen.
 *
 * Props in, callbacks out: it is handed a transcript and reports what the person
 * did. It cannot reach the network, does not know the transport exists, and
 * would render identically driven by an array of literals in a test.
 *
 * The header states the plain truth about this deployment — the address *is* the
 * product's name, because it has no other one — and the readout beside it shows
 * the wire doing its work.
 */

/** How close to the bottom still counts as following the reply. */
const PIN_THRESHOLD_PX = 48

interface ChatWindowProps {
  /** `https:` or `http:`, as the browser reports it. */
  readonly scheme: string
  /** Host and port — an elastic IP in production, `localhost:5173` in dev. */
  readonly host: string
  readonly endpoint: string
  readonly entries: readonly ChatEntry[]
  readonly wire: WireState
  readonly isStreaming: boolean
  readonly notice: string | null
  readonly onSend: (text: string) => void
  readonly onStop: () => void
}

export function ChatWindow({
  scheme,
  host,
  endpoint,
  entries,
  wire,
  isStreaming,
  notice,
  onSend,
  onStop,
}: ChatWindowProps) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  // Following by default, but a deliberate scroll upwards is respected: text
  // arriving is not a reason to drag someone away from what they were reading.
  const pinnedRef = useRef(true)

  useEffect(() => {
    const scroller = scrollerRef.current
    if (scroller === null || !pinnedRef.current) return
    scroller.scrollTop = scroller.scrollHeight
  }, [entries, wire.status])

  return (
    <div className="shell">
      <header className="head">
        <div className="head__identity">
          <span className="head__eyebrow">Streaming assistant</span>
          <p className="head__address">
            <span className="head__scheme">{scheme}//</span>
            {host}
          </p>
        </div>
        <WireReadout endpoint={endpoint} wire={wire} />
      </header>

      <div
        ref={scrollerRef}
        className="transcript"
        role="log"
        aria-label="Conversation"
        aria-live="polite"
        aria-busy={isStreaming}
        onScroll={(event) => {
          const { scrollHeight, scrollTop, clientHeight } = event.currentTarget
          pinnedRef.current = scrollHeight - scrollTop - clientHeight < PIN_THRESHOLD_PX
        }}
      >
        {/* Collapses to nothing once the transcript fills, so the conversation
            stacks up from the composer instead of hanging from the header. */}
        <div className="transcript__lift" aria-hidden="true" />

        {entries.length === 0 ? (
          <div className="transcript__empty">
            <span className="transcript__empty-label">No messages yet</span>
            <p className="transcript__empty-line">
              Send anything. The reply prints back a word at a time.
            </p>
          </div>
        ) : (
          entries.map((entry) =>
            isAwaitingFirstWord(entry) ? (
              <TypingIndicator key={entry.id} />
            ) : (
              <MessageBubble
                key={entry.id}
                entry={entry}
                showCaret={entry.status === 'streaming'}
              />
            ),
          )
        )}
      </div>

      <footer className="foot">
        {notice === null ? null : (
          <p className="foot__notice" role="alert">
            {notice}
          </p>
        )}
        <ChatInput isStreaming={isStreaming} onSend={onSend} onStop={onStop} />
      </footer>
    </div>
  )
}

/**
 * A reply row with nothing in it yet stands in for the wait itself, so the
 * indicator replaces it rather than sitting beside an empty box.
 */
function isAwaitingFirstWord(entry: ChatEntry): boolean {
  return entry.role === 'assistant' && entry.status === 'streaming' && entry.content === ''
}
