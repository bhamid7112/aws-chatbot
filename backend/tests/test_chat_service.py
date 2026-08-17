"""The use case, in isolation. No FastAPI, no network, no event loop plumbing."""

from __future__ import annotations

import pytest

from app.application.chat_service import ChatService
from app.domain.entities import ChatRequest, Message, ReplyChunk, Role
from app.domain.errors import InvalidPromptError, ReplyGenerationError
from tests.fakes import FailingReplyGenerator, FakeReplyGenerator


async def _collect(service: ChatService, request: ChatRequest) -> str:
    return "".join([chunk.text async for chunk in service.stream_reply(request)])


class TestHappyPath:
    async def test_streams_every_chunk_in_order(self) -> None:
        service = ChatService(FakeReplyGenerator(["Hi, ", "there"]))

        assert await _collect(service, ChatRequest(prompt="hello")) == "Hi, there"

    async def test_passes_the_request_through_untouched(self) -> None:
        generator = FakeReplyGenerator()
        service = ChatService(generator)
        request = ChatRequest(
            prompt="hello",
            history=(Message(role=Role.USER, content="earlier"),),
        )

        await _collect(service, request)

        assert generator.requests == [request]

    async def test_drops_empty_chunks(self) -> None:
        """Empty fragments are noise on the wire; the reply is unaffected."""
        service = ChatService(FakeReplyGenerator(["Hi", "", " there"]))

        assert await _collect(service, ChatRequest(prompt="hello")) == "Hi there"


class TestValidation:
    @pytest.mark.parametrize("prompt", ["", "   ", "\n\t "])
    async def test_rejects_a_blank_prompt(self, prompt: str) -> None:
        service = ChatService(FakeReplyGenerator())

        with pytest.raises(InvalidPromptError):
            service.stream_reply(ChatRequest(prompt=prompt))

    async def test_rejects_a_prompt_over_the_limit(self) -> None:
        service = ChatService(FakeReplyGenerator(), max_prompt_chars=5)

        with pytest.raises(InvalidPromptError):
            service.stream_reply(ChatRequest(prompt="123456"))

    async def test_accepts_a_prompt_exactly_at_the_limit(self) -> None:
        service = ChatService(FakeReplyGenerator(), max_prompt_chars=5)

        assert await _collect(service, ChatRequest(prompt="12345"))

    async def test_validates_before_the_stream_is_iterated(self) -> None:
        """The caller needs the rejection early enough to choose a status code.

        Were validation deferred to first iteration, the streaming response
        would already have committed to 200.
        """
        service = ChatService(FakeReplyGenerator())

        with pytest.raises(InvalidPromptError):
            service.stream_reply(ChatRequest(prompt=""))

    def test_rejects_a_nonsensical_limit(self) -> None:
        with pytest.raises(ValueError):
            ChatService(FakeReplyGenerator(), max_prompt_chars=0)


class TestErrorTranslation:
    async def test_propagates_a_domain_error_unchanged(self) -> None:
        expected = ReplyGenerationError("upstream refused")
        service = ChatService(FailingReplyGenerator(expected))

        with pytest.raises(ReplyGenerationError) as caught:
            await _collect(service, ChatRequest(prompt="hello"))

        assert caught.value is expected

    async def test_wraps_an_unexpected_exception(self) -> None:
        """A misbehaving adapter must not leak its own exception types outward."""
        service = ChatService(FailingReplyGenerator(RuntimeError("socket closed")))

        with pytest.raises(ReplyGenerationError) as caught:
            await _collect(service, ChatRequest(prompt="hello"))

        assert isinstance(caught.value.__cause__, RuntimeError)

    async def test_reports_a_generator_that_yields_nothing(self) -> None:
        """The port promises at least one non-empty chunk; silence is a failure."""
        service = ChatService(FakeReplyGenerator([]))

        with pytest.raises(ReplyGenerationError):
            await _collect(service, ChatRequest(prompt="hello"))

    async def test_reports_a_generator_that_yields_only_empty_chunks(self) -> None:
        service = ChatService(FakeReplyGenerator(["", ""]))

        with pytest.raises(ReplyGenerationError):
            await _collect(service, ChatRequest(prompt="hello"))

    async def test_emits_chunks_produced_before_a_failure(self) -> None:
        """Whatever arrived is still delivered — the error follows it."""
        service = ChatService(
            FailingReplyGenerator(
                ReplyGenerationError("cut off"), texts_before_failure=["Hi "]
            )
        )

        received: list[ReplyChunk] = []
        with pytest.raises(ReplyGenerationError):
            async for chunk in service.stream_reply(ChatRequest(prompt="hello")):
                received.append(chunk)

        assert [chunk.text for chunk in received] == ["Hi "]
