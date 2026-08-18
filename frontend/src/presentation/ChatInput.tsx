import { useEffect, useRef, useState } from 'react'

import './ChatInput.css'

/**
 * The composer.
 *
 * Holds the draft and nothing else — the draft is genuinely local, since no
 * other part of the application has any use for half-typed text. The message
 * leaves through `onSend` and is never touched again here.
 *
 * The action button always names what pressing it does: Send while idle, Stop
 * while a reply is arriving. It never changes meaning behind the same word.
 */

/** Roughly six lines, after which the field scrolls rather than growing. */
const MAX_HEIGHT_PX = 152

interface ChatInputProps {
  readonly isStreaming: boolean
  readonly onSend: (text: string) => void
  readonly onStop: () => void
}

export function ChatInput({ isStreaming, onSend, onStop }: ChatInputProps) {
  const [draft, setDraft] = useState('')
  const fieldRef = useRef<HTMLTextAreaElement>(null)

  // Grow to fit the draft, up to a limit. Re-measured from scratch each time so
  // deleting a line shrinks the field back.
  useEffect(() => {
    const field = fieldRef.current
    if (field === null) return
    field.style.height = 'auto'
    field.style.height = `${String(Math.min(field.scrollHeight, MAX_HEIGHT_PX))}px`
  }, [draft])

  const submit = () => {
    if (isStreaming || draft.trim() === '') return
    onSend(draft)
    setDraft('')
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      <div className="composer__row">
        <label className="composer__label" htmlFor="composer-field">
          Message
        </label>
        <textarea
          id="composer-field"
          ref={fieldRef}
          className="composer__field"
          rows={1}
          placeholder="Type a message"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // isComposing guards input methods where Enter commits a candidate
            // rather than ending the message.
            if (
              event.key === 'Enter' &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault()
              submit()
            }
          }}
        />
        {isStreaming ? (
          <button type="button" className="composer__action" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button
            type="submit"
            className="composer__action composer__action--send"
            disabled={draft.trim() === ''}
          >
            Send
          </button>
        )}
      </div>
      <p className="composer__hint">Enter to send · Shift + Enter for a new line</p>
    </form>
  )
}
