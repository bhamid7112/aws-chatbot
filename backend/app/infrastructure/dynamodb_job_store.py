"""A :class:`JobStore` backed by one DynamoDB table.

Everything vendor-specific stops here: boto3, botocore's exception hierarchy,
the typed attribute maps of the low-level API, and the condition expressions
that make the lifecycle safe. ``domain`` and ``application`` never learn that a
database is involved (DIP).

Two things about this adapter are load-bearing rather than incidental, and both
are consequences of the SDK rather than of DynamoDB:

* **Every write is conditional**, and one of them is conditional on the reply's
  current length. Appending is not idempotent, and botocore retries a call
  whose response was lost coming back — so an append can commit and then be
  re-sent. Without the length condition that duplicates text in the middle of a
  reply, non-deterministically, on a network blip.
* **A failed condition is an answer, not an error.** It is how a worker learns
  it was cancelled, how a duplicate hand-off learns it lost the race, and how a
  re-sent append learns it already landed. Callers get an outcome; nobody sees a
  ``ConditionalCheckFailedException``.

The attribute is ``#status`` everywhere, never ``status``: ``STATUS`` is a
DynamoDB reserved word, and using it unaliased fails the request outright.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar, cast

import anyio.to_thread
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.domain.entities import (
    AppendOutcome,
    AppendResult,
    ChatJob,
    JobStatus,
)
from app.domain.errors import JobNotFoundError, JobStoreError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
# A store call sits between two tokens of a reply, so it is either fast or it is
# a problem. Retries are cheap and the calls are all conditional, which is what
# makes retrying them safe.
DEFAULT_MAX_ATTEMPTS = 4

_CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
_UNREACHABLE_MESSAGE = "The job store could not be reached."

_KEY = "job_id"
_STATUS = "#status"
_ERROR = "#error"
_NAMES = {_STATUS: "status", _ERROR: "error"}

_T = TypeVar("_T")


def build_dynamodb_client(
    region: str | None = None,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> DynamoDBClient:
    """Build the client the store talks to.

    Separate from the class for the same reason as the Bedrock client factory:
    constructing a real client resolves credentials, and no unit test should
    need to.

    There is no setting for the region, and its absence is deliberate. Unlike
    Bedrock — which is called over the network and can legitimately live
    elsewhere — the table is always in the same region as the process reading
    it. ``None`` lets boto3's own chain answer, which on Lambda means the
    ``AWS_REGION`` the runtime sets for us. Adding a setting would create a
    value that can be wrong.
    """
    return boto3.client(
        "dynamodb",
        region_name=region,
        config=Config(
            retries={"max_attempts": max_attempts, "mode": "standard"},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        ),
    )


class DynamoDbJobStore:
    """Holds asynchronous replies in a single DynamoDB table.

    One item per job, keyed by job id. The reply is a list of strings appended
    in place, so its length doubles as the client's cursor and no separate
    counter can drift from it.
    """

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table_name must not be blank")
        self._client = client
        self._table_name = table_name

    async def create(
        self,
        job_id: str,
        *,
        created_at: int,
        deadline_at: int,
        expires_at: int,
    ) -> None:
        """Write a new pending job with an empty reply.

        ``segments`` is created as an empty list rather than left absent:
        ``list_append`` against a missing attribute is a validation error, so
        the first flush of every job would otherwise fail.
        """
        call = partial(
            self._client.put_item,
            TableName=self._table_name,
            Item={
                _KEY: {"S": job_id},
                "status": {"S": JobStatus.PENDING.value},
                "segments": {"L": []},
                "created_at": {"N": str(created_at)},
                "deadline_at": {"N": str(deadline_at)},
                "expires_at": {"N": str(expires_at)},
            },
            ConditionExpression=f"attribute_not_exists({_KEY})",
        )
        try:
            await self._run(call, "put_item")
        except ClientError as exc:
            if _conditional_check_failed(exc):
                # Ids are generated, so this is not a collision to retry around
                # — it means something reused one.
                raise JobStoreError(f"Job {job_id} already exists.") from exc
            raise

    async def claim(self, job_id: str) -> bool:
        """Move a pending job to running. False if it was not pending.

        Also false for a job that does not exist, since the condition cannot
        hold on a missing attribute — which is the right answer either way.
        """
        call = partial(
            self._client.update_item,
            TableName=self._table_name,
            Key={_KEY: {"S": job_id}},
            UpdateExpression=f"SET {_STATUS} = :running",
            ConditionExpression=f"{_STATUS} = :pending",
            ExpressionAttributeNames=_NAMES,
            ExpressionAttributeValues={
                ":running": {"S": JobStatus.RUNNING.value},
                ":pending": {"S": JobStatus.PENDING.value},
            },
        )
        try:
            await self._run(call, "update_item (claim)")
        except ClientError as exc:
            if _conditional_check_failed(exc):
                return False
            raise
        return True

    async def append(self, job_id: str, text: str, *, expected: int) -> AppendResult:
        """Append one segment, if the reply is still ``expected`` segments long.

        The success cursor is computed rather than read back: exactly one
        element was appended to a list of known length, so asking DynamoDB to
        return the whole grown list on every flush would buy nothing and cost
        the reply's size in bandwidth each time.
        """
        call = partial(
            self._client.update_item,
            TableName=self._table_name,
            Key={_KEY: {"S": job_id}},
            UpdateExpression="SET segments = list_append(segments, :chunk)",
            ConditionExpression=(
                f"{_STATUS} = :running AND size(segments) = :expected"
            ),
            ExpressionAttributeNames=_NAMES,
            ExpressionAttributeValues={
                ":chunk": {"L": [{"S": text}]},
                ":running": {"S": JobStatus.RUNNING.value},
                ":expected": {"N": str(expected)},
            },
            ReturnValues="NONE",
            # The one call has to say *which* half of the condition failed:
            # a cancellation and a re-sent append are the same exception and
            # opposite instructions. Without this it takes a second read to
            # tell them apart, and the answer could change in between.
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
        try:
            await self._run(call, "update_item (append)")
        except ClientError as exc:
            if _conditional_check_failed(exc):
                return self._explain_rejection(job_id, exc, expected)
            raise

        return AppendResult(
            outcome=AppendOutcome.APPLIED,
            cursor=expected + 1,
            status=JobStatus.RUNNING,
        )

    async def finish(self, job_id: str) -> None:
        """Mark a running job done. A no-op if it is no longer running."""
        await self._settle(
            job_id,
            "SET " + _STATUS + " = :terminal",
            {":terminal": {"S": JobStatus.DONE.value}},
            "finish",
        )

    async def fail(self, job_id: str, reason: str) -> None:
        """Mark a job failed, recording why.

        Conditional on *not being terminal* rather than on being running, so
        that a job whose hand-off failed before anything claimed it can still
        be reported — that job is still pending, and always will be.
        """
        await self._settle(
            job_id,
            f"SET {_STATUS} = :terminal, {_ERROR} = :reason",
            {
                ":terminal": {"S": JobStatus.FAILED.value},
                ":reason": {"S": reason},
            },
            "fail",
        )

    async def cancel(self, job_id: str) -> None:
        """Stop a pending or running job.

        The condition is what protects a completed reply: a cancellation
        arriving just after the last flush must not overwrite ``done`` and
        erase the answer from under a client that is still reading it.
        """
        await self._settle(
            job_id,
            "SET " + _STATUS + " = :terminal",
            {":terminal": {"S": JobStatus.CANCELLED.value}},
            "cancel",
        )

    async def read(self, job_id: str, *, consistent: bool = False) -> ChatJob:
        """Return the job as stored.

        Raises:
            JobNotFoundError: No such job, or it has expired.
        """
        call = partial(
            self._client.get_item,
            TableName=self._table_name,
            Key={_KEY: {"S": job_id}},
            ConsistentRead=consistent,
        )
        response = await self._run(call, "get_item")

        item = response.get("Item")
        if not item:
            raise JobNotFoundError(f"No job with id {job_id}.")
        return _to_job(job_id, item)

    # ---------------------------------------------------------------- private

    async def _settle(
        self,
        job_id: str,
        update: str,
        values: dict[str, Any],
        what: str,
    ) -> None:
        """Apply a terminal transition, treating "too late" as success.

        Every terminal write is idempotent by design: if the job is already
        terminal, the caller's intent has been satisfied by whoever got there
        first, and reporting that as an error would only invite a caller to
        retry something that can never succeed.
        """
        call = partial(
            self._client.update_item,
            TableName=self._table_name,
            Key={_KEY: {"S": job_id}},
            UpdateExpression=update,
            ConditionExpression=f"{_STATUS} IN (:pending, :running)",
            ExpressionAttributeNames=_NAMES,
            ExpressionAttributeValues={
                **values,
                ":pending": {"S": JobStatus.PENDING.value},
                ":running": {"S": JobStatus.RUNNING.value},
            },
        )
        try:
            await self._run(call, f"update_item ({what})")
        except ClientError as exc:
            if _conditional_check_failed(exc):
                logger.info(
                    "job %s was already settled; %s had no effect", job_id, what
                )
                return
            raise

    def _explain_rejection(
        self, job_id: str, exc: ClientError, expected: int
    ) -> AppendResult:
        """Turn a refused append into the reason it was refused."""
        # ``Item`` is present only because the call asked for it on failure, so
        # it is not part of botocore's declared error shape.
        item = cast("Mapping[str, Any]", exc.response).get("Item")
        if not isinstance(item, dict) or not item:
            # The condition cannot hold because there is nothing to hold it
            # against. Someone removed the job while it was running.
            raise JobNotFoundError(f"No job with id {job_id}.") from exc

        job = _to_job(job_id, item)

        if job.status is not JobStatus.RUNNING:
            return AppendResult(
                outcome=AppendOutcome.NOT_RUNNING,
                cursor=len(job.segments),
                status=job.status,
            )

        actual = len(job.segments)
        if actual > expected:
            # Our own write landed and its acknowledgement did not, so the SDK
            # sent it again. Refusing it is the point.
            return AppendResult(
                outcome=AppendOutcome.SUPERSEDED,
                cursor=actual,
                status=job.status,
            )

        # Running, and shorter than we expected: append-only growth with a
        # single writer makes this impossible, so it is a defect rather than a
        # state to recover from.
        raise JobStoreError(
            f"Job {job_id} has {actual} segments but {expected} were expected."
        ) from exc

    async def _run(self, call: Callable[[], _T], what: str) -> _T:
        """Run a blocking boto3 call off the event loop, translating failures.

        A conditional-check failure is re-raised untouched: it is the caller's
        to interpret, and only the caller knows what it means. Everything else
        becomes a :class:`JobStoreError` with the cause logged here rather than
        carried outwards, since botocore messages can name tables and accounts.
        """
        try:
            return await anyio.to_thread.run_sync(call)
        except ClientError as exc:
            if _conditional_check_failed(exc):
                raise
            logger.exception("DynamoDB rejected %s", what)
            raise JobStoreError(_UNREACHABLE_MESSAGE) from exc
        except BotoCoreError as exc:
            logger.exception("DynamoDB %s did not complete", what)
            raise JobStoreError(_UNREACHABLE_MESSAGE) from exc


def _conditional_check_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == _CONDITIONAL_CHECK_FAILED


def _to_job(job_id: str, item: Mapping[str, Any]) -> ChatJob:
    """Read an item into a domain entity, or say the item is unusable."""
    try:
        return ChatJob(
            job_id=job_id,
            status=JobStatus(item["status"]["S"]),
            segments=tuple(
                element["S"] for element in item.get("segments", {}).get("L", [])
            ),
            created_at=int(item["created_at"]["N"]),
            deadline_at=int(item["deadline_at"]["N"]),
            error=item.get("error", {}).get("S"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        # A shape we do not recognise is a deployment problem — an older writer,
        # a renamed attribute — and pretending to read it would be worse than
        # saying so.
        logger.error("job %s is stored in an unreadable shape", job_id)
        raise JobStoreError(f"Job {job_id} could not be read.") from exc
