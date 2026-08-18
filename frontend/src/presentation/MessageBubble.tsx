import type { ChatEntry } from '../application/useChat'

import { clockTime, isoTime } from './format'

import './MessageBubble.css'

/**
 * One turn of the conversation.
 *
 * The two voices are made of different material rather than differently tinted:
 * the assistant's words are printed on perforated tape in the machine's own
 * face, because they arrived over a wire a word at a time; the person's words
 * sit on a warm note in a human face, because a person wrote them. Purely
 * presentational — it receives an entry and renders it.
 */

interface MessageBubbleProps {
  readonly entry: ChatEntry
  /** Draw the printing carriage at the end of the text. */
  readonly showCaret: boolean
}

export function MessageBubble({ entry, showCaret }: MessageBubbleProps) {
  const isAssistant = entry.role === 'assistant'

  return (
    <article
      className={isAssistant ? 'turn turn--machine' : 'turn turn--human'}
      data-status={entry.status}
    >
      <header className="turn__meta">
        <span className="turn__who">{isAssistant ? 'Assistant' : 'You'}</span>
        <time className="turn__time" dateTime={isoTime(entry.at)}>
          {clockTime(entry.at)}
        </time>
      </header>

      <div className="turn__surface">
        <p className="turn__text">
          {entry.content}
          {showCaret ? <span className="turn__caret" aria-hidden="true" /> : null}
        </p>
      </div>

      {entry.status === 'interrupted' ? (
        <p className="turn__fault">
          The stream stopped before the reply finished. Send the message again.
        </p>
      ) : null}

      {/* Why it was refused is said once, beside the composer. Here it only needs
          to be clear that this message never left. */}
      {entry.status === 'rejected' ? <p className="turn__fault">Not sent</p> : null}
    </article>
  )
}
