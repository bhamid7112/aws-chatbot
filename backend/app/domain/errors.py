"""Domain errors.

Every failure the inner layers can express is one of these. Adapters translate
vendor-specific exceptions into them at the boundary, which is what stops
infrastructure concerns (``botocore.exceptions``, ``httpx.HTTPError``, ...) from
leaking inwards and forcing the use case to know who its collaborators are.
"""

from __future__ import annotations


class ChatError(Exception):
    """Base class for all chat domain errors."""


class InvalidPromptError(ChatError):
    """The prompt breaks a domain rule — blank, or longer than allowed.

    Caller error: the request should be rejected, not retried as-is.
    """


class RequestTooLargeError(InvalidPromptError):
    """The request will not fit in the transport that carries it to a worker.

    Deliberately an :class:`InvalidPromptError`: it is caller error with the
    same remedy — send less — so the interface layer's existing 422 mapping
    already handles it and needs no new branch.

    The bound is an adapter's, not a domain rule's, which is why the limit
    itself lives with the adapter that is constrained by it.
    """


class ReplyGenerationError(ChatError):
    """A :class:`~app.domain.ports.ReplyGenerator` could not produce a reply.

    The only exception type a generator is permitted to raise. See the port's
    contract for why that matters.
    """


class JobNotFoundError(ChatError):
    """No such job — never created, or already expired by its TTL.

    Not a failure of the job: a job that ran to completion and then aged out
    is indistinguishable from one that never existed, and both mean the same
    thing to a caller holding the id.
    """


class JobStoreError(ChatError):
    """A :class:`~app.domain.ports.JobStore` could not be read or written."""


class JobDispatchError(ChatError):
    """A :class:`~app.domain.ports.JobDispatcher` could not hand off the work.

    Distinct from :class:`JobStoreError` because the remedies differ: the job
    record exists and is sound, but nothing is going to pick it up, so the
    caller can be told immediately rather than waiting out a deadline.
    """
