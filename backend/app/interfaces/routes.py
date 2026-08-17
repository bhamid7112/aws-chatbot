"""HTTP routing.

Sole responsibility: translating between HTTP and the use case (SRP). It parses
and validates the request shape, maps domain errors to status codes, and hands
the chunk stream to the SSE renderer. It contains no reply logic and no wire
framing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.domain.errors import InvalidPromptError
from app.interfaces.dependencies import ChatServiceDep
from app.interfaces.schemas import ChatRequestDTO, ErrorDTO, HealthDTO
from app.interfaces.sse import SSE_MEDIA_TYPE, STREAM_HEADERS, event_stream

router = APIRouter(prefix="/api")

# Literal codes rather than Starlette's ``status`` constants: the 422 constant
# was renamed (ENTITY -> CONTENT) and the old name now warns, so the number is
# the one spelling that stays correct across versions.
HTTP_OK = 200
HTTP_UNPROCESSABLE = 422


@router.get(
    "/health",
    response_model=HealthDTO,
    summary="Liveness probe",
    tags=["ops"],
)
async def health() -> HealthDTO:
    """Report that the process is up. Used by the container healthcheck."""
    return HealthDTO()


@router.post(
    "/chat",
    summary="Stream a reply to a message",
    tags=["chat"],
    responses={
        HTTP_OK: {
            "content": {SSE_MEDIA_TYPE: {}},
            "description": "Reply streamed as Server-Sent Events.",
        },
        HTTP_UNPROCESSABLE: {
            "model": ErrorDTO,
            "description": "The request or the prompt was rejected.",
        },
    },
)
async def chat(payload: ChatRequestDTO, service: ChatServiceDep) -> StreamingResponse:
    """Answer ``payload.message``, streaming the reply as it is produced.

    ``stream_reply`` validates eagerly, so an unacceptable prompt is rejected
    here with a 422 — before the streaming response starts and the status code
    becomes unchangeable. Failures that surface *during* generation are reported
    in-band by the SSE renderer instead.
    """
    try:
        chunks = service.stream_reply(payload.to_domain())
    except InvalidPromptError as exc:
        raise HTTPException(status_code=HTTP_UNPROCESSABLE, detail=str(exc)) from exc

    return StreamingResponse(
        event_stream(chunks),
        media_type=SSE_MEDIA_TYPE,
        headers=STREAM_HEADERS,
    )
