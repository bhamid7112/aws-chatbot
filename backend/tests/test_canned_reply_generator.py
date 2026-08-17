"""The canned adapter — chiefly, that it honours the port's contract."""

from __future__ import annotations

import pytest

from app.domain.entities import ChatRequest
from app.domain.ports import ReplyGenerator
from app.infrastructure.canned_reply_generator import (
    DEFAULT_REPLY,
    CannedReplyGenerator,
)


async def _collect(generator: CannedReplyGenerator) -> list[str]:
    request = ChatRequest(prompt="anything")
    return [chunk.text async for chunk in generator.generate(request)]


def test_satisfies_the_port() -> None:
    assert isinstance(CannedReplyGenerator(), ReplyGenerator)


async def test_chunks_reassemble_into_the_original_reply() -> None:
    """Chunks are concatenated verbatim downstream, so spacing must survive."""
    chunks = await _collect(CannedReplyGenerator(word_delay_seconds=0))

    assert "".join(chunks) == DEFAULT_REPLY


async def test_streams_word_by_word() -> None:
    chunks = await _collect(
        CannedReplyGenerator("Hi there friend", word_delay_seconds=0)
    )

    assert chunks == ["Hi ", "there ", "friend"]


async def test_yields_at_least_one_non_empty_chunk() -> None:
    chunks = await _collect(CannedReplyGenerator(word_delay_seconds=0))

    assert chunks
    assert all(chunk for chunk in chunks)


async def test_ignores_the_request() -> None:
    """The reply is canned; that is the point, and it is worth pinning down."""
    generator = CannedReplyGenerator(word_delay_seconds=0)

    first = [c.text async for c in generator.generate(ChatRequest(prompt="one"))]
    second = [c.text async for c in generator.generate(ChatRequest(prompt="two"))]

    assert first == second


@pytest.mark.parametrize("reply", ["", "   "])
def test_rejects_a_reply_with_no_words(reply: str) -> None:
    with pytest.raises(ValueError):
        CannedReplyGenerator(reply)


def test_rejects_a_negative_delay() -> None:
    with pytest.raises(ValueError):
        CannedReplyGenerator(word_delay_seconds=-1)
