import type { WireState } from '../application/useChat'

import './WireReadout.css'

/**
 * The wire readout — the one element this screen is meant to be remembered by.
 *
 * Streaming is the only thing this application actually does, so the design's
 * loudest element is the evidence of it: one tick lights for each reply chunk
 * that genuinely arrived, and the strip settles into a count and a duration when
 * the stream closes. Every number here is measured, never decorative.
 */

/** Enough slots for the canned reply with room to spare, at a fixed width so
 *  the header never reflows mid-stream. */
const SLOTS = 24

interface WireReadoutProps {
  /** What the gateway posts to, e.g. `POST /api/chat`. */
  readonly endpoint: string
  readonly wire: WireState
}

export function WireReadout({ endpoint, wire }: WireReadoutProps) {
  const lit = Math.min(wire.frames, SLOTS)

  return (
    <div className="wire" data-state={wire.status}>
      <span className="wire__label">Wire</span>
      <code className="wire__endpoint">{endpoint}</code>
      <div className="wire__ticks" aria-hidden="true">
        {Array.from({ length: SLOTS }, (_, index) => (
          <span
            key={index}
            className={index < lit ? 'wire__tick wire__tick--lit' : 'wire__tick'}
          />
        ))}
      </div>
      <span className="wire__status">{describe(wire)}</span>
    </div>
  )
}

function describe(wire: WireState): string {
  switch (wire.status) {
    case 'standby':
      return 'standing by'
    case 'opening':
      return 'opening'
    case 'receiving':
      return `${String(wire.frames)} frames`
    case 'closed':
      return `${String(wire.frames)} frames · ${String(wire.elapsedMs)} ms`
    case 'failed':
      return 'interrupted'
  }
}
