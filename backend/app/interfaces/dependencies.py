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

from app.application.chat_job_service import ChatJobService
from app.application.chat_service import ChatService
from app.domain.ports import JobDispatcher, JobStore, ReplyGenerator
from app.infrastructure.bedrock_reply_generator import (
    BedrockReplyGenerator,
    build_bedrock_runtime_client,
)
from app.infrastructure.canned_reply_generator import CannedReplyGenerator
from app.infrastructure.config import ReplySource, Settings
from app.infrastructure.dynamodb_job_store import (
    DynamoDbJobStore,
    build_dynamodb_client,
)
from app.infrastructure.lambda_job_dispatcher import (
    LambdaJobDispatcher,
    build_lambda_client,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_lambda.client import LambdaClient


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read configuration once per process."""
    return Settings.from_env()


SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache(maxsize=1)
def get_bedrock_client(region: str, read_timeout: float) -> BedrockRuntimeClient:
    """One client per process, not one per request.

    Cached because a client owns a connection pool and a credential resolver;
    rebuilding it per request would re-read the SSO cache or re-query IMDS before
    every reply. boto3 clients are safe to share across concurrent calls, which
    is what makes the cache sound rather than merely convenient.

    Deliberately *not* a FastAPI dependency. As one it would be resolved on every
    request regardless of which generator is wanted, so the canned path — the one
    that is supposed to need no AWS anything — would still construct a client.
    Called from the branch that actually needs it instead.

    ``read_timeout`` is a parameter rather than left at its default because the
    asynchronous worker's own timeout has to be sized around it: nothing can
    interrupt a blocked read, so the client's patience is the real bound on how
    long a stalled reply occupies a worker.
    """
    return build_bedrock_runtime_client(region, read_timeout=read_timeout)


@lru_cache(maxsize=1)
def get_dynamodb_client() -> DynamoDBClient:
    """One client per process. Cached for the same reasons as the Bedrock one.

    No region argument: the table is always in the region the process runs in,
    so boto3's own resolution is not merely sufficient but strictly safer than
    a setting that could disagree with reality.
    """
    return build_dynamodb_client()


@lru_cache(maxsize=1)
def get_lambda_client() -> LambdaClient:
    """One client per process, used only to hand work off."""
    return build_lambda_client()


def get_reply_generator(settings: SettingsDep) -> ReplyGenerator:
    """Choose the reply source. **The swap point.**"""
    if settings.reply_source is ReplySource.CANNED:
        return CannedReplyGenerator(
            word_delay_seconds=settings.reply_word_delay_seconds,
        )
    return BedrockReplyGenerator(
        get_bedrock_client(
            settings.bedrock_region, settings.bedrock_read_timeout_seconds
        ),
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


def get_job_store(settings: SettingsDep) -> JobStore:
    """Choose where asynchronous replies are kept. **The swap point.**

    There is one implementation, and asking for it when no table is configured
    is a programming error rather than a runtime condition: the routes that
    need it are not mounted in that case, and ``/api/health`` does not offer
    the transport. Failing loudly here beats a confusing failure later.
    """
    if not settings.jobs_table_name:
        raise RuntimeError("CHAT_JOBS_TABLE is not set, so no job store can be built.")
    return DynamoDbJobStore(get_dynamodb_client(), settings.jobs_table_name)


def get_job_dispatcher(settings: SettingsDep) -> JobDispatcher:
    """Choose how work reaches a worker. **The swap point.**"""
    if not settings.worker_function_name:
        raise RuntimeError(
            "CHAT_WORKER_FUNCTION_NAME is not set, so no dispatcher can be built."
        )
    return LambdaJobDispatcher(get_lambda_client(), settings.worker_function_name)


JobStoreDep = Annotated[JobStore, Depends(get_job_store)]
JobDispatcherDep = Annotated[JobDispatcher, Depends(get_job_dispatcher)]


def get_chat_job_service(
    chat_service: ChatServiceDep,
    store: JobStoreDep,
    dispatcher: JobDispatcherDep,
    settings: SettingsDep,
) -> ChatJobService:
    """Assemble the asynchronous use case.

    Note it composes ``ChatService`` rather than a ``ReplyGenerator``: both
    transports then share one set of prompt rules, one reply source and one
    error translation, so the only thing that can differ between them is how
    the answer gets to the browser.
    """
    return ChatJobService(
        chat_service,
        store,
        dispatcher,
        deadline_seconds=settings.job_deadline_seconds,
        retention_seconds=settings.job_retention_seconds,
        flush_interval_seconds=settings.job_flush_interval_seconds,
        flush_chars=settings.job_flush_chars,
        poll_interval_ms=settings.job_poll_interval_ms,
        max_reply_chars=settings.job_max_reply_chars,
    )


ChatJobServiceDep = Annotated[ChatJobService, Depends(get_chat_job_service)]
