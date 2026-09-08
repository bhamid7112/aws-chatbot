"""The asynchronous chat use case.

The synchronous path holds a reply in an HTTP response: if the connection
drops, the reply is gone, and nothing can stop the work once it has started.
This path puts the reply in a store instead, so it survives the connection —
and, because every write is conditional, so that stopping it is a single write
rather than an impossibility.

Note what is *not* here: any knowledge of how a reply is produced. ``run``
consumes :class:`~app.application.chat_service.ChatService` exactly as the
synchronous route does, so both paths share one reply generator, one set of
prompt rules and one error translation. The only thing this module adds is a
lifecycle.

Depends on ``domain`` and ``chat_service``, and nothing else — no boto3, no
HTTP, no knowledge of Lambda or DynamoDB.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from time import time

from app.application.chat_service import ChatService
from app.domain.entities import (
    AppendOutcome,
    AppendResult,
    ChatJob,
    ChatRequest,
    JobStatus,
    ReplyChunk,
    ReplyProgress,
)
from app.domain.errors import ChatError, ReplyGenerationError
from app.domain.ports import JobDispatcher, JobStore

logger = logging.getLogger(__name__)

DEFAULT_DEADLINE_SECONDS = 660
"""How long after creation a job is presumed abandoned.

Must comfortably exceed the whole hand-off plus the whole run: the time the
transport may spend retrying delivery, plus the longest a worker may run,
plus slack. Set too low, a slow-but-healthy job is reported as failed; set too
high, a genuinely lost job leaves a client waiting. It is deliberately one
number rather than three, because only the sum is ever used.
"""

DEFAULT_RETENTION_SECONDS = 3_600
"""How long a finished reply remains readable before it is expired."""

DEFAULT_FLUSH_INTERVAL_SECONDS = 0.4
DEFAULT_FLUSH_CHARS = 120
"""Flush triggers, whichever comes first.

Never flush per chunk. A store charges for the whole record on every write, so
appending N times to a growing reply costs O(N^2); batching is what keeps that
affordable, and it is the reason these two numbers exist at all.
"""

DEFAULT_POLL_INTERVAL_MS = 300
"""How long a client is told to wait before asking again.

Server-side so the cadence can be retuned without rebuilding the client.
"""

DEFAULT_MAX_REPLY_CHARS = 75_000
"""A ceiling on stored reply length, well inside any record-size limit.

Conservative on purpose: worst-case UTF-8 is four bytes per character, so even
a pathological reply of this length stays under a 400 KB record. Its job is to
turn "someone raised the output-token limit" into a clean failure rather than
an unhandled store error mid-reply.
"""

_DISPATCH_FAILURE_MESSAGE = "The assistant could not be reached."
_ABANDONED_MESSAGE = "The assistant stopped responding."
_UNEXPECTED_FAILURE_MESSAGE = "The assistant could not complete the reply."
_TOO_LONG_MESSAGE = "The reply grew too long to store."


class ChatJobService:
    """Submits, reads and cancels asynchronous replies. The client's half.

    Three methods for one caller: a browser. It never generates a reply, so it
    holds no reply generator beyond the prompt rules it validates against, and
    it never runs a job.

    Split from :class:`ChatJobRunner` because the two halves need almost
    disjoint collaborators, and a class that demanded both made the worker
    depend on a dispatcher it would never call (ISP). That was not theoretical:
    the worker has no dispatcher configured, and no permission to invoke
    anything, so requiring one stopped it from starting at all.

    Every collaborator arrives as a port (DIP). The clock and id generator are
    injected for the same reason — a test that cannot control time cannot test
    a deadline.
    """

    def __init__(
        self,
        chat_service: ChatService,
        store: JobStore,
        dispatcher: JobDispatcher,
        *,
        clock: Callable[[], float] = time,
        new_job_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
    ) -> None:
        self._chat_service = chat_service
        self._store = store
        self._dispatcher = dispatcher
        self._clock = clock
        self._new_job_id = new_job_id
        self._deadline_seconds = deadline_seconds
        self._retention_seconds = retention_seconds
        self._poll_interval_ms = poll_interval_ms

    async def submit(self, request: ChatRequest) -> str:
        """Record a job, hand it off, and return its id.

        The record is written **before** the hand-off, and the order is not
        incidental: a worker that starts first would find nothing to claim,
        conclude it was a duplicate, and drop the job silently.

        Raises:
            InvalidPromptError: The prompt breaks a rule, or the request is
                too large for the hand-off to carry.
            JobStoreError: The job could not be recorded.
            JobDispatchError: The job was recorded but not accepted.
        """
        self._chat_service.validate(request)

        job_id = self._new_job_id()
        now = int(self._clock())
        await self._store.create(
            job_id,
            created_at=now,
            deadline_at=now + self._deadline_seconds,
            expires_at=now + self._retention_seconds,
        )

        try:
            await self._dispatcher.dispatch(job_id, request)
        except ChatError:
            # We already know nothing will pick this up, so record it now
            # rather than leaving a client to wait out the deadline for news
            # we have in hand. The record stays, accurate, until it expires.
            logger.warning("job %s could not be dispatched", job_id)
            await _fail_quietly(self._store, job_id, _DISPATCH_FAILURE_MESSAGE)
            raise

        logger.info("job %s submitted", job_id)
        return job_id

    async def read(self, job_id: str, cursor: int) -> ReplyProgress:
        """Return everything generated since ``cursor``.

        Raises:
            JobNotFoundError: No such job, or it has expired.
            JobStoreError: The store could not be read.
        """
        cursor = max(cursor, 0)

        # A consistent read only where staleness would be indistinguishable
        # from absence: the first poll follows the create by milliseconds, and
        # a stale miss there would report a healthy job as gone. Later polls
        # can be stale harmlessly — see the cursor arithmetic below.
        job = await self._store.read(job_id, consistent=cursor == 0)

        status, error = _resolve(job, self._clock())
        return ReplyProgress(
            status=status,
            chunks=tuple(ReplyChunk(text=text) for text in job.segments[cursor:]),
            # Never hand back a cursor behind the one we were given. A stale
            # snapshot shorter than the caller's position yields no chunks and
            # no movement, and the next poll catches up.
            cursor=max(cursor, len(job.segments)),
            next_poll_ms=0 if status.is_terminal else self._poll_interval_ms,
            error=error,
        )

    async def cancel(self, job_id: str) -> None:
        """Stop a job, if it is still stoppable.

        Idempotent and silent about the outcome: cancelling a finished job, a
        cancelled job or an unknown id all leave the caller's intent satisfied,
        and none of them is a problem worth reporting.
        """
        await self._store.cancel(job_id)
        logger.info("job %s cancellation requested", job_id)


class ChatJobRunner:
    """Generates a reply for one job and writes it out as it arrives.

    The worker's half, and the counterpart of :class:`ChatJobService`. It
    reads nothing and dispatches nothing: every step it takes is a conditional
    write, and each one learns what it needs from whether the condition held.
    That is why the worker's execution role can be write-only on the table and
    hold no permission to invoke anything.
    """

    def __init__(
        self,
        chat_service: ChatService,
        store: JobStore,
        *,
        clock: Callable[[], float] = time,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        flush_chars: int = DEFAULT_FLUSH_CHARS,
        max_reply_chars: int = DEFAULT_MAX_REPLY_CHARS,
    ) -> None:
        if flush_chars < 1:
            raise ValueError("flush_chars must be at least 1")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        if max_reply_chars < 1:
            raise ValueError("max_reply_chars must be at least 1")

        self._chat_service = chat_service
        self._store = store
        self._clock = clock
        self._flush_interval_seconds = flush_interval_seconds
        self._flush_chars = flush_chars
        self._max_reply_chars = max_reply_chars

    async def run(self, job_id: str, request: ChatRequest) -> None:
        """Generate the reply for ``job_id``, writing it out as it arrives.

        Returns normally for every outcome a person could be told about,
        including failure — the caller's hand-off succeeded either way. Only a
        defect in this process propagates, so that it is reported as one.
        """
        if not await self._store.claim(job_id):
            # Someone already has it, or it is no longer runnable. Both mean
            # there is nothing to do and nothing wrong; returning normally is
            # what stops a duplicated hand-off from being retried forever.
            logger.info("job %s was not claimable; nothing to do", job_id)
            return

        logger.info("job %s claimed", job_id)
        try:
            await self._generate(job_id, request)
        except ChatError as exc:
            # A reply that could not be produced is not a defect in this
            # process — the same distinction the synchronous path draws.
            logger.info("job %s failed: %s", job_id, exc)
            await _fail_quietly(self._store, job_id, str(exc))
        except Exception:
            # A defect. Record it so nobody waits out a deadline for news we
            # already have, then re-raise so it is reported and alerted on.
            logger.exception("job %s failed unexpectedly", job_id)
            await _fail_quietly(self._store, job_id, _UNEXPECTED_FAILURE_MESSAGE)
            raise

    async def _generate(self, job_id: str, request: ChatRequest) -> None:
        # Called here rather than above because `stream_reply` validates
        # synchronously: an InvalidPromptError at this point means submit and
        # this worker disagree about the rules, which is a failed reply to
        # report rather than a crash to alert on.
        chunks = self._chat_service.stream_reply(request)

        buffer: list[str] = []
        pending = 0
        stored = 0
        cursor = 0
        flushed_at = self._clock()

        async for chunk in chunks:
            buffer.append(chunk.text)
            pending += len(chunk.text)
            stored += len(chunk.text)

            if stored > self._max_reply_chars:
                raise ReplyGenerationError(_TOO_LONG_MESSAGE)

            if not self._should_flush(pending, flushed_at):
                continue

            result = await self._store.append(job_id, "".join(buffer), expected=cursor)
            if not self._continue_after(job_id, result):
                return
            buffer, pending, cursor, flushed_at = [], 0, result.cursor, self._clock()

        if buffer:
            result = await self._store.append(job_id, "".join(buffer), expected=cursor)
            if not self._continue_after(job_id, result):
                return

        await self._store.finish(job_id)
        logger.info("job %s done", job_id)

    # ---------------------------------------------------------------- helpers

    def _should_flush(self, pending: int, flushed_at: float) -> bool:
        if pending >= self._flush_chars:
            return True
        return self._clock() - flushed_at >= self._flush_interval_seconds

    def _continue_after(self, job_id: str, result: AppendResult) -> bool:
        """Decide whether to keep generating, given what the store just said.

        This is the whole of cancellation: a stopped job rejects the write, and
        the rejection is how the news arrives. No extra read, no polling, and
        the work ends within one flush of the request.
        """
        if result.outcome is AppendOutcome.NOT_RUNNING:
            logger.info("job %s is %s; stopping", job_id, result.status)
            return False

        if result.outcome is AppendOutcome.SUPERSEDED:
            # The write landed and its acknowledgement did not; the SDK re-sent
            # it. Applying it again would duplicate text in the middle of a
            # reply, which is exactly what the store refused to let us do.
            logger.warning(
                "job %s append was already applied; resyncing cursor to %d",
                job_id,
                result.cursor,
            )

        return True


def _resolve(job: ChatJob, now: float) -> tuple[JobStatus, str | None]:
    """Report a job past its deadline as failed, **without writing.**

    A worker killed outright cannot record its own death, so somebody has to
    notice. Doing it on read costs nothing, needs no scheduled sweep, and keeps
    the read path free of writes; the record itself is left alone and expires
    on its own.
    """
    if job.status.is_terminal:
        return job.status, job.error

    if now > job.deadline_at:
        logger.warning("job %s passed its deadline with no result", job.job_id)
        return JobStatus.FAILED, _ABANDONED_MESSAGE

    return job.status, None


async def _fail_quietly(store: JobStore, job_id: str, reason: str) -> None:
    """Record a failure without letting that attempt mask the real one.

    A module function because both halves need it: the client's half when a
    hand-off is refused, the worker's when a reply cannot be produced.
    """
    try:
        await store.fail(job_id, reason)
    except ChatError:
        logger.exception("could not record the failure of job %s", job_id)
