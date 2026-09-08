import { ChatUnavailableError, PromptRejectedError } from '../domain/errors'

/**
 * What both gateways need from HTTP, and neither of them should own.
 *
 * There are two transports now — Server-Sent Events and job polling — and three
 * things they must agree on exactly: how a request body is hashed for the CDN,
 * how a refusal becomes a domain error, and which transports the backend says it
 * can serve. Duplicating any of those would let them drift, and the payload hash
 * in particular fails as a 403 with no useful diagnostic.
 *
 * An `infrastructure → infrastructure` import, which the dependency rule allows.
 * This layer needs no bare imports at all: `fetch`, `crypto` and `TextEncoder`
 * are platform globals.
 */

const HTTP_UNPROCESSABLE = 422

/** Same relative path in dev (via the Vite proxy) and in production. */
export const DEFAULT_HEALTH_ENDPOINT = '/api/health'

/**
 * SHA-256 of the empty string — the payload hash of a request with no body.
 *
 * A constant rather than a call to {@link sha256Hex} because it is needed on
 * requests that must be able to leave during page unload, where there is no time
 * to await a digest.
 */
export const EMPTY_SHA256 =
  'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

/**
 * Hex SHA-256 of the request body, or `null` if this browser cannot compute one.
 *
 * ── Why a request body needs a hash at all ──────────────────────────────────
 *
 * On the serverless target CloudFront reaches the API through Origin Access
 * Control, which signs each origin request with SigV4 — and a SigV4 signature
 * covers the body. CloudFront cannot hash a body it is streaming through, and
 * Lambda does not accept `UNSIGNED-PAYLOAD`, so the *viewer* supplies the digest
 * and CloudFront signs using it. Without this header a POST is rejected with 403,
 * which was confirmed by removing it against a real distribution.
 *
 * Note what this is **not**: the browser holds no AWS credentials and signs
 * nothing. It contributes one hash of its own request body.
 *
 * A **bodyless** request needs no hash for the signature to hold — a bodyless
 * `DELETE` through the real distribution returns 204 with the header and
 * without it. {@link EMPTY_SHA256} is sent anyway, so that every request leaves
 * here the same shape and nobody has to remember which ones are exempt.
 *
 * ── Why the same bundle still serves both deployment targets ────────────────
 *
 * On the EC2 target Caddy neither reads nor forwards this header, so sending it
 * is harmless there — which is what allows one bundle to be both baked into the
 * Caddy image and synced to S3, rather than built twice with different flags.
 *
 * `crypto.subtle` exists only in a secure context. HTTPS via CloudFront
 * qualifies, and so do `http://localhost` and `127.0.0.1`, so the compose stack
 * and the Vite dev server are unaffected. Reaching a local stack over a LAN
 * address is the one case that is neither, and there the header is returned as
 * `null` and simply omitted instead of throwing — correct, because the only
 * target that requires the header is served exclusively over HTTPS.
 */
export async function sha256Hex(body: string): Promise<string | null> {
  if (typeof crypto === 'undefined' || crypto.subtle === undefined) return null

  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body))

  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

/**
 * Turn a non-2xx response into the error that describes it.
 *
 * Deliberately says nothing about 404. It means one thing on a submit — the job
 * routes are not mounted, so this deployment cannot serve them — and quite
 * another on a poll, where the job itself is gone. A shared helper that guessed
 * would be wrong half the time, so each caller reads its own.
 */
export async function toRejection(response: Response): Promise<Error> {
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

/** Read a JSON body, or say that it could not be read. */
export async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch (cause) {
    throw new ChatUnavailableError('The assistant sent an unreadable reply.', {
      cause,
    })
  }
}

/**
 * Which transports the backend can serve, or an empty list if it will not say.
 *
 * Never rejects, and that is the point: this runs at page load and its answer
 * decides a preference, not a capability. A failed probe means "use the
 * transport that has always existed", which is a sound default rather than an
 * error to show somebody who has not asked for anything yet.
 *
 * An older deployment that predates the field answers the same way, since a
 * health response without `transports` yields an empty list.
 */
export async function probeTransports(
  endpoint: string = DEFAULT_HEALTH_ENDPOINT,
): Promise<readonly string[]> {
  let body: unknown
  try {
    const response = await fetch(endpoint, {
      method: 'GET',
      headers: { 'x-amz-content-sha256': EMPTY_SHA256 },
      cache: 'no-store',
    })
    if (!response.ok) return []
    body = await response.json()
  } catch {
    return []
  }

  if (typeof body !== 'object' || body === null) return []
  const advertised = (body as { transports?: unknown }).transports

  if (!Array.isArray(advertised)) return []
  return advertised.filter((name): name is string => typeof name === 'string')
}
