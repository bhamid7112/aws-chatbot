"""The Lambda dispatcher, against a stub client.

The payload shape is the interesting part. It is a wire contract with the
worker's ``/events`` route, and the two halves are deployed by separate,
non-simultaneous updates — so these tests pin the shape that both sides agree
on today.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from app.domain.entities import ChatRequest, Message, Role
from app.domain.errors import InvalidPromptError, JobDispatchError, RequestTooLargeError
from app.domain.ports import JobDispatcher
from app.infrastructure.lambda_job_dispatcher import LambdaJobDispatcher
from app.interfaces.schemas import WorkerEventDTO

JOB = "j1"
FUNCTION = "chatbot-worker"


class StubLambda:
    """Records invocations and can be primed to fail."""

    def __init__(
        self,
        *,
        status_code: int = 202,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status_code = status_code
        self._error = error

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"StatusCode": self._status_code}

    @property
    def payload(self) -> dict[str, Any]:
        decoded: dict[str, Any] = json.loads(self.calls[-1]["Payload"])
        return decoded


def _dispatcher(client: Any, **kwargs: Any) -> LambdaJobDispatcher:
    return LambdaJobDispatcher(client, FUNCTION, **kwargs)


def test_satisfies_the_port() -> None:
    assert isinstance(_dispatcher(StubLambda()), JobDispatcher)


@pytest.mark.parametrize("kwargs", [{"max_payload_bytes": 0}])
def test_rejects_nonsense_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _dispatcher(StubLambda(), **kwargs)


def test_rejects_a_blank_function_name() -> None:
    with pytest.raises(ValueError):
        LambdaJobDispatcher(cast("Any", StubLambda()), "   ")


class TestDispatch:
    async def test_invokes_the_worker_without_waiting_for_it(self) -> None:
        # "Event", not "RequestResponse". A buffered invocation would hold the
        # submit request open for the length of the reply, which is the entire
        # thing this transport exists to avoid.
        client = StubLambda()

        await _dispatcher(client).dispatch(JOB, ChatRequest(prompt="hi"))

        assert client.calls[-1]["FunctionName"] == FUNCTION
        assert client.calls[-1]["InvocationType"] == "Event"

    async def test_carries_the_prompt_and_the_history(self) -> None:
        client = StubLambda()
        request = ChatRequest(
            prompt="and then?",
            history=(
                Message(Role.USER, "hello"),
                Message(Role.ASSISTANT, "hi there"),
            ),
        )

        await _dispatcher(client).dispatch(JOB, request)

        assert client.payload["job_id"] == JOB
        assert client.payload["request"]["message"] == "and then?"
        assert client.payload["request"]["history"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    async def test_the_payload_is_what_the_worker_parses(self) -> None:
        # The two ends of the contract, checked against each other. A rename on
        # either side would otherwise only show up on a real deployment.
        client = StubLambda()
        request = ChatRequest(
            prompt="and then?", history=(Message(Role.USER, "hello"),)
        )

        await _dispatcher(client).dispatch(JOB, request)
        event = WorkerEventDTO.model_validate(client.payload)

        assert event.job_id == JOB
        assert event.request.to_domain() == request

    async def test_does_not_store_the_prompt_anywhere_else(self) -> None:
        # The prompt travels in the payload precisely so it is never written to
        # the job table. Nothing here should be persisting it.
        client = StubLambda()

        await _dispatcher(client).dispatch(JOB, ChatRequest(prompt="secret"))

        assert len(client.calls) == 1


class TestTooLarge:
    async def test_refuses_a_request_that_will_not_fit(self) -> None:
        client = StubLambda()

        with pytest.raises(RequestTooLargeError):
            await _dispatcher(client, max_payload_bytes=64).dispatch(
                JOB, ChatRequest(prompt="x" * 500)
            )

        assert client.calls == []

    async def test_too_large_is_caller_error(self) -> None:
        # So the interface layer's existing 422 mapping already covers it and
        # needs no branch of its own.
        assert issubclass(RequestTooLargeError, InvalidPromptError)


class TestFailure:
    async def test_a_refused_invocation_is_reported(self) -> None:
        client = StubLambda(
            error=ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "arn:aws:lambda:us-east-2:123456789012:function/x",
                    }
                },
                "Invoke",
            )
        )

        with pytest.raises(JobDispatchError) as raised:
            await _dispatcher(client).dispatch(JOB, ChatRequest(prompt="hi"))

        assert "arn:aws" not in str(raised.value)

    async def test_an_unreachable_lambda_is_reported(self) -> None:
        client = StubLambda(error=ConnectTimeoutError(endpoint_url="https://lambda"))

        with pytest.raises(JobDispatchError):
            await _dispatcher(client).dispatch(JOB, ChatRequest(prompt="hi"))

    async def test_anything_other_than_accepted_is_a_failure(self) -> None:
        # A queued event is always 202. Treating anything else as success would
        # leave a job that nothing is ever going to run.
        client = StubLambda(status_code=200)

        with pytest.raises(JobDispatchError):
            await _dispatcher(client).dispatch(JOB, ChatRequest(prompt="hi"))
