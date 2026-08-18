import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import { SseChatGateway } from './infrastructure/sseChatGateway'

import './styles/tokens.css'
import './styles/base.css'

/**
 * The composition root — the counterpart of `backend/app/interfaces/dependencies.py`.
 *
 * The only module in the frontend that names a concrete adapter. Swapping the
 * transport, or standing the UI up against a fake, is an edit to this file and
 * to nothing else.
 */

const gateway = new SseChatGateway()

const container = document.getElementById('root')
if (container === null) {
  throw new Error('Missing #root element in index.html.')
}

createRoot(container).render(
  <StrictMode>
    <App
      gateway={gateway}
      endpoint={`POST ${gateway.endpoint}`}
      scheme={window.location.protocol}
      host={window.location.host}
    />
  </StrictMode>,
)
