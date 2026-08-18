import type { ChatGateway } from '../domain/chatGateway'
import {
  ChatUnavailableError,
  PromptRejectedError,
  ReplyInterruptedError,
} from '../domain/errors'
import type { Message, ReplyChunk } from '../domain/message'

/**
 * The only file in the frontend that knows the reply arrives over HTTP.
 *
 * `EventSource` cannot be used: it issues a GET and has no way to carry a
 * request body, and this request carries the message and its history. So the
 * stream is read by hand — `fetch`, `body.getReader()`, `TextDecoder` — and the
 * frames are reassembled here.
 *
 * The frame vocabulary mirrors `backend/app/interfaces/sse.py`:
 *
 *   data: {"delta": "Hi "}   append verbatim
 *   data: {"error": "..."}   generation failed mid-stream
 *   data: [DONE]             terminal sentinel, always last
 */

/** Same relative path in dev (via the Vite proxy) and in production (via Caddy). */
export const DEFAULT_CHAT_ENDPOINT = '/api/chat'

const DONE_SENTINEL = '[DONE]'
const HTTP_UNPROCESSABLE = 422

/** A frame ends at a blank line. Tolerates CRLF, though the backend sends LF. */
const FRAME_SEPARATOR = /\r?\n\r?\n/
const LINE_SEPARATOR = /\r?\n/

interface SseChatGatewayOptions {
  readonly endpoint?: string
}

export class SseChatGateway implements ChatGateway {
  /** Public so the composition root can label the wire readout with the truth. */
  readonly endpoint: string

  constructor(options: SseChatGatewayOptions = {}) {
    this.endpoint = options.endpoint ?? DEFAULT_CHAT_ENDPOINT
  }

  async *send(
    message: string,
    history: readonly Message[],
    signal?: AbortSignal,
  ): AsyncIterable<ReplyChunk> {
    const response = await this.post(message, history, signal)

    if (response.body === null) {
      throw new ChatUnavailableError('The assistant replied without a body.')
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      for (;;) {
        const { done, value } = await reader.read()

        if (done) {
          // The backend always closes with [DONE], which returns from inside the
          // loop below. Reaching here means the connection dropped instead.
          throw new ReplyInterruptedError(
            'The connection closed before the reply finished.',
          )
        }

        buffer += decoder.decode(value, { stream: true })

        // Everything up to the last separator is complete; the remainder is a
        // frame still arriving and stays in the buffer.
        const frames = buffer.split(FRAME_SEPARATOR)
        buffer = frames.pop() ?? ''

        for (const frame of frames) {
          const payload = dataOf(frame)
          if (payload === null) continue
          if (payload === DONE_SENTINEL) return

          const chunk = toChunk(payload)
          if (chunk !== null) yield chunk
        }
      }
    } finally {
      // Runs on early return, on a thrown error, and when the consumer abandons
      // the loop — so the socket is never left open.
      await reader.cancel().catch(() => undefined)
    }
  }

  private async post(
    message: string,
    history: readonly Message[],
    signal?: AbortSignal,
  ): Promise<Response> {
    let response: Response
    try {
      response = await fetch(this.endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        // Rebuilt rather than passed through, so nothing the UI happens to keep
        // on an entry can leak into the request body.
        body: JSON.stringify({
          message,
          history: history.map(({ role, content }) => ({ role, content })),
        }),
        cache: 'no-store',
        // `null`, not `undefined`: RequestInit declares the absence of a signal
        // as null, and exactOptionalPropertyTypes holds it to that.
        signal: signal ?? null,
      })
    } catch (cause) {
      // An aborted fetch is the caller's own doing; it is not a failure and is
      // re-thrown untouched so the caller can recognise its own signal.
      if (signal?.aborted === true) throw cause
      throw new ChatUnavailableError('The assistant could not be reached.', {
        cause,
      })
    }

    if (response.ok) return response
    throw await toRejection(response)
  }
}

/**
 * Extract a frame's `data` payload, per the SSE grammar: `data` lines join with
 * newlines, one leading space after the colon is dropped, and comment lines and
 * other fields (`event`, `id`, `retry`) are ignored.
 */
function dataOf(frame: string): string | null {
  const lines: string[] = []

  for (const line of frame.split(LINE_SEPARATOR)) {
    if (line === '' || line.startsWith(':')) continue

    const colon = line.indexOf(':')
    if ((colon === -1 ? line : line.slice(0, colon)) !== 'data') continue

    const value = colon === -1 ? '' : line.slice(colon + 1)
    lines.push(value.startsWith(' ') ? value.slice(1) : value)
  }

  return lines.length === 0 ? null : lines.join('\n')
}

/** Decode one payload into a chunk, or `null` if it carries no text. */
function toChunk(payload: string): ReplyChunk | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(payload)
  } catch (cause) {
    throw new ChatUnavailableError('The assistant sent an unreadable frame.', {
      cause,
    })
  }

  if (typeof parsed !== 'object' || parsed === null) {
    throw new ChatUnavailableError('The assistant sent an unreadable frame.')
  }

  const frame = parsed as { delta?: unknown; error?: unknown }

  if (typeof frame.error === 'string') {
    throw new ReplyInterruptedError(frame.error)
  }

  if (typeof frame.delta !== 'string' || frame.delta === '') return null
  return { text: frame.delta }
}

/** Turn a non-2xx response into the error that describes it. */
async function toRejection(response: Response): Promise<Error> {
  if (response.status !== HTTP_UNPROCESSABLE) {
    return new ChatUnavailableError(
      `The assistant answered with status ${String(response.status)}.`,
    )
  }

  // 422 arrives two ways: our own `detail` string, written for a person to read,
  // or FastAPI's list of field errors when the body itself was malformed. Only
  // the first is worth showing.
  const detail: unknown = await response
    .json()
    .then((body: { detail?: unknown }) => body.detail)
    .catch(() => undefined)

  return new PromptRejectedError(
    typeof detail === 'string' ? detail : 'The assistant rejected that message.',
  )
}
