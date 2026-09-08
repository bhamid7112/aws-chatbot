"""A :class:`JobDispatcher` that hands a job to a Lambda function.

The hand-off is an asynchronous invocation, which is worth naming because of
what it makes unnecessary. Lambda's own event queue already buffers durably,
retries throttles and service errors with exponential backoff, retries function
errors a bounded number of times, and diverts what it cannot deliver to a
failure destination. A queue in front of the worker would add a second delivery
mechanism to configure and reason about, and would provide none of that twice.

What it does *not* provide is exactly-once delivery: AWS documents that the
queue is eventually consistent and can deliver the same event more than once.
That is safe here only because claiming a job is a conditional write, so the
second delivery loses and no prompt is ever answered — or billed — twice.

Everything vendor-specific stops here (DIP). ``domain`` and ``application``
never learn that the worker is a Lambda function, or that there is a worker at
all.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import TYPE_CHECKING, Any

import anyio.to_thread
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.domain.entities import ChatRequest
from app.domain.errors import JobDispatchError, RequestTooLargeError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from mypy_boto3_lambda.client import LambdaClient

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAYLOAD_BYTES = 256 * 1024
"""The largest request this dispatcher will carry.

Lambda's own ceiling for an asynchronous payload is 1 MB, so this is a
deliberate quarter of it. The margin is not timidity: the payload is copied
verbatim into the record Lambda writes to the failure destination when a job
exhausts its retries, and a record too large for that destination is dropped
with only a metric to say so — losing the report for precisely the jobs most
worth reporting. Refusing the request up front turns a silent loss into a
straightforward rejection the caller can act on.
"""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 4

_ACCEPTED_STATUS = 202
_REFUSED_MESSAGE = "The assistant could not accept the request."


def build_lambda_client(
    region: str | None = None,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> LambdaClient:
    """Build the client used to hand jobs off.

    Retries are enabled, and they are safe for the same reason the store's are:
    a re-sent invocation produces a second event, and a second event loses the
    claim. Without that condition this configuration would be a way to answer
    one prompt twice.

    ``region`` defaults to boto3's own resolution — the worker is always in the
    same region as whatever is dispatching to it.
    """
    return boto3.client(
        "lambda",
        region_name=region,
        config=Config(
            retries={"max_attempts": max_attempts, "mode": "standard"},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        ),
    )


class LambdaJobDispatcher:
    """Invokes a function asynchronously, once per job."""

    def __init__(
        self,
        client: LambdaClient,
        function_name: str,
        *,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        if not function_name.strip():
            raise ValueError("function_name must not be blank")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be at least 1")
        self._client = client
        self._function_name = function_name
        self._max_payload_bytes = max_payload_bytes

    async def dispatch(self, job_id: str, request: ChatRequest) -> None:
        """Ask the worker to answer ``request`` under ``job_id``.

        Raises:
            RequestTooLargeError: The request will not fit in an event.
            JobDispatchError: Lambda refused or could not be reached.
        """
        payload = json.dumps(_to_event(job_id, request), ensure_ascii=False).encode(
            "utf-8"
        )

        if len(payload) > self._max_payload_bytes:
            raise RequestTooLargeError(
                "The conversation is too long to continue. Start a new chat."
            )

        call = partial(
            self._client.invoke,
            FunctionName=self._function_name,
            # The whole point. A buffered invocation would hold this request
            # open for the length of the reply, which is what we are here to
            # avoid.
            InvocationType="Event",
            Payload=payload,
        )

        try:
            response = await anyio.to_thread.run_sync(call)
        except ClientError as exc:
            logger.exception("Lambda refused an asynchronous invocation")
            raise JobDispatchError(_REFUSED_MESSAGE) from exc
        except BotoCoreError as exc:
            logger.exception("Could not reach Lambda to dispatch a job")
            raise JobDispatchError(_REFUSED_MESSAGE) from exc

        status = response.get("StatusCode")
        if status != _ACCEPTED_STATUS:
            # An accepted event is always 202. Anything else means it was not
            # queued, and treating it as success would leave a job that nothing
            # is ever going to run.
            logger.error(
                "Lambda answered %s rather than %s for job %s",
                status,
                _ACCEPTED_STATUS,
                job_id,
            )
            raise JobDispatchError(_REFUSED_MESSAGE)


def _to_event(job_id: str, request: ChatRequest) -> dict[str, Any]:
    """Render the hand-off payload.

    This shape is a wire contract with the worker's ``/events`` route, and the
    two are deployed by separate updates that are not simultaneous. So it may
    only ever be **extended**: renaming a field, or adding a required one,
    guarantees a window in which one half rejects everything the other sends.

    ``request`` is deliberately the same shape as the public request body — the
    receiver parses it with the very same DTO, so there is no second definition
    of a chat request to fall out of step with this one.
    """
    return {
        "job_id": job_id,
        "request": {
            "message": request.prompt,
            "history": [
                {"role": message.role.value, "content": message.content}
                for message in request.history
            ],
        },
    }
