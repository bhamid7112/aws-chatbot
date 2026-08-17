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

from app.domain.entities import ChatRequest, Message, Role


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
    """Response body of ``GET /api/health``, read by the container healthcheck."""

    status: Literal["ok"] = "ok"


class ErrorDTO(BaseModel):
    """A rejected request. Streaming failures are reported in-band instead."""

    detail: str
