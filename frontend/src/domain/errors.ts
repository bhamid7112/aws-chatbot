/**
 * The failures a chat can suffer, named by what they mean rather than by how
 * they were detected.
 *
 * There are exactly three because there are exactly three things the UI can
 * usefully do about them: ask the person to reword the message, tell them the
 * reply was cut short, or tell them the assistant could not be reached. A
 * fourth error class with no distinct outcome would be noise.
 */

/** Base class, so a caller can catch every chat failure with one clause. */
export class ChatError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
    this.name = new.target.name
  }
}

/**
 * The backend refused the message before generating anything.
 *
 * The `message` is the backend's own explanation and is written for a person to
 * read, so the UI may show it as-is.
 */
export class PromptRejectedError extends ChatError {}

/**
 * Generation began but did not finish — an in-band error frame, or a connection
 * that closed before the terminal sentinel arrived.
 *
 * Whatever text arrived before the break is real and is kept on screen.
 */
export class ReplyInterruptedError extends ChatError {}

/** The assistant could not be reached, or answered in a shape we cannot read. */
export class ChatUnavailableError extends ChatError {}
