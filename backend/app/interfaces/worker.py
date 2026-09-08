"""The worker's entrypoint: one route, reached only by the runtime.

This is the same application, in the same image, mounted differently. The
adapter that fronts the process turns a non-HTTP invocation into a POST against
a configured path, so a background worker needs no separate handler, no second
dependency and no code that knows it is on Lambda — it needs a route.

Kept out of ``routes.py`` because it is not part of the API. Nothing a browser
can reach is defined here, the path carries no ``/api`` prefix, and the router
is mounted only when the process is configured as a worker.

**Two invariants this route depends on, both configured rather than coded:**

* The path must match the adapter's pass-through path. It defaults to
  ``/events``, which is what this uses.
* The adapter must be told which status codes mean failure. It treats *every*
  response as a success by default — so without that setting a crash here is
  reported to Lambda as a job well done, and no retry, no failure destination
  and no alert would ever fire. See ``AWS_LWA_ERROR_STATUS_CODES`` in
  ``infra/serverless/locals.tf``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.interfaces.dependencies import ChatJobRunnerDep
from app.interfaces.schemas import WorkerAckDTO, WorkerEventDTO

#: No prefix. This is not part of the ``/api`` surface and must not be routed
#: to as though it were.
router = APIRouter()


@router.post(
    "/events",
    response_model=WorkerAckDTO,
    summary="Generate the reply for one job",
    tags=["worker"],
    include_in_schema=False,
)
async def handle_event(
    payload: WorkerEventDTO,
    runner: ChatJobRunnerDep,
) -> WorkerAckDTO:
    """Run one job to completion, or record why it could not be run.

    Deliberately catches nothing. The use case already absorbs every outcome a
    person could be told about — including a failed reply, which it records
    before returning normally — so anything reaching this far is a defect in
    this process. Letting it become a 500 is what turns it into a failed
    invocation, and from there into a retry, a failure record and an alert.

    Returning normally is equally load-bearing: it is how a duplicate delivery
    is acknowledged. Lambda's event queue can deliver the same event twice, and
    a job that was already claimed must be answered with "handled" rather than
    an error, or the duplicate is retried until it ages out.
    """
    await runner.run(payload.job_id, payload.request.to_domain())
    return WorkerAckDTO()
