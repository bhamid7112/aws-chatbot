"""A :class:`ReplyGenerator` that always says the same thing.

**This is the LLM swap point.** Adding a real model later means adding a
sibling module (``bedrock_reply_generator.py``) and changing one line at the
composition root — zero edits to ``domain`` or ``application`` (OCP).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.domain.entities import ChatRequest, ReplyChunk

DEFAULT_REPLY = "Hi, I'm your AI assistant. How may I help you?"
DEFAULT_WORD_DELAY_SECONDS = 0.06


class CannedReplyGenerator:
    """Streams a fixed reply word by word, to simulate a model typing.

    The only place in the codebase where the reply text lives. Ignores the
    request entirely, which is the whole point: the reply is canned.
    """

    def __init__(
        self,
        reply: str = DEFAULT_REPLY,
        *,
        word_delay_seconds: float = DEFAULT_WORD_DELAY_SECONDS,
    ) -> None:
        if not reply.strip():
            raise ValueError("reply must contain at least one non-whitespace word")
        if word_delay_seconds < 0:
            raise ValueError("word_delay_seconds must not be negative")
        self._chunks = self._split_into_chunks(reply)
        self._word_delay_seconds = word_delay_seconds

    async def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        """Yield the canned reply one word at a time.

        Cannot fail, so it never raises ``ReplyGenerationError`` — the contract
        permits that exception, it does not oblige it.
        """
        for index, text in enumerate(self._chunks):
            if index and self._word_delay_seconds:
                await asyncio.sleep(self._word_delay_seconds)
            yield ReplyChunk(text=text)

    @staticmethod
    def _split_into_chunks(reply: str) -> tuple[str, ...]:
        """Split into words, keeping the separating space on the leading word.

        Consumers concatenate chunks verbatim, so spacing has to travel with the
        text rather than being reinserted downstream.
        """
        words = reply.split()
        return tuple(
            word if index == len(words) - 1 else f"{word} "
            for index, word in enumerate(words)
        )
