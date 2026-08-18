/**
 * Display formatting. Presentation-only: no other layer knows or cares how a
 * timestamp looks.
 */

const CLOCK = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

/** `14:07:23` — seconds included, because a reply is timed in seconds here. */
export function clockTime(at: number): string {
  return CLOCK.format(at)
}

/** The machine-readable twin of {@link clockTime}, for the `datetime` attribute. */
export function isoTime(at: number): string {
  return new Date(at).toISOString()
}
