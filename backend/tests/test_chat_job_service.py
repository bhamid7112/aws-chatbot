"""The asynchronous use case, driven entirely through its ports.

No AWS, no network, no moto. Every test here is about a *lifecycle* decision —
who may claim, what a rejected write means, when a job is presumed dead — which
is exactly the part that has no equivalent on the synchronous path and so has
no existing coverage to lean on.
"""

from __future__ import annotations

import pytest

from app.application.chat_job_service import ChatJobService
from app.application.chat_service import ChatService
from app.domain.entities import AppendResult, ChatRequest, JobStatus
from app.domain.errors import (
    InvalidPromptError,
    JobDispatchError,
    JobNotFoundError,
    ReplyGenerationError,
)
from app.domain.ports import ReplyGenerator
from tests.fakes import (
    FailingReplyGenerator,
    FakeJobDispatcher,
    FakeJobStore,
    FakeReplyGenerator,
)

REQUEST = ChatRequest(prompt="Hello?")


class Clock:
    """A clock a test can move, because deadlines cannot be tested otherwise."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(
    *,
    generator: ReplyGenerator | None = None,
    store: FakeJobStore | None = None,
    dispatcher: FakeJobDispatcher | None = None,
    clock: Clock | None = None,
    **kwargs: object,
) -> tuple[ChatJobService, FakeJobStore, FakeJobDispatcher, Clock]:
    resolved_store = store or FakeJobStore()
    resolved_dispatcher = dispatcher or FakeJobDispatcher()
    resolved_clock = clock or Clock()
    # Flush per chunk unless a test says otherwise, so that "what was written"
    # is legible rather than dependent on timing.
    kwargs.setdefault("flush_chars", 1)
    service = ChatJobService(
        ChatService(generator or FakeReplyGenerator()),
        resolved_store,
        resolved_dispatcher,
        clock=resolved_clock,
        **kwargs,  # type: ignore[arg-type]
    )
    return service, resolved_store, resolved_dispatcher, resolved_clock


class TestSubmit:
    async def test_records_the_job_before_handing_it_off(self) -> None:
        service, store, dispatcher, _ = build()

        job_id = await service.submit(REQUEST)

        assert store.jobs[job_id].status is JobStatus.PENDING
        assert dispatcher.dispatched == [(job_id, REQUEST)]

    async def test_rejects_a_bad_prompt_without_recording_anything(self) -> None:
        service, store, dispatcher, _ = build()

        with pytest.raises(InvalidPromptError):
            await service.submit(ChatRequest(prompt="   "))

        assert store.jobs == {}
        assert dispatcher.dispatched == []

    async def test_a_refused_handoff_is_reported_immediately(self) -> None:
        # Otherwise the client waits out the whole deadline for news we already
        # have, which is the difference between a second and eleven minutes.
        service, store, _, _ = build(
            dispatcher=FakeJobDispatcher(JobDispatchError("nope"))
        )

        with pytest.raises(JobDispatchError):
            await service.submit(REQUEST)

        [job] = store.jobs.values()
        assert job.status is JobStatus.FAILED
        assert job.error is not None

    async def test_the_deadline_covers_handoff_and_run(self) -> None:
        service, store, _, clock = build(deadline_seconds=120)

        job_id = await service.submit(REQUEST)

        assert store.jobs[job_id].deadline_at == int(clock.now) + 120


class TestRun:
    async def test_writes_the_reply_and_finishes(self) -> None:
        service, store, _, _ = build(generator=FakeReplyGenerator(["Hi ", "there"]))
        job_id = await service.submit(REQUEST)

        await service.run(job_id, REQUEST)

        job = store.jobs[job_id]
        assert job.status is JobStatus.DONE
        assert "".join(job.segments) == "Hi there"

    async def test_a_second_delivery_of_the_same_job_does_nothing(self) -> None:
        # Lambda's event queue can deliver the same event twice. The claim is
        # what stops that from answering — and billing — one prompt twice.
        generator = FakeReplyGenerator(["Hi"])
        service, store, _, _ = build(generator=generator)
        job_id = await service.submit(REQUEST)

        await service.run(job_id, REQUEST)
        await service.run(job_id, REQUEST)

        assert len(generator.requests) == 1
        assert store.jobs[job_id].segments == ("Hi",)

    async def test_batches_chunks_rather_than_writing_each_one(self) -> None:
        # Cost, not latency: a store charges for the whole record per write, so
        # appending per chunk makes a reply quadratic in its own length.
        service, store, _, _ = build(
            generator=FakeReplyGenerator(["a", "b", "c", "d"]),
            flush_chars=1_000,
            flush_interval_seconds=1_000.0,
        )
        job_id = await service.submit(REQUEST)

        await service.run(job_id, REQUEST)

        assert store.jobs[job_id].segments == ("abcd",)
        assert len(store.appends) == 1

    async def test_each_append_says_what_length_it_expects(self) -> None:
        service, store, _, _ = build(generator=FakeReplyGenerator(["a", "b"]))
        job_id = await service.submit(REQUEST)

        await service.run(job_id, REQUEST)

        assert [expected for _, _, expected in store.appends] == [0, 1]

    async def test_a_failed_reply_is_recorded_and_not_raised(self) -> None:
        # A reply that could not be produced is news for the person waiting,
        # not a defect in this process — so the invocation must succeed.
        service, store, _, _ = build(
            generator=FailingReplyGenerator(ReplyGenerationError("no model"))
        )
        job_id = await service.submit(REQUEST)

        await service.run(job_id, REQUEST)

        assert store.jobs[job_id].status is JobStatus.FAILED

    async def test_a_misbehaving_generator_is_still_only_a_failed_reply(
        self,
    ) -> None:
        # An adapter that raises something exotic is normalised by ChatService's
        # contract-enforcement point before it ever reaches here. So it is news
        # for the person waiting, not a defect — and the invocation succeeds.
        service, store, _, _ = build(
            generator=FailingReplyGenerator(RuntimeError("boom"))
        )
        job_id = await service.submit(REQUEST)

        await service.run(job_id, REQUEST)

        assert store.jobs[job_id].status is JobStatus.FAILED

    async def test_a_real_defect_is_recorded_and_re_raised(self) -> None:
        # Both halves matter: recorded so nobody waits out the deadline, and
        # re-raised so the invocation fails, is retried, and is alerted on.
        store = FakeJobStore()

        async def explode(job_id: str) -> None:
            raise RuntimeError("the store is broken in an unforeseen way")

        service, _, _, _ = build(store=store)
        job_id = await service.submit(REQUEST)
        store.finish = explode  # type: ignore[method-assign]

        with pytest.raises(RuntimeError):
            await service.run(job_id, REQUEST)

        assert store.jobs[job_id].status is JobStatus.FAILED

    async def test_stops_when_the_job_is_cancelled_mid_reply(self) -> None:
        # The rejected write *is* the cancellation signal — there is no second
        # read to notice it, which is what makes stopping free.
        store = FakeJobStore()
        service, _, _, _ = build(
            generator=FakeReplyGenerator(["a", "b", "c", "d"]), store=store
        )
        job_id = await service.submit(REQUEST)

        original = store.append

        async def cancel_after_first(
            job: str, text: str, *, expected: int
        ) -> AppendResult:
            result = await original(job, text, expected=expected)
            await store.cancel(job)
            return result

        store.append = cancel_after_first  # type: ignore[assignment]
        await service.run(job_id, REQUEST)

        job = store.jobs[job_id]
        assert job.status is JobStatus.CANCELLED
        # It stopped rather than generating to the end and being ignored.
        assert len(job.segments) == 1

    async def test_a_reply_that_grows_too_large_fails_cleanly(self) -> None:
        service, store, _, _ = build(
            generator=FakeReplyGenerator(["x" * 50, "y" * 50]),
            max_reply_chars=60,
        )
        job_id = await service.submit(REQUEST)

        await service.run(job_id, REQUEST)

        assert store.jobs[job_id].status is JobStatus.FAILED


class TestRetriedAppend:
    """The failure mode that a status-only condition would not catch."""

    async def test_re_sending_a_committed_append_does_not_duplicate_text(
        self,
    ) -> None:
        # The SDK retries a call whose response was lost coming back, so an
        # append can commit and then be sent again. Without the length
        # condition this duplicates text in the middle of a reply, on a network
        # blip, with nothing in the logs to explain it.
        store = FakeJobStore()
        service, _, _, _ = build(store=store)
        job_id = await service.submit(REQUEST)
        await store.claim(job_id)

        first = await store.append(job_id, "Hi", expected=0)
        retried = await store.append(job_id, "Hi", expected=0)

        assert first.cursor == 1
        assert retried.cursor == 1
        assert store.jobs[job_id].segments == ("Hi",)

    async def test_the_writer_resynchronises_and_carries_on(self) -> None:
        store = FakeJobStore()
        service, _, _, _ = build(store=store)
        job_id = await service.submit(REQUEST)
        await store.claim(job_id)
        await store.append(job_id, "Hi", expected=0)

        # A stale writer still believes the reply is empty.
        result = await store.append(job_id, "Hi", expected=0)

        assert service._continue_after(job_id, result) is True
        assert result.cursor == 1


class TestRead:
    async def test_returns_only_what_the_caller_has_not_seen(self) -> None:
        service, _, _, _ = build(
            generator=FakeReplyGenerator(["one ", "two ", "three"])
        )
        job_id = await service.submit(REQUEST)
        await service.run(job_id, REQUEST)

        progress = await service.read(job_id, 1)

        assert [chunk.text for chunk in progress.chunks] == ["two ", "three"]
        assert progress.cursor == 3
        assert progress.status is JobStatus.DONE

    async def test_the_first_poll_is_read_consistently(self) -> None:
        # It follows the create by milliseconds, and a stale miss there would
        # report a perfectly healthy job as gone.
        service, store, _, _ = build()
        job_id = await service.submit(REQUEST)

        await service.read(job_id, 0)
        await service.read(job_id, 1)

        assert store.reads == [(job_id, True), (job_id, False)]

    async def test_never_hands_back_a_cursor_behind_the_one_it_was_given(
        self,
    ) -> None:
        service, _, _, _ = build()
        job_id = await service.submit(REQUEST)

        progress = await service.read(job_id, 9)

        assert progress.cursor == 9
        assert progress.chunks == ()

    async def test_stops_asking_once_there_is_nothing_to_wait_for(self) -> None:
        service, _, _, _ = build()
        job_id = await service.submit(REQUEST)

        assert (await service.read(job_id, 0)).next_poll_ms > 0
        await service.run(job_id, REQUEST)
        assert (await service.read(job_id, 0)).next_poll_ms == 0

    async def test_an_unknown_job_is_not_found(self) -> None:
        service, _, _, _ = build()

        with pytest.raises(JobNotFoundError):
            await service.read("nope", 0)


class TestDeadline:
    async def test_a_job_past_its_deadline_reads_as_failed(self) -> None:
        # A worker killed outright cannot record its own death, so the read
        # path is what stops the client spinning forever.
        service, _, _, clock = build(deadline_seconds=60)
        job_id = await service.submit(REQUEST)

        clock.advance(61)
        progress = await service.read(job_id, 0)

        assert progress.status is JobStatus.FAILED
        assert progress.error is not None

    async def test_healing_does_not_write(self) -> None:
        # No write on the read path, and no sweeper process to own one.
        service, store, _, clock = build(deadline_seconds=60)
        job_id = await service.submit(REQUEST)

        clock.advance(61)
        await service.read(job_id, 0)

        assert store.jobs[job_id].status is JobStatus.PENDING

    async def test_a_finished_job_is_never_healed(self) -> None:
        service, _, _, clock = build()
        job_id = await service.submit(REQUEST)
        await service.run(job_id, REQUEST)

        clock.advance(100_000)

        assert (await service.read(job_id, 0)).status is JobStatus.DONE


class TestCancel:
    async def test_stops_a_running_job(self) -> None:
        service, store, _, _ = build()
        job_id = await service.submit(REQUEST)
        await store.claim(job_id)

        await service.cancel(job_id)

        assert store.jobs[job_id].status is JobStatus.CANCELLED

    async def test_will_not_overwrite_a_finished_reply(self) -> None:
        # A cancellation arriving just after the last flush must not erase an
        # answer a client is still reading.
        service, store, _, _ = build(generator=FakeReplyGenerator(["done"]))
        job_id = await service.submit(REQUEST)
        await service.run(job_id, REQUEST)

        await service.cancel(job_id)

        job = store.jobs[job_id]
        assert job.status is JobStatus.DONE
        assert job.segments == ("done",)

    async def test_an_unknown_job_is_not_an_error(self) -> None:
        service, _, _, _ = build()

        await service.cancel("nope")
