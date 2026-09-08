import type { ChatGateway } from '../domain/chatGateway'
import type { Message, ReplyChunk } from '../domain/message'

/**
 * Picks a transport once, then gets out of the way.
 *
 * This exists because **one bundle serves two deployments.** The serverless
 * target can generate a reply in the background and hand it over in pieces; the
 * EC2 target has no job store and no worker, so those routes are not merely
 * unused there, they are absent. Rather than build the bundle twice or hard-code
 * a guess, the backend is asked what it can do, and the answer decides.
 *
 * It is itself a {@link ChatGateway}, which is what keeps the decision invisible
 * above this layer: `useChat` is handed one gateway, as it always was, and never
 * learns there was a choice (LSP — a substitute for the port that also happens
 * to delegate to one).
 *
 * The choice is made on **first use**, not at construction, so that a page load
 * never waits on a network round trip to render. See `main.tsx`.
 */

export type TransportName = 'sse' | 'jobs'

export interface TransportOption {
  readonly name: TransportName
  /** How this transport describes itself in the wire readout. */
  readonly endpoint: string
  readonly gateway: ChatGateway
}

interface TransportSelectingChatGatewayOptions {
  /** What the backend says it can serve. Must never reject. */
  readonly advertised: Promise<readonly string[]>
  /** In preference order: the first one the backend advertises wins. */
  readonly options: readonly TransportOption[]
  /**
   * What to use when the backend advertises nothing we recognise, including
   * when the probe failed.
   *
   * Named separately from the preference order rather than derived from it,
   * because the two answers genuinely differ: the transport we would *rather*
   * use and the transport we can *always* use are not the same, and conflating
   * them would silently break whichever deployment lacks the preferred one.
   */
  readonly fallback: TransportName
}

export class TransportSelectingChatGateway implements ChatGateway {
  private readonly advertised: Promise<readonly string[]>
  private readonly options: readonly TransportOption[]
  private readonly fallback: TransportName
  private readonly forced: TransportName | undefined

  /** Memoised, so concurrent first sends cannot each resolve their own. */
  private selection: Promise<TransportOption> | null = null
  private selected: TransportOption | null = null

  constructor(
    options: TransportSelectingChatGatewayOptions & {
      /** Overrides the preference order, for verifying a transport by hand. */
      readonly forced?: TransportName | undefined
    },
  ) {
    if (options.options.length === 0) {
      throw new Error('At least one transport is required.')
    }
    this.advertised = options.advertised
    this.options = options.options
    this.fallback = options.fallback
    this.forced = options.forced
  }

  /**
   * The chosen transport's label, or the one we would fall back to.
   *
   * Read by the composition root for the wire readout, which is why it is a
   * plain value: before the choice is made it names the honest default, and
   * after it names the truth.
   */
  get endpoint(): string {
    return (this.selected ?? this.fallbackOption()).endpoint
  }

  /** Resolve the transport, or return the one already resolved. */
  async choose(): Promise<TransportOption> {
    this.selection ??= this.resolve()
    return this.selection
  }

  async *send(
    message: string,
    history: readonly Message[],
    signal?: AbortSignal,
  ): AsyncIterable<ReplyChunk> {
    const chosen = await this.choose()
    // `yield*` and not a hand-rolled loop: it forwards the consumer's early
    // return to the delegate, which is how the chosen gateway gets to release
    // its socket or cancel its job.
    yield* chosen.gateway.send(message, history, signal)
  }

  private async resolve(): Promise<TransportOption> {
    const chosen = this.pick(await this.advertised)
    this.selected = chosen
    return chosen
  }

  private pick(advertised: readonly string[]): TransportOption {
    if (this.forced !== undefined) {
      const forced = this.options.find((option) => option.name === this.forced)
      if (forced !== undefined && advertised.includes(forced.name)) return forced

      // Silence here would make a verification session lie about what it tested.
      console.warn(
        `Transport '${String(this.forced)}' was asked for but is not available; ` +
          `the backend advertises [${advertised.join(', ')}].`,
      )
    }

    return (
      this.options.find((option) => advertised.includes(option.name)) ??
      this.fallbackOption()
    )
  }

  private fallbackOption(): TransportOption {
    const option = this.options.find((candidate) => candidate.name === this.fallback)
    if (option === undefined) {
      throw new Error(`The fallback transport '${this.fallback}' was not supplied.`)
    }
    return option
  }
}
