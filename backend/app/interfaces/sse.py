"""Server-Sent Events wire format.

Sole responsibility: turning reply chunks into bytes on the wire (SRP). It knows
nothing about routing, HTTP status codes or where chunks come from — changing the
framing here can never touch reply logic, and vice versa.

Frame vocabulary, which the frontend gateway mirrors:

* ``data: {"delta": "Hi "}``  — a fragment of the reply, append verbatim
* ``data: {"error": "..."}``  — generation failed mid-stream
* ``data: [DONE]``            — terminal sentinel, always last
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from app.domain.entities import ReplyChunk
from app.domain.errors import ChatError

SSE_MEDIA_TYPE = "text/event-stream"
DONE_SENTINEL = "[DONE]"

STREAM_HEADERS = {
    # no-transform additionally forbids proxies from re-chunking the body
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Belt and braces alongside Caddy's flush_interval -1
    "X-Accel-Buffering": "no",
}

_GENERIC_ERROR_MESSAGE = "The assistant could not complete the reply."


def delta_event(text: str) -> str:
    """Frame one reply fragment.

    JSON-encoded rather than sent raw: SSE terminates a frame at a blank line,
    so a payload containing newlines would otherwise split into two frames.
    """
    return _frame(json.dumps({"delta": text}, ensure_ascii=False))


def error_event(message: str) -> str:
    """Frame an in-band failure."""
    return _frame(json.dumps({"error": message}, ensure_ascii=False))


def done_event() -> str:
    """Frame the terminal sentinel."""
    return _frame(DONE_SENTINEL)


async def event_stream(chunks: AsyncIterator[ReplyChunk]) -> AsyncIterator[str]:
    """Render ``chunks`` as SSE frames, always terminated by ``[DONE]``.

    Once a streaming response has begun the status code is already sent, so a
    mid-stream failure cannot become a 500. It is reported in-band as an error
    frame instead, and the sentinel still closes the stream — a client that
    waits for ``[DONE]`` never hangs.

    Only :class:`ChatError` is caught. Anything else is a defect in this process
    rather than a failed reply, and is left to propagate so it is logged rather
    than quietly rendered as a polite message to the user.
    """
    try:
        async for chunk in chunks:
            yield delta_event(chunk.text)
    except ChatError:
        yield error_event(_GENERIC_ERROR_MESSAGE)
    yield done_event()


def _frame(payload: str) -> str:
    return f"data: {payload}\n\n"
