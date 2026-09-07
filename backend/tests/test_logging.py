"""Guards on the two things logging setup has to get right.

Both are easy to get wrong invisibly, which is why they are tested rather than
trusted:

  1. Records must carry their level. Without a level in the emitted line there is
     nothing for a CloudWatch metric filter to match, so an application error
     reaches the log group and raises no alert at all. That failure is silent in
     exactly the situation the alert exists for.
  2. uvicorn must not undo it. uvicorn calls ``logging.config.dictConfig`` while
     starting, *after* this module has run, and a dictConfig can detach handlers
     it does not know about. The claim that it does not is worth an assertion.
"""

from __future__ import annotations

import io
import logging
import logging.config
from collections.abc import Iterator
from copy import deepcopy

import pytest

from app.infrastructure.logging import HANDLER_NAME, configure_logging


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Leave the root logger as it was found.

    ``configure_logging`` mutates process-global state on purpose, so every test
    here would otherwise leak into the next one — and into the rest of the suite,
    which builds real apps through the ``app`` fixture.
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _installed_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if h.name == HANDLER_NAME]


def test_error_records_carry_their_level_and_logger_name() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("app.infrastructure.bedrock_reply_generator").error("boom")

    assert stream.getvalue() == (
        "ERROR app.infrastructure.bedrock_reply_generator: boom\n"
    )


def test_exceptions_are_logged_with_their_traceback() -> None:
    """``logger.exception`` is how the Bedrock adapter reports failure."""
    stream = io.StringIO()
    configure_logging(stream=stream)

    try:
        raise RuntimeError("underlying cause")
    except RuntimeError:
        logging.getLogger("app.infrastructure").exception("could not reach the model")

    written = stream.getvalue()
    assert written.startswith("ERROR app.infrastructure: could not reach the model\n")
    assert "Traceback (most recent call last):" in written
    assert "RuntimeError: underlying cause" in written


def test_configuring_twice_leaves_one_handler() -> None:
    """``create_app`` runs once per test, and duplicate handlers double every line."""
    configure_logging(stream=io.StringIO())
    configure_logging(stream=io.StringIO())

    assert len(_installed_handlers()) == 1


def test_reconfiguring_redirects_to_the_new_stream() -> None:
    """A replaced handler, not a skipped call — the second stream is the live one."""
    first, second = io.StringIO(), io.StringIO()
    configure_logging(stream=first)
    configure_logging(stream=second)

    logging.getLogger("app").error("boom")

    assert first.getvalue() == ""
    assert second.getvalue() == "ERROR app: boom\n"


def test_records_below_the_level_are_dropped() -> None:
    stream = io.StringIO()
    configure_logging(level=logging.INFO, stream=stream)

    logging.getLogger("app").debug("noise")

    assert stream.getvalue() == ""


def test_survives_uvicorns_logging_config() -> None:
    """The ordering risk: uvicorn configures logging after the app is imported."""
    from uvicorn.config import LOGGING_CONFIG

    stream = io.StringIO()
    configure_logging(stream=stream)

    # deepcopy because dictConfig replaces the "()" factory keys it consumes, and
    # uvicorn's LOGGING_CONFIG is a module-level dict the real server also reads.
    logging.config.dictConfig(deepcopy(LOGGING_CONFIG))

    logging.getLogger("app.infrastructure").error("boom")

    assert len(_installed_handlers()) == 1
    assert stream.getvalue() == "ERROR app.infrastructure: boom\n"


def test_uvicorn_loggers_do_not_propagate_into_the_root_handler() -> None:
    """Otherwise every access line would appear twice: once from each handler."""
    from uvicorn.config import LOGGING_CONFIG

    stream = io.StringIO()
    configure_logging(stream=stream)
    logging.config.dictConfig(deepcopy(LOGGING_CONFIG))

    logging.getLogger("uvicorn.access").error("GET /api/health")

    assert stream.getvalue() == ""
