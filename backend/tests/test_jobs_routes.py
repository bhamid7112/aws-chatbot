"""The asynchronous transport's HTTP boundary.

Two things are asserted here that are easy to overlook and expensive to get
wrong:

* The job routes are **mounted conditionally**. One bundle serves two
  deployment targets and only one has a job store, so on the other these paths
  must be genuinely absent — a 404, matching what ``/api/health`` reports.
* The worker's entrypoint is mounted **only for a worker**. Nothing a viewer can
  reach routes to it on either target today, but that is a property of a CDN
  path pattern and a proxy matcher, not of the application.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.application.chat_job_service import ChatJobRunner, ChatJobService
from app.application.chat_service import ChatService
from app.domain.entities import JobStatus
from app.domain.errors import JobDispatchError
from app.infrastructure.config import ProcessRole, ReplySource, Settings
from app.interfaces.dependencies import (
    get_chat_job_runner,
    get_chat_job_service,
    get_job_store,
)
from app.main import create_app
from tests.fakes import FakeJobDispatcher, FakeJobStore, FakeReplyGenerator


def _settings(
    *,
    role: ProcessRole = ProcessRole.API,
    table: str = "jobs",
    worker: str = "chatbot-worker",
    canned: bool = False,
) -> Settings:
    return Settings(
        reply_source=ReplySource.CANNED if canned else ReplySource.BEDROCK,
        reply_word_delay_seconds=0.0,
        cors_allow_origins=(),
        role=role,
        jobs_table_name=table,
        worker_function_name=worker,
    )


class Harness:
    """An app wired to fakes, plus the fakes, so a test can inspect both."""

    def __init__(
        self,
        *,
        role: ProcessRole = ProcessRole.API,
        table: str = "jobs",
        worker: str = "chatbot-worker",
        dispatcher: FakeJobDispatcher | None = None,
        texts: tuple[str, ...] = ("Hi ", "there"),
    ) -> None:
        self.store = FakeJobStore()
        self.dispatcher = dispatcher or FakeJobDispatcher()
        self.app = create_app(_settings(role=role, table=table, worker=worker))

        chat = ChatService(FakeReplyGenerator(texts))
        service = ChatJobService(chat, self.store, self.dispatcher)
        runner = ChatJobRunner(chat, self.store, flush_chars=1)
        self.app.dependency_overrides[get_chat_job_service] = lambda: service
        self.app.dependency_overrides[get_chat_job_runner] = lambda: runner

    def client(self) -> TestClient:
        return TestClient(self.app)


class RealGraphHarness:
    """An app whose use cases are assembled by the real composition root.

    Exists for one question the harness above cannot ask: does the graph the
    deployment actually builds *resolve*? Overriding ``get_chat_job_runner``
    substitutes the very thing whose construction was broken, which is how a
    worker that could not build its own collaborators passed every test and
    then failed every event in production.

    Only the store is substituted, and only because it is the one collaborator
    that would otherwise open a connection to AWS. Everything above it —
    which use case the route asks for, and therefore what that use case
    demands — is the real wiring. The canned reply source keeps the rest of the
    graph AWS-free for the same reason.
    """

    def __init__(self, *, role: ProcessRole, table: str, worker: str) -> None:
        self.store = FakeJobStore()
        self.app = create_app(
            _settings(role=role, table=table, worker=worker, canned=True)
        )
        self.app.dependency_overrides[get_job_store] = lambda: self.store

    def client(self) -> TestClient:
        return TestClient(self.app)


@pytest.fixture
def harness() -> Iterator[Harness]:
    built = Harness()
    yield built
    built.app.dependency_overrides.clear()


class TestAvailability:
    def test_health_advertises_both_transports_when_configured(self) -> None:
        with Harness().client() as client:
            assert client.get("/api/health").json()["transports"] == ["sse", "jobs"]

    def test_the_routes_are_absent_without_a_store(self) -> None:
        # Absent, not broken. The client is told as much by /api/health, and a
        # 404 is the honest answer if it asks anyway.
        with Harness(table="").client() as client:
            assert client.get("/api/health").json()["transports"] == ["sse"]
            blocked = client.post("/api/chat/jobs", json={"message": "x"})
            assert blocked.status_code == 404

    def test_the_streamed_route_still_works_alongside(self) -> None:
        with Harness().client() as client:
            assert client.post("/api/chat", json={"message": "x"}).status_code == 200


class TestSubmit:
    def test_accepts_a_message_and_returns_a_job_id(self, harness: Harness) -> None:
        with harness.client() as client:
            response = client.post("/api/chat/jobs", json={"message": "hello"})

        assert response.status_code == 202
        assert response.json()["job_id"] in harness.store.jobs

    def test_takes_the_same_body_as_the_streamed_route(self, harness: Harness) -> None:
        with harness.client() as client:
            response = client.post(
                "/api/chat/jobs",
                json={
                    "message": "and then?",
                    "history": [{"role": "user", "content": "hello"}],
                },
            )

        assert response.status_code == 202

    def test_rejects_a_blank_prompt(self, harness: Harness) -> None:
        with harness.client() as client:
            response = client.post("/api/chat/jobs", json={"message": "   "})

        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)

    def test_reports_a_refused_handoff_as_unavailable(self) -> None:
        harness = Harness(dispatcher=FakeJobDispatcher(JobDispatchError("no")))

        with harness.client() as client:
            response = client.post("/api/chat/jobs", json={"message": "hello"})

        assert response.status_code == 503


class TestRead:
    def test_returns_the_reply_from_the_cursor_on(self, harness: Harness) -> None:
        with harness.client() as client:
            job_id = client.post("/api/chat/jobs", json={"message": "x"}).json()[
                "job_id"
            ]
            response = client.get(f"/api/chat/jobs/{job_id}/segments/0")

        body = response.json()
        assert response.status_code == 200
        assert body["status"] == JobStatus.PENDING.value
        assert body["segments"] == []
        assert body["cursor"] == 0
        assert body["next_poll_ms"] > 0

    def test_an_unknown_job_is_not_found(self, harness: Harness) -> None:
        with harness.client() as client:
            response = client.get("/api/chat/jobs/nope/segments/0")

        assert response.status_code == 404

    def test_a_negative_cursor_is_rejected(self, harness: Harness) -> None:
        with harness.client() as client:
            response = client.get("/api/chat/jobs/abc/segments/-1")

        assert response.status_code == 422


class TestCancel:
    def test_stops_a_job(self, harness: Harness) -> None:
        with harness.client() as client:
            job_id = client.post("/api/chat/jobs", json={"message": "x"}).json()[
                "job_id"
            ]
            response = client.delete(f"/api/chat/jobs/{job_id}")

        assert response.status_code == 204
        assert harness.store.jobs[job_id].status is JobStatus.CANCELLED

    def test_is_idempotent_and_bodyless(self, harness: Harness) -> None:
        # Bodyless matters beyond tidiness: through the CDN this request is
        # signed by an origin access control, and a body would have to be
        # hashed by the browser for that signature to hold.
        with harness.client() as client:
            first = client.delete("/api/chat/jobs/never-existed")
            second = client.delete("/api/chat/jobs/never-existed")

        assert first.status_code == second.status_code == 204
        assert first.content == b""


class TestWorkerRoute:
    def test_is_absent_on_the_serving_deployment(self, harness: Harness) -> None:
        with harness.client() as client:
            response = client.post("/events", json={"job_id": "x", "request": {}})

        assert response.status_code == 404

    def test_runs_a_job_on_the_worker_deployment(self) -> None:
        harness = Harness(role=ProcessRole.WORKER, texts=("Hi ", "there"))

        with harness.client() as client:
            job_id = "j1"
            harness.store.jobs.clear()
            client_response = client.post(
                "/events",
                json={"job_id": job_id, "request": {"message": "hello"}},
            )

        # No such job, so the claim fails and the worker acknowledges rather
        # than erroring — which is exactly how a duplicate delivery is handled.
        assert client_response.status_code == 200
        assert client_response.json() == {"handled": True}

    def test_generates_the_reply_for_a_submitted_job(self) -> None:
        harness = Harness(role=ProcessRole.WORKER, texts=("Hi ", "there"))

        with harness.client() as client:
            job_id = client.post("/api/chat/jobs", json={"message": "x"}).json()[
                "job_id"
            ]
            client.post("/events", json={"job_id": job_id, "request": {"message": "x"}})
            body = client.get(f"/api/chat/jobs/{job_id}/segments/0").json()

        assert body["status"] == JobStatus.DONE.value
        assert "".join(body["segments"]) == "Hi there"

    def test_ignores_fields_it_does_not_know(self) -> None:
        # The payload crosses a deployment boundary updated by two separate
        # calls, so for a moment a new sender talks to an old receiver. Adding
        # a field has to be survivable rather than an outage.
        harness = Harness(role=ProcessRole.WORKER)

        with harness.client() as client:
            response = client.post(
                "/events",
                json={
                    "job_id": "j1",
                    "request": {"message": "x"},
                    "trace_id": "something-added-later",
                },
            )

        assert response.status_code == 200

    def test_a_malformed_event_is_rejected(self) -> None:
        harness = Harness(role=ProcessRole.WORKER)

        with harness.client() as client:
            response = client.post("/events", json={"job_id": "j1"})

        assert response.status_code == 422


class TestTheRealDependencyGraph:
    """What the deployment actually builds, with nothing overridden.

    Found in production: the worker's route asked for a use case that required
    a job *dispatcher*, but a worker has no worker-function name configured —
    correctly, since it dispatches nothing and holds no permission to invoke
    anything. So the graph could not be satisfied and every event failed with
    ``CHAT_WORKER_FUNCTION_NAME is not set`` before reaching any of the code
    these tests exercise. Every other test in this file overrode the very
    dependency that was broken.
    """

    def test_a_worker_needs_no_dispatcher_to_handle_an_event(self) -> None:
        # No worker function name, exactly as the worker is deployed. Reaching
        # the route at all is the assertion; a 500 here is the original bug.
        harness = RealGraphHarness(role=ProcessRole.WORKER, table="jobs", worker="")

        with harness.client() as client:
            response = client.post(
                "/events", json={"job_id": "j1", "request": {"message": "x"}}
            )

        assert response.status_code != 500
        # No such job, so the claim fails and the worker acknowledges — which
        # also proves it got as far as its own store.
        assert response.status_code == 200

    def test_a_worker_still_reports_the_asynchronous_transport(self) -> None:
        # `async_replies_enabled` must not require a dispatcher for a worker,
        # or the routes and the health report disagree with the deployment.
        harness = RealGraphHarness(role=ProcessRole.WORKER, table="jobs", worker="")

        with harness.client() as client:
            assert client.get("/api/health").json()["transports"] == ["sse", "jobs"]

    def test_the_api_half_does_need_a_dispatcher(self) -> None:
        # The complement: without somewhere to send work, the serving role
        # cannot offer the transport at all.
        harness = RealGraphHarness(role=ProcessRole.API, table="jobs", worker="")

        with harness.client() as client:
            assert client.get("/api/health").json()["transports"] == ["sse"]
            blocked = client.post("/api/chat/jobs", json={"message": "x"})
            assert blocked.status_code == 404
