"""Composition root — the only module that names concrete adapters.

This is where DIP is paid for. ``ChatService`` asks for a ``ReplyGenerator``;
this module is what decides the answer is ``CannedReplyGenerator``. Swapping in
Bedrock later is one edited line, here.

Expressed as FastAPI dependencies so that tests can substitute any part of the
graph through ``app.dependency_overrides`` without touching production wiring.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.application.chat_service import ChatService
from app.domain.ports import ReplyGenerator
from app.infrastructure.canned_reply_generator import CannedReplyGenerator
from app.infrastructure.config import Settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read configuration once per process."""
    return Settings.from_env()


SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_reply_generator(settings: SettingsDep) -> ReplyGenerator:
    """Choose the reply source. **The swap point** for a real model."""
    return CannedReplyGenerator(
        word_delay_seconds=settings.reply_word_delay_seconds,
    )


ReplyGeneratorDep = Annotated[ReplyGenerator, Depends(get_reply_generator)]


def get_chat_service(
    reply_generator: ReplyGeneratorDep,
    settings: SettingsDep,
) -> ChatService:
    """Assemble the use case from its port and its policy values."""
    return ChatService(
        reply_generator,
        max_prompt_chars=settings.max_prompt_chars,
    )


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
