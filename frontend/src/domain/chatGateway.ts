import type { Message, ReplyChunk } from './message'

/**
 * The port through which a reply is obtained.
 *
 * One method, because there is one thing to do (ISP) — the counterpart of the
 * backend's `ReplyGenerator`. Nothing here mentions HTTP, JSON or Server-Sent
 * Events: `infrastructure/sseChatGateway` happens to speak all three, and a
 * WebSocket or in-memory implementation would satisfy this interface equally.
 *
 * `AbortSignal` appears deliberately. It is a platform cancellation primitive
 * rather than a transport detail — the same type governs timers and streams —
 * and cancelling a reply in progress is a use-case concern, not an HTTP one.
 *
 * ### Contract every implementation must uphold
 *
 * Callers rely on all of this without re-checking it (LSP):
 *
 * 1. Yields zero or more chunks, each appended verbatim to the reply.
 * 2. Completes normally only once the reply is whole. Ending early is a failure
 *    and must be reported as one.
 * 3. Throws only {@link PromptRejectedError}, {@link ReplyInterruptedError},
 *    {@link ChatUnavailableError}, or the abort reason when `signal` fires.
 * 4. Never mutates `history`.
 * 5. Releases the underlying resource when the consumer stops iterating early.
 */
export interface ChatGateway {
  send(
    message: string,
    history: readonly Message[],
    signal?: AbortSignal,
  ): AsyncIterable<ReplyChunk>
}
