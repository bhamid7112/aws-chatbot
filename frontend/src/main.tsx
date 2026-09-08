import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import { probeTransports } from './infrastructure/chatHttp'
import { JobsChatGateway } from './infrastructure/jobsChatGateway'
import { SseChatGateway } from './infrastructure/sseChatGateway'
import {
  TransportSelectingChatGateway,
  type TransportName,
} from './infrastructure/transportSelectingChatGateway'

import './styles/tokens.css'
import './styles/base.css'

/**
 * The composition root — the counterpart of `backend/app/interfaces/dependencies.py`.
 *
 * The only module in the frontend that names a concrete adapter. Swapping the
 * transport, or standing the UI up against a fake, is an edit to this file and
 * to nothing else.
 *
 * ### Why the probe is not awaited before rendering
 *
 * It would be the obvious thing, and it would be a regression. A cold page load
 * would then show nothing at all for the whole of the API's initialisation —
 * seconds, on a container-image function — where today the app paints
 * immediately and only the first *chat* is slow.
 *
 * Firing it here and awaiting it inside the gateway's first `send()` gets both
 * halves: first paint is unchanged, and the API is warmed while the person is
 * still reading the page. The re-render below is only so the wire readout stops
 * naming a transport we did not end up using; it reconciles into the existing
 * tree, so nothing on screen is lost.
 */

const sse = new SseChatGateway()
const jobs = new JobsChatGateway()

const gateway = new TransportSelectingChatGateway({
  advertised: probeTransports(),
  // Preference order: **asynchronous replies are the default** where the
  // backend can serve them. What that buys over the streamed path is a reply
  // that survives a dropped connection or a closed laptop lid, a reply longer
  // than a CDN's origin read timeout can carry, and a Stop button that actually
  // stops the work being paid for. What it costs is roughly 150 ms on a warm
  // first token, and about 2.5 s on a session's first message while the worker
  // starts — measured, not assumed.
  options: [
    { name: 'jobs', endpoint: `POST ${jobs.endpoint}`, gateway: jobs },
    { name: 'sse', endpoint: `POST ${sse.endpoint}`, gateway: sse },
  ],
  // Not derived from the order above, and that separation is what makes the
  // line above safe to change: this is the transport that has existed since the
  // beginning and that every deployment serves. The EC2 target advertises only
  // this one and so keeps working, unchanged, from this same bundle.
  fallback: 'sse',
  forced: askedForTransport(),
})

const container = document.getElementById('root')
if (container === null) {
  throw new Error('Missing #root element in index.html.')
}

const root = createRoot(container)

render()
void gateway.choose().then(render, render)

function render(): void {
  root.render(
    <StrictMode>
      <App
        gateway={gateway}
        endpoint={gateway.endpoint}
        scheme={window.location.protocol}
        host={window.location.host}
      />
    </StrictMode>,
  )
}

/**
 * A `?transport=` override, for verifying one transport before it is the default.
 *
 * Deliberately narrow: an unrecognised value is ignored rather than treated as a
 * transport that does not exist, and the gateway refuses to honour even a
 * recognised one that the backend does not advertise.
 */
function askedForTransport(): TransportName | undefined {
  const asked = new URLSearchParams(window.location.search).get('transport')
  return asked === 'sse' || asked === 'jobs' ? asked : undefined
}
