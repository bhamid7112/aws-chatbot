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


class ReplyGenerationError(ChatError):
    """A :class:`~app.domain.ports.ReplyGenerator` could not produce a reply.

    The only exception type a generator is permitted to raise. See the port's
    contract for why that matters.
    """
