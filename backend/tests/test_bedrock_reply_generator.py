"""The Bedrock adapter, against a stub client.

No credentials, no network, no moto: the adapter takes its client as an argument
precisely so that the interesting behaviour — the request it builds, the events it
translates, the failures it converts — can be tested without AWS.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from app.application.chat_service import ChatService
from app.domain.entities import ChatRequest, Message, Role
from app.domain.errors import ReplyGenerationError
from app.domain.ports import ReplyGenerator
from app.infrastructure.bedrock_reply_generator import BedrockReplyGenerator


def _delta(text: str) -> dict[str, Any]:
    return {"contentBlockDelta": {"delta": {"text": text}}}


class StubClient:
    """Records the Converse request and replays a canned event stream."""

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        default = [_delta("Hi "), _delta("there")]
        self._events = default if events is None else events
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def converse_stream(self, **kwargs: Any) -> dict[str, Iterator[dict[str, Any]]]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"stream": iter(self._events)}


class FailingStream:
    """Raises partway through iteration, as a dropped connection would."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def converse_stream(self, **kwargs: Any) -> dict[str, Iterator[dict[str, Any]]]:
        def events() -> Iterator[dict[str, Any]]:
            yield _delta("Half a rep")
            raise self._error

        return {"stream": events()}


def _client_error(code: str = "ValidationException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "arn:aws:bedrock:internal-detail"}},
        "ConverseStream",
    )


async def _collect(generator: BedrockReplyGenerator, request: ChatRequest) -> list[str]:
    return [chunk.text async for chunk in generator.generate(request)]


def _make(client: Any, **kwargs: Any) -> BedrockReplyGenerator:
    return BedrockReplyGenerator(client, **kwargs)


def _texts(call: dict[str, Any]) -> list[str]:
    return [message["content"][0]["text"] for message in call["messages"]]


def _roles(call: dict[str, Any]) -> list[str]:
    return [message["role"] for message in call["messages"]]


# ── the port's contract ───────────────────────────────────────────────────────


def test_satisfies_the_port() -> None:
    assert isinstance(_make(StubClient()), ReplyGenerator)


async def test_streams_the_deltas_it_is_given() -> None:
    chunks = await _collect(_make(StubClient()), ChatRequest(prompt="hello"))

    assert chunks == ["Hi ", "there"]


async def test_chunks_concatenate_verbatim() -> None:
    """Spacing travels inside the chunk text; the adapter must not reflow it."""
    events = [_delta("Two "), _delta("words"), _delta("!")]

    chunks = await _collect(_make(StubClient(events)), ChatRequest(prompt="x"))

    assert "".join(chunks) == "Two words!"


async def test_skips_events_that_carry_no_reply_text() -> None:
    """Block boundaries, stop reasons and usage metadata are not reply content."""
    events: list[dict[str, Any]] = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        _delta("Only "),
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "ignored"}}}},
        _delta("this"),
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 2}}},
    ]

    chunks = await _collect(_make(StubClient(events)), ChatRequest(prompt="x"))

    assert chunks == ["Only ", "this"]


async def test_never_yields_an_empty_chunk() -> None:
    events = [_delta(""), _delta("real"), _delta("")]

    chunks = await _collect(_make(StubClient(events)), ChatRequest(prompt="x"))

    assert chunks == ["real"]


# ── the request it builds ─────────────────────────────────────────────────────


async def test_sends_max_tokens_explicitly() -> None:
    """Unset, Converse reserves the model maximum against the token quota."""
    client = StubClient()

    await _collect(
        _make(client, max_output_tokens=256, temperature=0.1),
        ChatRequest(prompt="x"),
    )

    assert client.calls[0]["inferenceConfig"] == {"maxTokens": 256, "temperature": 0.1}


async def test_sends_the_model_id_and_system_prompt() -> None:
    client = StubClient()

    await _collect(
        _make(client, model_id="google.gemma-3-27b-it", system_prompt="Be terse."),
        ChatRequest(prompt="x"),
    )

    assert client.calls[0]["modelId"] == "google.gemma-3-27b-it"
    assert client.calls[0]["system"] == [{"text": "Be terse."}]


async def test_omits_the_system_block_when_the_prompt_is_blank() -> None:
    """An empty system prompt means send none, not send an empty one."""
    client = StubClient()

    await _collect(_make(client, system_prompt="   "), ChatRequest(prompt="x"))

    assert "system" not in client.calls[0]


async def test_the_prompt_becomes_the_final_user_turn() -> None:
    client = StubClient()

    await _collect(_make(client), ChatRequest(prompt="What is Bedrock?"))

    assert client.calls[0]["messages"] == [
        {"role": "user", "content": [{"text": "What is Bedrock?"}]}
    ]


async def test_history_is_sent_before_the_prompt() -> None:
    client = StubClient()
    request = ChatRequest(
        prompt="And the second?",
        history=(
            Message(Role.USER, "Name an AWS region."),
            Message(Role.ASSISTANT, "us-east-1."),
        ),
    )

    await _collect(_make(client), request)

    assert client.calls[0]["messages"] == [
        {"role": "user", "content": [{"text": "Name an AWS region."}]},
        {"role": "assistant", "content": [{"text": "us-east-1."}]},
        {"role": "user", "content": [{"text": "And the second?"}]},
    ]


async def test_history_is_trimmed_to_a_window() -> None:
    """The browser sends the whole conversation; input is re-billed every turn."""
    client = StubClient()
    history = tuple(
        Message(Role.USER if index % 2 == 0 else Role.ASSISTANT, f"turn {index}")
        for index in range(10)
    )

    await _collect(_make(client, max_history_messages=4), ChatRequest("now", history))

    assert _texts(client.calls[0]) == ["turn 6", "turn 7", "turn 8", "turn 9", "now"]


async def test_a_window_of_zero_sends_no_history() -> None:
    client = StubClient()
    history = (Message(Role.USER, "old"), Message(Role.ASSISTANT, "older"))

    await _collect(_make(client, max_history_messages=0), ChatRequest("now", history))

    assert _texts(client.calls[0]) == ["now"]


# ── repairing untrusted history ───────────────────────────────────────────────
#
# History arrives from the browser. Converse requires alternating turns starting
# with a user turn, and answers anything else with a ValidationException — which
# the user would experience as a chat that simply stopped working. Degrading the
# context is the better failure.


async def test_drops_a_leading_assistant_turn() -> None:
    client = StubClient()
    history = (
        Message(Role.ASSISTANT, "unprompted"),
        Message(Role.USER, "hi"),
        Message(Role.ASSISTANT, "hello"),
    )

    await _collect(_make(client), ChatRequest("now", history))

    assert _texts(client.calls[0]) == ["hi", "hello", "now"]


async def test_a_dangling_user_turn_goes_too() -> None:
    """Dropping the leading turn can leave the history ending on a user turn.

    That is two user turns in a row once the prompt is appended, so the dangling
    one has to go as well — even though it was the only history left.
    """
    client = StubClient()
    history = (Message(Role.ASSISTANT, "unprompted"), Message(Role.USER, "hi"))

    await _collect(_make(client), ChatRequest("now", history))

    assert _texts(client.calls[0]) == ["now"]


async def test_drops_consecutive_same_role_turns() -> None:
    client = StubClient()
    history = (
        Message(Role.USER, "first"),
        Message(Role.USER, "duplicate"),
        Message(Role.ASSISTANT, "reply"),
    )

    await _collect(_make(client), ChatRequest("now", history))

    assert _texts(client.calls[0]) == ["first", "reply", "now"]


async def test_never_sends_two_user_turns_in_a_row() -> None:
    """A history ending on a user turn would collide with the prompt itself."""
    client = StubClient()
    history = (Message(Role.USER, "dangling"),)

    await _collect(_make(client), ChatRequest("now", history))

    assert _roles(client.calls[0]) == ["user"]


async def test_repaired_history_always_alternates_from_user() -> None:
    client = StubClient()
    history = (
        Message(Role.ASSISTANT, "a"),
        Message(Role.ASSISTANT, "b"),
        Message(Role.USER, "c"),
        Message(Role.USER, "d"),
        Message(Role.ASSISTANT, "e"),
    )

    await _collect(_make(client), ChatRequest("now", history))

    roles = _roles(client.calls[0])
    assert roles == ["user", "assistant", "user"]


# ── failure translation ───────────────────────────────────────────────────────


async def test_a_client_error_becomes_a_reply_generation_error() -> None:
    generator = _make(StubClient(error=_client_error()))

    with pytest.raises(ReplyGenerationError):
        await _collect(generator, ChatRequest(prompt="x"))


async def test_a_transport_error_becomes_a_reply_generation_error() -> None:
    error = ConnectTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.aws")
    generator = _make(StubClient(error=error))

    with pytest.raises(ReplyGenerationError):
        await _collect(generator, ChatRequest(prompt="x"))


async def test_an_error_midstream_becomes_a_reply_generation_error() -> None:
    generator = _make(FailingStream(_client_error("ModelStreamErrorException")))

    with pytest.raises(ReplyGenerationError):
        await _collect(generator, ChatRequest(prompt="x"))


async def test_the_error_message_carries_no_vendor_detail() -> None:
    """The message travels outwards; the cause is for the log, not the user."""
    generator = _make(StubClient(error=_client_error()))

    with pytest.raises(ReplyGenerationError) as raised:
        await _collect(generator, ChatRequest(prompt="x"))

    message = str(raised.value)
    assert "arn:aws:bedrock" not in message
    assert "ValidationException" not in message


async def test_the_original_cause_is_chained_for_the_log() -> None:
    original = _client_error()
    generator = _make(StubClient(error=original))

    with pytest.raises(ReplyGenerationError) as raised:
        await _collect(generator, ChatRequest(prompt="x"))

    assert raised.value.__cause__ is original


async def test_an_empty_stream_surfaces_through_the_use_case() -> None:
    """The port forbids yielding nothing; ChatService is where that is enforced."""
    service = ChatService(_make(StubClient([])))

    with pytest.raises(ReplyGenerationError):
        async for _ in service.stream_reply(ChatRequest(prompt="x")):
            pass


# ── construction ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model_id": "  "},
        {"max_output_tokens": 0},
        {"temperature": -0.1},
        {"temperature": 1.1},
        {"max_history_messages": -1},
    ],
)
def test_rejects_nonsense_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _make(StubClient(), **kwargs)
