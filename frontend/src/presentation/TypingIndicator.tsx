import './TypingIndicator.css'

/**
 * The gap between asking and the first word arriving.
 *
 * Shown only while that gap is real — the request is open and no chunk has come
 * back yet. Once text starts printing, the carriage in {@link MessageBubble}
 * takes over, so the two never appear at once and the screen never claims to be
 * waiting for something that is already happening.
 *
 * It is a blank length of tape with the carriage travelling across it, not three
 * bouncing dots: the same instrument, before it has printed anything.
 */
export function TypingIndicator() {
  return (
    <div className="opening" role="status">
      <span className="opening__label">Opening stream</span>
      <span className="opening__tape" aria-hidden="true">
        <span className="opening__carriage" />
      </span>
    </div>
  )
}
