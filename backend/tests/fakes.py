"""Test doubles for the domain's ports.

That these are this short is the return on the architecture: the use cases
depend on small ports, so substituting them needs no mocking framework and no
patching.

The job store is the exception to "short", and deliberately so. Its conditional
writes are not an implementation detail the use case works around — they are
what makes the lifecycle safe, so a fake that accepted every write would
happily pass tests that the real adapter fails. It therefore reproduces the
conditions faithfully, and the DynamoDB adapter is tested separately against a
stub client to confirm it expresses those same conditions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

from app.domain.entities import (
    AppendOutcome,
    AppendResult,
    ChatJob,
    ChatRequest,
    JobStatus,
    ReplyChunk,
)
from app.domain.errors import JobNotFoundError, JobStoreError


class FakeReplyGenerator:
    """Yields the chunks it was given, and records what it was asked."""

    def __init__(self, texts: Sequence[str] = ("Hello ", "world")) -> None:
        self._texts = tuple(texts)
        self.requests: list[ChatRequest] = []

    async def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        self.requests.append(request)
        for text in self._texts:
            yield ReplyChunk(text=text)


class FailingReplyGenerator:
    """Raises after optionally emitting some chunks."""

    def __init__(
        self,
        error: Exception,
        *,
        texts_before_failure: Sequence[str] = (),
    ) -> None:
        self._error = error
        self._texts = tuple(texts_before_failure)

    async def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        for text in self._texts:
            yield ReplyChunk(text=text)
        raise self._error


class FakeJobStore:
    """An in-memory job store that enforces the same conditions as the real one.

    Records every call so a test can assert not just the outcome but the
    conditions that were asked for — ``expected`` on each append in particular,
    since that argument is the whole of the retry-safety story.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, ChatJob] = {}
        self.appends: list[tuple[str, str, int]] = []
        self.reads: list[tuple[str, bool]] = []

    async def create(
        self,
        job_id: str,
        *,
        created_at: int,
        deadline_at: int,
        expires_at: int,
    ) -> None:
        if job_id in self.jobs:
            raise JobStoreError(f"Job {job_id} already exists.")
        self.jobs[job_id] = ChatJob(
            job_id=job_id,
            status=JobStatus.PENDING,
            segments=(),
            created_at=created_at,
            deadline_at=deadline_at,
        )

    async def claim(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status is not JobStatus.PENDING:
            return False
        self.jobs[job_id] = replace(job, status=JobStatus.RUNNING)
        return True

    async def append(self, job_id: str, text: str, *, expected: int) -> AppendResult:
        self.appends.append((job_id, text, expected))
        job = self._require(job_id)

        if job.status is not JobStatus.RUNNING:
            return AppendResult(
                outcome=AppendOutcome.NOT_RUNNING,
                cursor=len(job.segments),
                status=job.status,
            )

        actual = len(job.segments)
        if actual > expected:
            # What a re-sent append looks like: it already landed.
            return AppendResult(
                outcome=AppendOutcome.SUPERSEDED, cursor=actual, status=job.status
            )
        if actual < expected:
            raise JobStoreError(
                f"Job {job_id} has {actual} segments but {expected} were expected."
            )

        self.jobs[job_id] = replace(job, segments=(*job.segments, text))
        return AppendResult(
            outcome=AppendOutcome.APPLIED,
            cursor=actual + 1,
            status=JobStatus.RUNNING,
        )

    async def finish(self, job_id: str) -> None:
        self._settle(job_id, JobStatus.DONE, None)

    async def fail(self, job_id: str, reason: str) -> None:
        self._settle(job_id, JobStatus.FAILED, reason)

    async def cancel(self, job_id: str) -> None:
        self._settle(job_id, JobStatus.CANCELLED, None)

    async def read(self, job_id: str, *, consistent: bool = False) -> ChatJob:
        self.reads.append((job_id, consistent))
        return self._require(job_id)

    def _settle(self, job_id: str, status: JobStatus, error: str | None) -> None:
        job = self.jobs.get(job_id)
        # Terminal transitions are idempotent: too late is not an error.
        if job is None or job.status.is_terminal:
            return
        self.jobs[job_id] = replace(job, status=status, error=error)

    def _require(self, job_id: str) -> ChatJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No job with id {job_id}.")
        return job


class FakeJobDispatcher:
    """Records hand-offs, and optionally refuses them."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.dispatched: list[tuple[str, ChatRequest]] = []

    async def dispatch(self, job_id: str, request: ChatRequest) -> None:
        if self._error is not None:
            raise self._error
        self.dispatched.append((job_id, request))
