"""Pydantic DTOs — the HTTP boundary, and the only place Pydantic appears.

The domain stays Pydantic-free, so these types also own the translation into
domain entities. Note what they validate and what they do not: *shape* here
(field present, correct type), *rules* in the use case. Duplicating the length
limit as a Pydantic constraint would give the same rule two homes and two
chances to disagree.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities import (
    ChatRequest,
    JobStatus,
    Message,
    ReplyProgress,
    Role,
)


class MessageDTO(BaseModel):
    """One prior turn, as it arrives over the wire."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str

    def to_domain(self) -> Message:
        return Message(role=Role(self.role), content=self.content)


class ChatRequestDTO(BaseModel):
    """Request body of ``POST /api/chat``."""

    model_config = ConfigDict(extra="forbid")

    message: str
    history: list[MessageDTO] = Field(default_factory=list)

    def to_domain(self) -> ChatRequest:
        return ChatRequest(
            prompt=self.message,
            history=tuple(item.to_domain() for item in self.history),
        )


class HealthDTO(BaseModel):
    """Response body of ``GET /api/health``, read by the container healthcheck.

    Also how the browser learns which chat transports this deployment can
    actually serve. One bundle is deployed to both targets and only one of them
    has the job routes, so the client asks rather than assumes.
    """

    status: Literal["ok"] = "ok"
    transports: list[str] = Field(default_factory=lambda: ["sse"])


class ErrorDTO(BaseModel):
    """A rejected request. Streaming failures are reported in-band instead."""

    detail: str


class ChatJobCreatedDTO(BaseModel):
    """Response body of ``POST /api/chat/jobs``."""

    job_id: str


class ChatJobProgressDTO(BaseModel):
    """Response body of a poll: everything generated since the cursor asked for.

    ``segments`` are concatenated verbatim in order, exactly as the fragments of
    the streamed transport are, so the two transports produce identical text.
    """

    status: JobStatus
    segments: list[str]
    cursor: int
    #: How long to wait before asking again; zero once there is nothing to wait
    #: for. Server-side so the cadence can be retuned without a new bundle.
    next_poll_ms: int
    error: str | None = None

    @classmethod
    def from_domain(cls, progress: ReplyProgress) -> ChatJobProgressDTO:
        return cls(
            status=progress.status,
            segments=[chunk.text for chunk in progress.chunks],
            cursor=progress.cursor,
            next_poll_ms=progress.next_poll_ms,
            error=progress.error,
        )


class WorkerEventDTO(BaseModel):
    """The hand-off payload, as the worker receives it.

    Unknown fields are **ignored rather than forbidden**, which is the opposite
    of every other DTO here and deliberate. This shape crosses a deployment
    boundary: the two functions are updated by separate calls that are not
    simultaneous, so for a moment a new sender talks to an old receiver. Adding
    a field must be survivable; forbidding extras would make it an outage.

    The nested request is the public request DTO unchanged, so there is one
    definition of what a chat request is and no second copy to drift.
    """

    model_config = ConfigDict(extra="ignore")

    job_id: str
    request: ChatRequestDTO


class WorkerAckDTO(BaseModel):
    """What the worker returns once it has finished with an event.

    A body at all, rather than 204, because this is the invocation's result and
    something has to be able to appear in a log or a failure record.
    """

    handled: Literal[True] = True
