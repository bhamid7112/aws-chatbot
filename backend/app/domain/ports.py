"""Ports: the interfaces the inner layers depend on.

Structural (``Protocol``) rather than nominal (``ABC``) on purpose — an adapter
does not import this module to satisfy it, so the dependency arrow stays pointed
inwards and test fakes stay to three lines.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.domain.entities import AppendResult, ChatJob, ChatRequest, ReplyChunk


@runtime_checkable
class ReplyGenerator(Protocol):
    """Produces a reply to a :class:`ChatRequest`, streamed in fragments.

    One method, because consumers use exactly one (ISP). Adding a second reply
    source means adding a new implementation, never editing the callers (OCP).

    Implementations are substitutable only if they all honour this contract
    (LSP) — a caller must never need to know which one it received:

    * **Yields at least one chunk** whose ``text`` is non-empty. A generator
      that produces nothing has failed and must say so by raising.
    * **Terminates.** The stream is finite; it is not a subscription.
    * **Raises only** :class:`~app.domain.errors.ReplyGenerationError`. Any
      transport, vendor or parsing failure is translated by the adapter that
      owns it, since that adapter is the only code that understands it.
    * **Does not mutate** the request it is given.

    Note the signature is a plain ``def`` returning an ``AsyncIterator``, not an
    ``async def``: that is the shape an ``async def`` generator function has, so
    implementations can simply ``yield``.
    """

    def generate(self, request: ChatRequest) -> AsyncIterator[ReplyChunk]:
        """Stream the reply to ``request``."""
        ...


@runtime_checkable
class JobStore(Protocol):
    """Durable state for one asynchronous reply, addressed by job id.

    Every mutating method is **conditional** — the store, not the caller, is
    what makes the lifecycle safe. Three properties the whole design leans on,
    and which an implementation must provide or substitute unsafely (LSP):

    * **Exactly one writer wins the claim.** ``claim`` succeeds for the first
      caller and fails for every later one, so a duplicated hand-off can never
      produce two replies or two charges for one prompt.
    * **An append is safe to re-send.** ``append`` takes the segment count the
      caller believes it is extending. Re-sending a committed append must be
      reported as :attr:`~app.domain.entities.AppendOutcome.SUPERSEDED`, never
      applied twice.
    * **A cancelled or finished job rejects further writes**, reported as
      :attr:`~app.domain.entities.AppendOutcome.NOT_RUNNING`. That is how a
      caller learns to stop working — no separate poll, no extra read.

    Terminal transitions (``finish``, ``fail``, ``cancel``) are **idempotent**:
    applying one that no longer holds is a no-op, not an error, because the
    caller's intent has already been satisfied by whoever got there first.

    Raises:
        JobStoreError: The store could not be reached or answered unusably.
    """

    async def create(
        self,
        job_id: str,
        *,
        created_at: int,
        deadline_at: int,
        expires_at: int,
    ) -> None:
        """Record a new job as pending, with no segments.

        ``deadline_at`` is supplied here rather than on ``claim`` on purpose: a
        job whose hand-off is lost is never claimed, and a job with no deadline
        can never be recognised as abandoned.
        """
        ...

    async def claim(self, job_id: str) -> bool:
        """Take ownership of a pending job. False if someone already has."""
        ...

    async def append(self, job_id: str, text: str, *, expected: int) -> AppendResult:
        """Extend the reply, but only if it is still ``expected`` segments long."""
        ...

    async def finish(self, job_id: str) -> None:
        """Mark a running job complete."""
        ...

    async def fail(self, job_id: str, reason: str) -> None:
        """Mark a running job failed.

        ``reason`` is shown to a person, so it must be a domain message and
        never adapter detail — vendor error text can name models, accounts and
        endpoints.
        """
        ...

    async def cancel(self, job_id: str) -> None:
        """Stop a pending or running job. A terminal job is left alone."""
        ...

    async def read(self, job_id: str, *, consistent: bool = False) -> ChatJob:
        """Return the job as it stands.

        ``consistent`` asks for a read that cannot miss a recent write, at
        twice the cost. Callers use it only where staleness would be
        indistinguishable from absence.

        Raises:
            JobNotFoundError: No job with that id.
        """
        ...


@runtime_checkable
class JobDispatcher(Protocol):
    """Hands a job to whatever will actually generate the reply.

    Deliberately one method with no result: the dispatcher promises only that
    the work has been *accepted*, never that it has started or will succeed.
    Everything a caller learns afterwards it learns from the
    :class:`JobStore`, which is what keeps the two concerns separable and lets
    the transport change without the use case noticing.

    Raises:
        RequestTooLargeError: The request exceeds what this transport carries.
        JobDispatchError: The hand-off was refused or could not be attempted.
    """

    async def dispatch(self, job_id: str, request: ChatRequest) -> None:
        """Arrange for ``request`` to be answered under ``job_id``."""
        ...
