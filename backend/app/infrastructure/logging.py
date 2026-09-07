"""Logging setup — one handler, on the root logger, installed once.

Configuring logging is an infrastructure detail, so it lives here rather than in
the layers that log. Inner layers keep doing the only thing they should do:
``logging.getLogger(__name__)`` and a call. They never learn where the records
go, and nothing outside this module names a handler, a stream or a format.

Why this module exists at all: without it, records from ``app.*`` reach a root
logger with no handlers, and Python falls back to :data:`logging.lastResort` —
which writes the bare message to stderr with no level, no logger name and no
timestamp. In production that is not a cosmetic loss. A CloudWatch metric filter
matches on the text of a log event, so a line with no level in it is a line no
error alert can find, and the alert stays silent in precisely the case it was
built for. See ``infra/serverless/alerts.tf``.

The root logger rather than an ``app`` logger, deliberately: botocore, anyio and
urllib3 warnings are often the first sign of trouble, and routing them through
the same handler means they are subject to the same alerting.

Note the module name shadows the standard library's within this package only in
appearance. Python 3 resolves ``import logging`` below absolutely, so this file
imports the real :mod:`logging` and not itself.
"""

from __future__ import annotations

import logging
from typing import TextIO

#: Emitted level first so a metric filter can match on the token, and the logger
#: name second so a line identifies its origin without a traceback.
LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

#: How the handler is found again on a second call. A name rather than an
#: identity check or a module-level flag, because it survives a module reload and
#: because it makes the handler recognisable to anything inspecting the root
#: logger — a test, or a debugger.
HANDLER_NAME = "aws-chatbot"

DEFAULT_LEVEL = logging.INFO


def configure_logging(
    level: int = DEFAULT_LEVEL,
    stream: TextIO | None = None,
) -> None:
    """Install the application's log handler on the root logger.

    Idempotent by replacement rather than by early return: a previously
    installed handler is removed and a fresh one takes its place. Both halves of
    that matter. Replacing means repeated calls cannot accumulate handlers and
    double every line — ``create_app`` is called once per test, so accumulation
    is the default outcome, not an edge case. Replacing *rather than skipping*
    means a second call with different arguments is honoured, so the function
    has no order-dependent behaviour to remember.

    Args:
        level: Threshold for the handler and for the root logger. Both are set,
            because the root logger's own default is WARNING and would otherwise
            discard INFO records before any handler saw them.

            Note that lowering the root logger to INFO also admits INFO records
            from every installed library, where the previous fallback admitted
            only WARNING and above. In practice that is close to free: botocore
            and urllib3 keep their per-request chatter at DEBUG, so almost
            nothing new is emitted, and log volume is a real cost on this
            deployment. Revisit if that stops being true.
        stream: Where records are written. Defaults to ``sys.stderr``, which is
            what both deployment targets collect: Docker on EC2, and the Lambda
            runtime, which forwards the stream to CloudWatch Logs unchanged.
    """
    root = logging.getLogger()

    for existing in [h for h in root.handlers if h.name == HANDLER_NAME]:
        root.removeHandler(existing)
        existing.close()

    handler = logging.StreamHandler(stream)
    handler.set_name(HANDLER_NAME)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root.addHandler(handler)
    root.setLevel(level)
