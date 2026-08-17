"""Test doubles for the :class:`ReplyGenerator` port.

That these are this short is the return on the architecture: the use case depends
on a one-method port, so substituting it needs no mocking framework and no
patching.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from app.domain.entities import ChatRequest, ReplyChunk


class FakeReplyGenerator:
    """Yields the chunks it was given, and records what it was asked."""

    def __init__(self, texts: Sequence[str] = ("Hello ", "world")) -> None:
        self._texts = tuple(texts)
        self.requests: list[ChatRequest] = []

    async def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        self.requests.append(request)
        for text in self._texts:
            yield ReplyChunk(text=text)


class FailingReplyGenerator:
    """Raises after optionally emitting some chunks."""

    def __init__(
        self,
        error: Exception,
        *,
        texts_before_failure: Sequence[str] = (),
    ) -> None:
        self._error = error
        self._texts = tuple(texts_before_failure)

    async def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        for text in self._texts:
            yield ReplyChunk(text=text)
        raise self._error
