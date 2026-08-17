"""Domain entities: plain data, no framework types, no behaviour beyond shape.

Deliberately free of validation. Enforcing "what counts as a valid request" is a
separate responsibility that belongs to the use case (see
``application.chat_service``), so that there is exactly one place to look when a
rule changes. These types only answer *what a conversation is made of*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    """Who authored a message."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    """A single turn in a conversation."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A prompt to answer, plus the turns that came before it."""

    prompt: str
    history: tuple[Message, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ReplyChunk:
    """One fragment of a reply, emitted as it becomes available.

    Chunks are concatenated verbatim by the consumer, so any spacing between
    words must be carried inside ``text``.
    """

    text: str
