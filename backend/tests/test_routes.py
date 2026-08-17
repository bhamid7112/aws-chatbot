"""The HTTP boundary: status codes, and the SSE framing the frontend parses."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.domain.errors import ReplyGenerationError
from app.interfaces.sse import DONE_SENTINEL, SSE_MEDIA_TYPE
from tests.conftest import ClientFactory
from tests.fakes import FailingReplyGenerator, FakeReplyGenerator


def _frames(body: str) -> list[str]:
    """The payload of each ``data:`` frame, in order."""
    return [
        block.removeprefix("data: ").strip()
        for block in body.split("\n\n")
        if block.strip()
    ]


def _deltas(body: str) -> list[str]:
    return [
        json.loads(frame)["delta"]
        for frame in _frames(body)
        if frame != DONE_SENTINEL and "delta" in json.loads(frame)
    ]


class TestHealth:
    def test_reports_ok(self, client: TestClient) -> None:
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestChatStream:
    def test_streams_the_reply_as_sse(self, client_factory: ClientFactory) -> None:
        with client_factory(FakeReplyGenerator(["Hi, ", "there"])) as client:
            response = client.post("/api/chat", json={"message": "hello"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith(SSE_MEDIA_TYPE)
        assert _deltas(response.text) == ["Hi, ", "there"]

    def test_terminates_with_the_done_sentinel(self, client: TestClient) -> None:
        response = client.post("/api/chat", json={"message": "hello"})

        assert _frames(response.text)[-1] == DONE_SENTINEL

    def test_forbids_buffering_proxies(self, client: TestClient) -> None:
        """Without these the reply arrives in one lump and streaming is moot."""
        response = client.post("/api/chat", json={"message": "hello"})

        assert response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in response.headers["cache-control"]

    def test_forwards_history(self, client_factory: ClientFactory) -> None:
        generator = FakeReplyGenerator()
        with client_factory(generator) as client:
            client.post(
                "/api/chat",
                json={
                    "message": "and then?",
                    "history": [{"role": "user", "content": "earlier"}],
                },
            )

        request = generator.requests[0]
        assert request.prompt == "and then?"
        assert [(m.role.value, m.content) for m in request.history] == [
            ("user", "earlier")
        ]


class TestChatRejection:
    def test_rejects_a_blank_message_before_streaming(self, client: TestClient) -> None:
        """422 with a JSON body, not a 200 stream carrying an error frame."""
        response = client.post("/api/chat", json={"message": "   "})

        assert response.status_code == 422
        assert not response.headers["content-type"].startswith(SSE_MEDIA_TYPE)

    def test_rejects_a_missing_message(self, client: TestClient) -> None:
        assert client.post("/api/chat", json={}).status_code == 422

    def test_rejects_an_unknown_role(self, client: TestClient) -> None:
        response = client.post(
            "/api/chat",
            json={
                "message": "hello",
                "history": [{"role": "system", "content": "hi"}],
            },
        )

        assert response.status_code == 422


class TestMidStreamFailure:
    def test_reports_the_failure_in_band_and_still_closes(
        self, client_factory: ClientFactory
    ) -> None:
        """The status code is already sent, so the error has to travel in-band.

        The sentinel must still arrive — a client waiting for ``[DONE]`` would
        otherwise hang forever.
        """
        generator = FailingReplyGenerator(
            ReplyGenerationError("upstream refused"), texts_before_failure=["Hi "]
        )
        with client_factory(generator) as client:
            response = client.post("/api/chat", json={"message": "hello"})

        frames = _frames(response.text)
        assert response.status_code == 200
        assert _deltas(response.text) == ["Hi "]
        assert "error" in json.loads(frames[-2])
        assert frames[-1] == DONE_SENTINEL

    def test_does_not_leak_internal_detail(self, client_factory: ClientFactory) -> None:
        generator = FailingReplyGenerator(ReplyGenerationError("boto3 timeout at db"))
        with client_factory(generator) as client:
            response = client.post("/api/chat", json={"message": "hello"})

        assert "boto3" not in response.text
