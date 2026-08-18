import { useChat } from './application/useChat'
import type { ChatGateway } from './domain/chatGateway'
import { ChatWindow } from './presentation/ChatWindow'

/**
 * The container: it runs the use case and hands the result to the screen.
 *
 * The gateway arrives as a prop rather than being imported, so this component
 * names no implementation either — `main.tsx` is the only place that does. Point
 * it at a fake and the whole screen runs with no network.
 */

interface AppProps {
  readonly gateway: ChatGateway
  /** What the gateway posts to, for the wire readout. */
  readonly endpoint: string
  readonly scheme: string
  readonly host: string
}

export function App({ gateway, endpoint, scheme, host }: AppProps) {
  const { entries, wire, isStreaming, notice, send, stop } = useChat(gateway)

  return (
    <ChatWindow
      scheme={scheme}
      host={host}
      endpoint={endpoint}
      entries={entries}
      wire={wire}
      isStreaming={isStreaming}
      notice={notice}
      onSend={send}
      onStop={stop}
    />
  )
}
