"""Shared fixtures.

The client fixture overrides ``get_chat_service`` rather than patching anything:
the composition root is expressed as dependencies precisely so a test can
substitute the graph from outside.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.chat_service import ChatService
from app.domain.ports import ReplyGenerator
from app.infrastructure.config import Settings
from app.interfaces.dependencies import get_chat_service
from app.main import create_app
from tests.fakes import FakeReplyGenerator


class ClientFactory(Protocol):
    """Builds a test client whose chat service uses ``generator``."""

    def __call__(self, generator: ReplyGenerator | None = None) -> TestClient: ...


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings — no streaming delay, no CORS middleware."""
    return Settings(reply_word_delay_seconds=0.0, cors_allow_origins=())


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client_factory(app: FastAPI) -> Iterator[ClientFactory]:
    def _factory(generator: ReplyGenerator | None = None) -> TestClient:
        service = ChatService(generator or FakeReplyGenerator())
        app.dependency_overrides[get_chat_service] = lambda: service
        return TestClient(app)

    yield _factory
    app.dependency_overrides.clear()


@pytest.fixture
def client(client_factory: ClientFactory) -> Iterator[TestClient]:
    with client_factory() as test_client:
        yield test_client
