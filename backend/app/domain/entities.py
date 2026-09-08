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


class JobStatus(StrEnum):
    """Where an asynchronous reply has got to.

    The only legal transitions are ``PENDING -> RUNNING`` and then
    ``RUNNING -> DONE | FAILED | CANCELLED``, plus
    ``PENDING -> CANCELLED`` for a job stopped before any worker claimed it.
    Every transition is made by a conditional write, so the store — not this
    enum — is what actually enforces them.
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True once no further transition is possible."""
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED})


class AppendOutcome(StrEnum):
    """What happened to an attempt to append a segment.

    ``SUPERSEDED`` is the one that matters and the reason this type exists at
    all. Appending is not idempotent, and the AWS SDK retries a call whose
    response was lost on the way back — so an append can commit and then be
    re-sent. Distinguishing that from a cancellation is what stops a network
    blip from duplicating text mid-reply.
    """

    APPLIED = "applied"
    SUPERSEDED = "superseded"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class AppendResult:
    """The outcome of an append, plus the store's own view of the job.

    ``cursor`` is authoritative: the writer resynchronises to it rather than
    trusting its own count, which is what makes a superseded append harmless.
    """

    outcome: AppendOutcome
    cursor: int
    status: JobStatus


@dataclass(frozen=True, slots=True)
class ChatJob:
    """A snapshot of an asynchronous reply as the store holds it.

    Note what is absent: the prompt and the history. They travel to the worker
    in the invocation payload and are never written here, so this entity holds
    generated text only.
    """

    job_id: str
    status: JobStatus
    segments: tuple[str, ...]
    created_at: int
    deadline_at: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReplyProgress:
    """What a polling client is told: the reply so far, from its cursor on.

    ``chunks`` holds only the segments the client has not seen. ``cursor`` is
    where it should ask from next, and is monotonic — a client that has read
    further than a stale snapshot simply gets nothing and asks again.
    """

    status: JobStatus
    chunks: tuple[ReplyChunk, ...]
    cursor: int
    next_poll_ms: int
    error: str | None = None
