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
  // Preference order. **Flipping these two lines is what makes asynchronous
  // replies the default** — the change step 6 of the plan exists to make.
  options: [
    { name: 'sse', endpoint: `POST ${sse.endpoint}`, gateway: sse },
    { name: 'jobs', endpoint: `POST ${jobs.endpoint}`, gateway: jobs },
  ],
  // Not derived from the order above: this is the transport that has existed
  // since the beginning and is served by every deployment.
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
