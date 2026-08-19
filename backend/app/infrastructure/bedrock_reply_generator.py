"""A :class:`ReplyGenerator` backed by Amazon Bedrock's Converse API.

The real model, and the sibling that ``canned_reply_generator`` was written to
anticipate. Everything vendor-specific stops here: boto3, botocore's exception
hierarchy, the Converse request shape and the event-stream vocabulary are all
translated at this boundary, so ``domain`` and ``application`` remain unaware
that a model is involved at all (DIP).

Credentials are conspicuously absent from this module. boto3's default chain
resolves them — an SSO profile in development, the EC2 instance role in
production — and the application never learns which, so one image runs in both
places unmodified.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

import anyio.to_thread
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.domain.entities import ChatRequest, Message, ReplyChunk, Role
from app.domain.errors import ReplyGenerationError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from mypy_boto3_bedrock_runtime.client import BedrockRuntimeClient

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "google.gemma-3-27b-it"
# In-region only: this model has no cross-region inference profile, so there is
# no `us.` prefixed ID to fall back to and the region is a real constraint.
DEFAULT_REGION = "us-east-1"
# Explicit, and it matters. Left unset, Converse reserves the model's maximum
# (8K here) against the account's per-minute token quota on every single call —
# the usual cause of a ThrottlingException that arrives long before the traffic
# would justify one.
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer clearly and concisely. "
    "If you do not know something, say so plainly."
)
# A window, not the whole conversation: the browser sends back every turn it has,
# and each one is re-billed as input on every request.
DEFAULT_MAX_HISTORY_MESSAGES = 20

# Slow enough to let a long answer finish, short enough that a hung connection
# does not hold a request open indefinitely.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 60.0

_ROLE_NAMES: dict[Role, str] = {Role.USER: "user", Role.ASSISTANT: "assistant"}


def build_bedrock_runtime_client(
    region: str = DEFAULT_REGION,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
) -> BedrockRuntimeClient:
    """Build the client the generator talks to.

    Separate from the class so that the class can be unit-tested against a stub
    — constructing a real client resolves credentials, which no test should need
    — and so botocore's ``Config`` is written down exactly once.

    ``bedrock-runtime``, not ``bedrock``: the latter is the control plane and has
    no ``converse_stream`` at all.
    """
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            # Adaptive rather than standard: it backs off with client-side rate
            # limiting when Bedrock starts throttling, instead of retrying into
            # the same wall.
            retries={"max_attempts": 5, "mode": "adaptive"},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        ),
    )


class BedrockReplyGenerator:
    """Streams a model's reply from Bedrock, one text fragment at a time.

    Honours the port's contract at its own boundary: every failure it can
    encounter leaves as a :class:`ReplyGenerationError` and nothing else.
    """

    def __init__(
        self,
        client: BedrockRuntimeClient,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_history_messages: int = DEFAULT_MAX_HISTORY_MESSAGES,
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id must not be blank")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")
        if not 0.0 <= temperature <= 1.0:
            raise ValueError("temperature must be between 0.0 and 1.0")
        if max_history_messages < 0:
            raise ValueError("max_history_messages must not be negative")

        self._client = client
        self._model_id = model_id
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._system_prompt = system_prompt.strip()
        self._max_history_messages = max_history_messages

    async def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        """Stream the model's reply to ``request``.

        Raises:
            ReplyGenerationError: For any transport, service or protocol
                failure. The underlying cause is logged here and deliberately
                kept out of the exception message, which travels outwards.
        """
        try:
            stream = await anyio.to_thread.run_sync(self._start_stream, request)
            async for event in _aiterate(stream):
                text = _delta_text(event)
                if text:
                    yield ReplyChunk(text=text)
        except ReplyGenerationError:
            raise
        except (ClientError, BotoCoreError) as exc:
            logger.exception("Bedrock rejected or dropped a converse_stream call")
            raise ReplyGenerationError("The model could not be reached.") from exc
        except Exception as exc:
            logger.exception("Unexpected failure while streaming from Bedrock")
            raise ReplyGenerationError("The model reply failed.") from exc

    def _start_stream(self, request: ChatRequest) -> Iterator[Any]:
        """Open the response stream. Blocking, so callers run it off the loop."""
        kwargs: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": self._to_converse_messages(request),
            "inferenceConfig": {
                "maxTokens": self._max_output_tokens,
                "temperature": self._temperature,
            },
        }
        if self._system_prompt:
            kwargs["system"] = [{"text": self._system_prompt}]

        response = self._client.converse_stream(**kwargs)
        return iter(response["stream"])

    def _to_converse_messages(self, request: ChatRequest) -> list[dict[str, Any]]:
        """Render the request as a Converse ``messages`` list.

        The history arrives from the browser, so it is *repaired* rather than
        trusted. Converse requires turns to alternate and to begin with a user
        turn; a history that does not is rejected with a ``ValidationException``,
        which the user would experience as a chat that simply stopped working.
        Dropping the offending turns degrades the reply slightly instead, which
        is the better failure.
        """
        window = (
            request.history[-self._max_history_messages :]
            if self._max_history_messages
            else ()
        )

        messages: list[dict[str, Any]] = []
        for message in window:
            expected = Role.USER if len(messages) % 2 == 0 else Role.ASSISTANT
            if message.role is not expected:
                continue
            messages.append(_to_converse_message(message))

        # The prompt is a user turn, so anything ending on one has to go: two
        # consecutive user turns is the same validation error by another route.
        if len(messages) % 2 == 1:
            messages.pop()

        messages.append(_to_converse_message(Message(Role.USER, request.prompt)))
        return messages


def _to_converse_message(message: Message) -> dict[str, Any]:
    return {"role": _ROLE_NAMES[message.role], "content": [{"text": message.content}]}


def _delta_text(event: dict[str, Any]) -> str:
    """Extract reply text from one stream event, if it carries any.

    Events also announce block boundaries, stop reasons and token usage, and a
    ``contentBlockDelta`` can hold non-text content. Anything that is not reply
    text is skipped rather than guessed at.
    """
    delta = event.get("contentBlockDelta", {}).get("delta", {})
    text = delta.get("text")
    return text if isinstance(text, str) else ""


async def _aiterate(iterator: Iterator[Any]) -> AsyncIterator[Any]:
    """Consume a blocking iterator without blocking the event loop.

    botocore's event stream is synchronous: ``for event in response["stream"]``
    waits on the socket. Doing that directly inside an ``async def`` generator
    stalls the whole loop between tokens, so a second chat cannot even begin
    until the first has finished — every concurrent request would be serialised
    behind the slowest one. Each ``next()`` therefore goes to a worker thread.

    anyio rather than a new dependency: FastAPI already runs on it.
    """
    sentinel = object()

    def advance() -> Any:
        return next(iterator, sentinel)

    while True:
        item = await anyio.to_thread.run_sync(advance)
        if item is sentinel:
            return
        yield item
