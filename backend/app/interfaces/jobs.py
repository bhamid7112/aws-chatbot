"""HTTP routing for the asynchronous transport.

A separate router from ``routes.py`` because these routes are *conditional*.
One bundle is served by two deployment targets and only one of them has
anywhere to keep a job, so on the other this router is never mounted and the
paths simply do not exist. A 404 is then the truth — and the same truth
``GET /api/health`` tells the browser in its ``transports`` list — where a
mounted route with nothing behind it could only manage a 500.

Same responsibility as its sibling and no more: translate between HTTP and the
use case. No lifecycle logic, no store, no knowledge of what generates a reply.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response

from app.domain.errors import ChatError, InvalidPromptError, JobNotFoundError
from app.interfaces.dependencies import ChatJobServiceDep
from app.interfaces.schemas import (
    ChatJobCreatedDTO,
    ChatJobProgressDTO,
    ChatRequestDTO,
    ErrorDTO,
)

router = APIRouter(prefix="/api")

HTTP_ACCEPTED = 202
HTTP_NO_CONTENT = 204
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
HTTP_UNAVAILABLE = 503

# A job id is opaque here on purpose: constraining it to the shape the use case
# currently generates would couple this route to a decision made elsewhere. So
# it is bounded rather than described.
JobId = Annotated[str, Path(min_length=1, max_length=64)]
Cursor = Annotated[int, Path(ge=0)]


@router.post(
    "/chat/jobs",
    status_code=HTTP_ACCEPTED,
    response_model=ChatJobCreatedDTO,
    summary="Submit a reply to be generated in the background",
    tags=["chat"],
    responses={
        HTTP_UNPROCESSABLE: {
            "model": ErrorDTO,
            "description": "The request or the prompt was rejected.",
        },
        HTTP_UNAVAILABLE: {
            "model": ErrorDTO,
            "description": "The request could not be accepted for generation.",
        },
    },
)
async def submit_chat_job(
    payload: ChatRequestDTO,
    service: ChatJobServiceDep,
) -> ChatJobCreatedDTO:
    """Accept ``payload.message`` and answer it out of band.

    Returns as soon as the work is *accepted*, not when it is done — the entire
    difference from ``POST /api/chat``. The reply is read back from the
    segments route, and survives the loss of this connection.

    The request body is the same DTO the streamed route takes, so switching
    transports never changes what a client sends.
    """
    with _translated():
        return ChatJobCreatedDTO(job_id=await service.submit(payload.to_domain()))


@router.get(
    "/chat/jobs/{job_id}/segments/{cursor}",
    response_model=ChatJobProgressDTO,
    summary="Read a reply from a given point onwards",
    tags=["chat"],
    responses={
        HTTP_NOT_FOUND: {
            "model": ErrorDTO,
            "description": "No such job, or it has expired.",
        },
    },
)
async def read_chat_job(
    job_id: JobId,
    cursor: Cursor,
    service: ChatJobServiceDep,
) -> ChatJobProgressDTO:
    """Return the reply's segments from ``cursor`` on, plus its status.

    The cursor is a path segment rather than a query parameter, and that is not
    stylistic. The CDN in front of this API caches nothing today, but its cache
    policy *excludes query strings from the cache key* — so a cursor in the
    query string would be correct only for as long as nobody enables caching
    here, and would then collapse every client onto one snapshot. In the path
    it cannot be collapsed.
    """
    with _translated():
        return ChatJobProgressDTO.from_domain(await service.read(job_id, cursor))


@router.delete(
    "/chat/jobs/{job_id}",
    status_code=HTTP_NO_CONTENT,
    summary="Stop generating a reply",
    tags=["chat"],
)
async def cancel_chat_job(job_id: JobId, service: ChatJobServiceDep) -> Response:
    """Stop the reply, and stop paying for it.

    Idempotent, and deliberately silent about what it found: a job already
    finished, already cancelled, or never known all leave the caller's intent
    satisfied. Reporting the difference would only invite a client — often one
    already unloading the page — to care about an answer it cannot use.
    """
    with _translated():
        await service.cancel(job_id)
    return Response(status_code=HTTP_NO_CONTENT)


@contextmanager
def _translated() -> Iterator[None]:
    """Map domain errors onto status codes, once for all three routes.

    Ordered most specific first, which is what lets
    :class:`~app.domain.errors.RequestTooLargeError` reach 422 with no branch
    of its own: it is an ``InvalidPromptError``, so it is caller error with the
    same remedy and deserves the same code.
    """
    try:
        yield
    except InvalidPromptError as exc:
        raise HTTPException(HTTP_UNPROCESSABLE, detail=str(exc)) from exc
    except JobNotFoundError as exc:
        raise HTTPException(HTTP_NOT_FOUND, detail=str(exc)) from exc
    except ChatError as exc:
        # Store and dispatch failures. The message is a domain message written
        # for a person; the cause was logged where it was understood.
        raise HTTPException(HTTP_UNAVAILABLE, detail=str(exc)) from exc
