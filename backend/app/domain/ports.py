"""Ports: the interfaces the inner layers depend on.

Structural (``Protocol``) rather than nominal (``ABC``) on purpose — an adapter
does not import this module to satisfy it, so the dependency arrow stays pointed
inwards and test fakes stay to three lines.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.domain.entities import ChatRequest, ReplyChunk


@runtime_checkable
class ReplyGenerator(Protocol):
    """Produces a reply to a :class:`ChatRequest`, streamed in fragments.

    One method, because consumers use exactly one (ISP). Adding a second reply
    source means adding a new implementation, never editing the callers (OCP).

    Implementations are substitutable only if they all honour this contract
    (LSP) — a caller must never need to know which one it received:

    * **Yields at least one chunk** whose ``text`` is non-empty. A generator
      that produces nothing has failed and must say so by raising.
    * **Terminates.** The stream is finite; it is not a subscription.
    * **Raises only** :class:`~app.domain.errors.ReplyGenerationError`. Any
      transport, vendor or parsing failure is translated by the adapter that
      owns it, since that adapter is the only code that understands it.
    * **Does not mutate** the request it is given.

    Note the signature is a plain ``def`` returning an ``AsyncIterator``, not an
    ``async def``: that is the shape an ``async def`` generator function has, so
    implementations can simply ``yield``.
    """

    def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        """Stream the reply to ``request``."""
        ...
