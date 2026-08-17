"""The chat use case.

Depends on ``domain`` and nothing else — no FastAPI, no Pydantic, no boto3, no
knowledge of SSE. That is what makes it testable against a three-line fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.entities import ChatRequest, ReplyChunk
from app.domain.errors import InvalidPromptError, ReplyGenerationError
from app.domain.ports import ReplyGenerator

DEFAULT_MAX_PROMPT_CHARS = 4_000


class ChatService:
    """Answers a chat request by delegating to a :class:`ReplyGenerator`.

    Three responsibilities, and only these three (SRP): validate the request,
    delegate generation, and guarantee the port's contract to its own caller.
    It decides *nothing* about what the reply says — that is the generator's
    job — and nothing about how the reply is framed on the wire.

    The constructor takes the **port**, never a concrete class (DIP); which
    implementation arrives is chosen at the composition root.
    """

    def __init__(
        self,
        reply_generator: ReplyGenerator,
        *,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    ) -> None:
        if max_prompt_chars < 1:
            raise ValueError("max_prompt_chars must be at least 1")
        self._reply_generator = reply_generator
        self._max_prompt_chars = max_prompt_chars

    def stream_reply(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        """Validate ``request``, then return the stream of reply chunks.

        Validation happens **now**, synchronously, rather than on first
        iteration. That is deliberate: the caller can therefore reject a bad
        request with a normal error response, before any streaming response has
        begun and its status code has become unchangeable.

        Raises:
            InvalidPromptError: The prompt is blank or too long.
            ReplyGenerationError: Raised while iterating, if generation fails.
        """
        self._validate(request)
        return self._generate(request)

    def _validate(self, request: ChatRequest) -> None:
        if not request.prompt.strip():
            raise InvalidPromptError("The prompt must not be empty.")
        if len(request.prompt) > self._max_prompt_chars:
            raise InvalidPromptError(
                f"The prompt must be at most {self._max_prompt_chars} characters."
            )

    async def _generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        """Relay the generator's chunks, upholding its contract on its behalf.

        A generator that misbehaves — raises something exotic, or yields nothing
        — is normalised here into a ``ReplyGenerationError``. Consumers can then
        rely on the contract absolutely, rather than defensively, no matter which
        adapter is installed.
        """
        emitted = 0
        try:
            async for chunk in self._reply_generator.generate(request):
                if not chunk.text:
                    continue
                emitted += 1
                yield chunk
        except ReplyGenerationError:
            raise
        except Exception as exc:  # the contract-enforcement point
            raise ReplyGenerationError(
                "The reply generator failed unexpectedly."
            ) from exc

        if emitted == 0:
            raise ReplyGenerationError("The reply generator produced no content.")
