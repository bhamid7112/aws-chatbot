/**
 * The vocabulary of a conversation.
 *
 * The innermost layer: no React, no fetch, no DOM, nothing importable from
 * outside `domain/`. These types mirror the backend's domain entities, which is
 * why the two ends of the wire agree without either one importing the other.
 */

export type Role = 'user' | 'assistant'

/** One completed turn, as it travels to the backend as prior context. */
export interface Message {
  readonly role: Role
  readonly content: string
}

/**
 * A fragment of a reply.
 *
 * Fragments are concatenated **verbatim** — separating whitespace travels inside
 * `text`, so a consumer never inserts spacing of its own.
 */
export interface ReplyChunk {
  readonly text: string
}
