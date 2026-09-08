"""The DynamoDB adapter, against a stub client.

No credentials, no network, no moto — the adapter takes its client as an
argument so the interesting behaviour can be tested without AWS.

Most of what is asserted here is the *condition expressions*. That is the point:
the use case is only safe because these writes refuse to apply under the wrong
circumstances, and a fake store that merely behaves correctly would not catch an
adapter that forgot to ask.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from app.domain.entities import AppendOutcome, JobStatus
from app.domain.errors import JobNotFoundError, JobStoreError
from app.domain.ports import JobStore
from app.infrastructure.dynamodb_job_store import DynamoDbJobStore

JOB = "j1"
TABLE = "jobs"


def _item(
    status: str = "running",
    segments: tuple[str, ...] = (),
    error: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "job_id": {"S": JOB},
        "status": {"S": status},
        "segments": {"L": [{"S": text} for text in segments]},
        "created_at": {"N": "100"},
        "deadline_at": {"N": "200"},
    }
    if error is not None:
        item["error"] = {"S": error}
    return item


def _conditional_failure(item: dict[str, Any] | None = None) -> ClientError:
    response: dict[str, Any] = {
        "Error": {
            "Code": "ConditionalCheckFailedException",
            "Message": "The conditional request failed",
        }
    }
    if item is not None:
        # Present only because the adapter asks for it on a failed condition, so
        # it is not part of botocore's declared error shape.
        response["Item"] = item
    return ClientError(cast("Any", response), "UpdateItem")


def _other_failure() -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                # Real botocore messages name accounts and table ARNs, which is
                # why the adapter must not pass them outwards.
                "Message": "arn:aws:dynamodb:us-east-2:123456789012:table/jobs",
            }
        },
        "UpdateItem",
    )


class StubDynamoDb:
    """Records every call, and can be primed to fail or to return an item.

    It also **rejects a request DynamoDB would reject**, which is the whole
    reason it is not a bare recorder. A stub that accepts anything is a stub
    that passes tests the service fails on first contact: an unused
    ``ExpressionAttributeNames`` entry is a ``ValidationException`` in
    production and was silently fine here until this check existed.
    """

    def __init__(
        self,
        *,
        item: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._item = item
        self._error = error

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("put_item", kwargs)

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("update_item", kwargs)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return self._respond("get_item", kwargs)

    def _respond(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        _reject_unused_placeholders(kwargs)
        if self._error is not None:
            raise self._error
        return {} if self._item is None else {"Item": self._item}

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1][1]


def _reject_unused_placeholders(kwargs: dict[str, Any]) -> None:
    """Fail the way DynamoDB fails on a placeholder no expression mentions.

    Both maps are checked, because DynamoDB rejects an unused entry in either
    with the same class of ValidationException.
    """
    expressions = " ".join(
        str(kwargs.get(key, ""))
        for key in ("UpdateExpression", "ConditionExpression", "ProjectionExpression")
    )

    for field, placeholders in (
        ("ExpressionAttributeNames", kwargs.get("ExpressionAttributeNames") or {}),
        ("ExpressionAttributeValues", kwargs.get("ExpressionAttributeValues") or {}),
    ):
        unused = sorted(p for p in placeholders if p not in expressions)
        if unused:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": (
                            f"Value provided in {field} unused in expressions: "
                            f"keys: {{{', '.join(unused)}}}"
                        ),
                    }
                },
                "UpdateItem",
            )


def _store(client: Any) -> DynamoDbJobStore:
    return DynamoDbJobStore(client, TABLE)


def test_satisfies_the_port() -> None:
    assert isinstance(_store(StubDynamoDb()), JobStore)


def test_rejects_a_blank_table_name() -> None:
    with pytest.raises(ValueError):
        DynamoDbJobStore(cast("Any", StubDynamoDb()), "  ")


class TestReservedWord:
    """``STATUS`` is a DynamoDB reserved word.

    Used unaliased it fails the whole request, so every expression that mentions
    it has to go through ``ExpressionAttributeNames``. This is cheap to get
    wrong and fails on the very first call against a real table.
    """

    @pytest.mark.parametrize(
        "operation",
        ["claim", "append", "finish", "fail", "cancel"],
    )
    async def test_status_is_always_aliased(self, operation: str) -> None:
        client = StubDynamoDb()
        store = _store(client)

        if operation == "claim":
            await store.claim(JOB)
        elif operation == "append":
            await store.append(JOB, "x", expected=0)
        elif operation == "finish":
            await store.finish(JOB)
        elif operation == "fail":
            await store.fail(JOB, "why")
        else:
            await store.cancel(JOB)

        names = client.last["ExpressionAttributeNames"]
        assert names["#status"] == "status"
        assert "status" not in client.last["ConditionExpression"].replace("#status", "")


class TestPlaceholdersAreDeclaredExactly:
    """Every declared placeholder must be used, and only the used ones declared.

    Found in production, not here: a single shared map of every alias was
    passed to all five write operations, and DynamoDB rejected four of them with
    "Value provided in ExpressionAttributeNames unused in expressions: keys:
    {#error}". Claim, append, finish and cancel all failed; only ``fail``
    happened to use every alias it declared. Nothing caught it because the stub
    recorded the call instead of judging it.
    """

    @pytest.mark.parametrize(
        "operation", ["claim", "append", "finish", "fail", "cancel"]
    )
    async def test_no_operation_declares_an_unused_placeholder(
        self, operation: str
    ) -> None:
        # The stub raises the same ValidationException DynamoDB does, so simply
        # completing is the assertion.
        client = StubDynamoDb()
        store = _store(client)

        if operation == "claim":
            await store.claim(JOB)
        elif operation == "append":
            await store.append(JOB, "x", expected=0)
        elif operation == "finish":
            await store.finish(JOB)
        elif operation == "fail":
            await store.fail(JOB, "why")
        else:
            await store.cancel(JOB)

    @pytest.mark.parametrize("operation", ["claim", "append", "finish", "cancel"])
    async def test_only_fail_declares_the_error_alias(self, operation: str) -> None:
        # Because only `fail` writes that attribute. This is the specific
        # mistake that broke the deployment.
        client = StubDynamoDb()
        store = _store(client)

        if operation == "claim":
            await store.claim(JOB)
        elif operation == "append":
            await store.append(JOB, "x", expected=0)
        elif operation == "finish":
            await store.finish(JOB)
        else:
            await store.cancel(JOB)

        assert "#error" not in client.last["ExpressionAttributeNames"]

    async def test_the_stub_would_have_caught_it(self) -> None:
        # Guards the guard: if this stops raising, the check above proves
        # nothing and the next unused alias reaches production.
        with pytest.raises(ClientError) as raised:
            _reject_unused_placeholders(
                {
                    "UpdateExpression": "SET #status = :s",
                    "ExpressionAttributeNames": {
                        "#status": "status",
                        "#error": "error",
                    },
                    "ExpressionAttributeValues": {":s": {"S": "done"}},
                }
            )

        assert "unused in expressions" in str(raised.value)


class TestCreate:
    async def test_writes_a_pending_job_with_an_empty_reply(self) -> None:
        # The empty list is not decoration: list_append against a missing
        # attribute is a validation error, so the first flush would fail.
        client = StubDynamoDb()

        await _store(client).create(JOB, created_at=10, deadline_at=20, expires_at=30)

        item = client.last["Item"]
        assert item["status"] == {"S": "pending"}
        assert item["segments"] == {"L": []}
        assert item["expires_at"] == {"N": "30"}
        assert item["deadline_at"] == {"N": "20"}

    async def test_refuses_to_overwrite_an_existing_job(self) -> None:
        client = StubDynamoDb()

        await _store(client).create(JOB, created_at=1, deadline_at=2, expires_at=3)

        assert "attribute_not_exists(job_id)" in client.last["ConditionExpression"]

    async def test_a_collision_is_reported(self) -> None:
        client = StubDynamoDb(error=_conditional_failure())

        with pytest.raises(JobStoreError):
            await _store(client).create(JOB, created_at=1, deadline_at=2, expires_at=3)


class TestClaim:
    async def test_claims_a_pending_job(self) -> None:
        client = StubDynamoDb()

        assert await _store(client).claim(JOB) is True
        assert client.last["ConditionExpression"] == "#status = :pending"

    async def test_a_job_that_is_not_pending_cannot_be_claimed(self) -> None:
        # This is what makes a duplicated hand-off harmless, and is the reason
        # retries can be left enabled at all.
        client = StubDynamoDb(error=_conditional_failure())

        assert await _store(client).claim(JOB) is False


class TestAppend:
    async def test_asks_for_the_length_it_expects(self) -> None:
        client = StubDynamoDb()

        await _store(client).append(JOB, "hello", expected=3)

        condition = client.last["ConditionExpression"]
        assert "size(segments) = :expected" in condition
        assert "#status = :running" in condition
        assert client.last["ExpressionAttributeValues"][":expected"] == {"N": "3"}

    async def test_asks_for_the_old_item_when_refused(self) -> None:
        # One call has to distinguish a cancellation from a re-sent append.
        # Without this it takes a second read, and the answer could change in
        # between.
        client = StubDynamoDb()

        await _store(client).append(JOB, "x", expected=0)

        assert client.last["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"

    async def test_a_successful_append_advances_by_one(self) -> None:
        client = StubDynamoDb()

        result = await _store(client).append(JOB, "x", expected=4)

        assert result.outcome is AppendOutcome.APPLIED
        assert result.cursor == 5

    async def test_a_cancelled_job_stops_the_writer(self) -> None:
        client = StubDynamoDb(
            error=_conditional_failure(_item(status="cancelled", segments=("a",)))
        )

        result = await _store(client).append(JOB, "b", expected=1)

        assert result.outcome is AppendOutcome.NOT_RUNNING
        assert result.status is JobStatus.CANCELLED

    async def test_a_re_sent_append_is_reported_as_already_applied(self) -> None:
        # The reply is longer than this writer believes, which means its own
        # earlier write landed and only the acknowledgement was lost.
        client = StubDynamoDb(error=_conditional_failure(_item(segments=("a", "b"))))

        result = await _store(client).append(JOB, "b", expected=1)

        assert result.outcome is AppendOutcome.SUPERSEDED
        assert result.cursor == 2

    async def test_a_shorter_reply_than_expected_is_a_defect(self) -> None:
        # Append-only growth with a single writer makes this impossible, so it
        # is not a state to recover from.
        client = StubDynamoDb(error=_conditional_failure(_item(segments=("a",))))

        with pytest.raises(JobStoreError):
            await _store(client).append(JOB, "z", expected=5)

    async def test_a_deleted_job_is_not_found(self) -> None:
        client = StubDynamoDb(error=_conditional_failure())

        with pytest.raises(JobNotFoundError):
            await _store(client).append(JOB, "z", expected=0)


class TestTerminalTransitions:
    @pytest.mark.parametrize("operation", ["finish", "fail", "cancel"])
    async def test_only_applies_to_a_job_that_is_not_terminal(
        self, operation: str
    ) -> None:
        client = StubDynamoDb()
        store = _store(client)

        if operation == "finish":
            await store.finish(JOB)
        elif operation == "fail":
            await store.fail(JOB, "why")
        else:
            await store.cancel(JOB)

        assert client.last["ConditionExpression"] == "#status IN (:pending, :running)"

    @pytest.mark.parametrize("operation", ["finish", "fail", "cancel"])
    async def test_being_too_late_is_not_an_error(self, operation: str) -> None:
        # Idempotent by design: whoever got there first has already satisfied
        # the caller's intent, and reporting an error would invite a retry of
        # something that can never succeed.
        client = StubDynamoDb(error=_conditional_failure())
        store = _store(client)

        if operation == "finish":
            await store.finish(JOB)
        elif operation == "fail":
            await store.fail(JOB, "why")
        else:
            await store.cancel(JOB)

    async def test_failing_records_the_reason(self) -> None:
        client = StubDynamoDb()

        await _store(client).fail(JOB, "The model could not be reached.")

        values = client.last["ExpressionAttributeValues"]
        assert values[":reason"] == {"S": "The model could not be reached."}
        assert client.last["ExpressionAttributeNames"]["#error"] == "error"


class TestRead:
    async def test_reads_a_job(self) -> None:
        client = StubDynamoDb(
            item=_item(status="failed", segments=("a", "b"), error="nope")
        )

        job = await _store(client).read(JOB)

        assert job.status is JobStatus.FAILED
        assert job.segments == ("a", "b")
        assert job.error == "nope"
        assert job.created_at == 100
        assert job.deadline_at == 200

    async def test_asks_for_a_consistent_read_when_told_to(self) -> None:
        client = StubDynamoDb(item=_item())

        await _store(client).read(JOB, consistent=True)

        assert client.last["ConsistentRead"] is True

    async def test_is_eventually_consistent_by_default(self) -> None:
        client = StubDynamoDb(item=_item())

        await _store(client).read(JOB)

        assert client.last["ConsistentRead"] is False

    async def test_a_missing_job_is_not_found(self) -> None:
        client = StubDynamoDb(item=None)

        with pytest.raises(JobNotFoundError):
            await _store(client).read(JOB)

    async def test_an_unreadable_item_is_reported_rather_than_guessed_at(
        self,
    ) -> None:
        # An unrecognised shape means an older writer or a renamed attribute —
        # a deployment problem, and reading it anyway would be worse.
        client = StubDynamoDb(item={"job_id": {"S": JOB}})

        with pytest.raises(JobStoreError):
            await _store(client).read(JOB)


class TestFailureTranslation:
    async def test_a_refused_call_becomes_a_store_error(self) -> None:
        client = StubDynamoDb(error=_other_failure())

        with pytest.raises(JobStoreError) as raised:
            await _store(client).read(JOB)

        # The cause is kept for the log; the message that travels outwards must
        # not carry the account id or the table ARN botocore puts in it.
        assert "arn:aws" not in str(raised.value)
        assert isinstance(raised.value.__cause__, ClientError)

    async def test_an_unreachable_table_becomes_a_store_error(self) -> None:
        client = StubDynamoDb(
            error=ConnectTimeoutError(endpoint_url="https://dynamodb")
        )

        with pytest.raises(JobStoreError):
            await _store(client).read(JOB)
