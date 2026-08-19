"""Composition root — the only module that names concrete adapters.

This is where DIP is paid for. ``ChatService`` asks for a ``ReplyGenerator``;
this module is what decides the answer. That decision is now configuration:
Bedrock by default, the canned generator when asked for one.

Expressed as FastAPI dependencies so that tests can substitute any part of the
graph through ``app.dependency_overrides`` without touching production wiring.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.application.chat_service import ChatService
from app.domain.ports import ReplyGenerator
from app.infrastructure.bedrock_reply_generator import (
    BedrockReplyGenerator,
    build_bedrock_runtime_client,
)
from app.infrastructure.canned_reply_generator import CannedReplyGenerator
from app.infrastructure.config import ReplySource, Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_bedrock_runtime.client import BedrockRuntimeClient


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read configuration once per process."""
    return Settings.from_env()


SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache(maxsize=1)
def get_bedrock_client(region: str) -> BedrockRuntimeClient:
    """One client per process, not one per request.

    Cached because a client owns a connection pool and a credential resolver;
    rebuilding it per request would re-read the SSO cache or re-query IMDS before
    every reply. boto3 clients are safe to share across concurrent calls, which
    is what makes the cache sound rather than merely convenient.

    Deliberately *not* a FastAPI dependency. As one it would be resolved on every
    request regardless of which generator is wanted, so the canned path — the one
    that is supposed to need no AWS anything — would still construct a client.
    Called from the branch that actually needs it instead.
    """
    return build_bedrock_runtime_client(region)


def get_reply_generator(settings: SettingsDep) -> ReplyGenerator:
    """Choose the reply source. **The swap point.**"""
    if settings.reply_source is ReplySource.CANNED:
        return CannedReplyGenerator(
            word_delay_seconds=settings.reply_word_delay_seconds,
        )
    return BedrockReplyGenerator(
        get_bedrock_client(settings.bedrock_region),
        model_id=settings.bedrock_model_id,
        max_output_tokens=settings.bedrock_max_output_tokens,
        temperature=settings.bedrock_temperature,
        system_prompt=settings.system_prompt,
        max_history_messages=settings.max_history_messages,
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
