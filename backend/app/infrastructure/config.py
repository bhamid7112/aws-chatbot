"""Runtime configuration, read from the environment.

Reading the environment is an infrastructure detail, so it lives here and not in
the layers that consume the values. Inner layers receive plain arguments and
never learn that an environment variable was involved.

Plain stdlib rather than ``pydantic-settings``: the values are few and their
parsing is trivial. Defaults are imported from the components that own them, so
no number is written down twice.

Note what is *not* here: nothing to do with AWS credentials. boto3 owns that
resolution entirely — an SSO profile locally, the instance role in production —
and keeping it out of ``Settings`` is what lets one image run in both places.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from app.application.chat_job_service import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_FLUSH_CHARS,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    DEFAULT_MAX_REPLY_CHARS,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_RETENTION_SECONDS,
)
from app.application.chat_service import DEFAULT_MAX_PROMPT_CHARS
from app.infrastructure.bedrock_reply_generator import (
    DEFAULT_MAX_HISTORY_MESSAGES,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL_ID,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_REGION,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEMPERATURE,
)
from app.infrastructure.canned_reply_generator import DEFAULT_WORD_DELAY_SECONDS

DEFAULT_CORS_ALLOW_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


class ReplySource(StrEnum):
    """Which :class:`~app.domain.ports.ReplyGenerator` to install.

    ``BEDROCK`` is the default because that is what the application now is.
    ``CANNED`` remains supported and is not merely a test hook: it is how the
    stack runs with no AWS account, no credentials and no network — a working
    demo for anyone who cannot reach Bedrock.
    """

    BEDROCK = "bedrock"
    CANNED = "canned"


class ProcessRole(StrEnum):
    """What this process is for.

    One image is deployed twice: once to serve requests, once to generate
    replies asynchronously. They run the same code and differ only in which
    routes are mounted, which is what keeps them from drifting apart.

    The role decides whether the worker's entrypoint exists at all. It could be
    inferred — nothing can currently reach that route on the serving deployment
    — but inferring it would make an application's attack surface depend on a
    CDN path pattern and a reverse-proxy matcher staying exactly as they are.
    Naming the role costs one variable and removes that dependency.
    """

    API = "api"
    WORKER = "worker"


@dataclass(frozen=True, slots=True)
class Settings:
    """Values that vary between environments.

    Defaults describe local development. In production Caddy serves the bundle
    and the API from one origin, so ``CHAT_CORS_ALLOW_ORIGINS`` is set empty and
    no CORS middleware is installed at all.
    """

    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    reply_word_delay_seconds: float = DEFAULT_WORD_DELAY_SECONDS
    cors_allow_origins: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_CORS_ALLOW_ORIGINS
    )
    reply_source: ReplySource = ReplySource.BEDROCK
    bedrock_model_id: str = DEFAULT_MODEL_ID
    bedrock_region: str = DEFAULT_REGION
    bedrock_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    bedrock_temperature: float = DEFAULT_TEMPERATURE
    bedrock_read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_history_messages: int = DEFAULT_MAX_HISTORY_MESSAGES

    role: ProcessRole = ProcessRole.API
    #: Empty means the asynchronous path is not available in this deployment.
    #: It is the single switch: no table, no job routes, and ``/api/health``
    #: stops advertising the transport, so a client cannot choose one the
    #: server cannot serve.
    jobs_table_name: str = ""
    worker_function_name: str = ""
    job_deadline_seconds: int = DEFAULT_DEADLINE_SECONDS
    job_retention_seconds: int = DEFAULT_RETENTION_SECONDS
    job_flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS
    job_flush_chars: int = DEFAULT_FLUSH_CHARS
    job_poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS
    job_max_reply_chars: int = DEFAULT_MAX_REPLY_CHARS

    @property
    def async_replies_enabled(self) -> bool:
        """Whether this deployment can accept asynchronous replies.

        Derived rather than configured, so there is no way to advertise the
        transport without having somewhere to keep its state. The serving role
        additionally needs somewhere to send the work.
        """
        if not self.jobs_table_name:
            return False
        if self.role is ProcessRole.API:
            return bool(self.worker_function_name)
        return True

    @property
    def transports(self) -> tuple[str, ...]:
        """Which chat transports a client may use against this deployment.

        Reported by ``/api/health`` so that one bundle can serve both
        deployment targets: the server target has no job routes, and a client
        that guessed otherwise would simply fail.
        """
        return ("sse", "jobs") if self.async_replies_enabled else ("sse",)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``env``, defaulting to the process environment.

        Taking the mapping as an argument keeps this testable without mutating
        global process state.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        defaults = cls()
        return cls(
            max_prompt_chars=_read_int(
                source, "CHAT_MAX_PROMPT_CHARS", defaults.max_prompt_chars
            ),
            reply_word_delay_seconds=_read_float(
                source, "CHAT_WORD_DELAY_SECONDS", defaults.reply_word_delay_seconds
            ),
            cors_allow_origins=_read_csv(
                source, "CHAT_CORS_ALLOW_ORIGINS", defaults.cors_allow_origins
            ),
            reply_source=_read_enum(
                source, "CHAT_REPLY_SOURCE", defaults.reply_source, ReplySource
            ),
            bedrock_model_id=_read_str(
                source, "CHAT_BEDROCK_MODEL_ID", defaults.bedrock_model_id
            ),
            bedrock_region=_read_str(
                source, "CHAT_BEDROCK_REGION", defaults.bedrock_region
            ),
            bedrock_max_output_tokens=_read_int(
                source,
                "CHAT_BEDROCK_MAX_OUTPUT_TOKENS",
                defaults.bedrock_max_output_tokens,
            ),
            bedrock_temperature=_read_float(
                source, "CHAT_BEDROCK_TEMPERATURE", defaults.bedrock_temperature
            ),
            # Not _read_str: an explicitly empty prompt means "send no system
            # block at all", which is a legitimate thing to want.
            system_prompt=source.get("CHAT_SYSTEM_PROMPT", defaults.system_prompt),
            bedrock_read_timeout_seconds=_read_float(
                source,
                "CHAT_BEDROCK_READ_TIMEOUT_SECONDS",
                defaults.bedrock_read_timeout_seconds,
            ),
            max_history_messages=_read_int(
                source, "CHAT_MAX_HISTORY_MESSAGES", defaults.max_history_messages
            ),
            role=_read_enum(source, "CHAT_ROLE", defaults.role, ProcessRole),
            jobs_table_name=_read_str(
                source, "CHAT_JOBS_TABLE", defaults.jobs_table_name
            ),
            worker_function_name=_read_str(
                source, "CHAT_WORKER_FUNCTION_NAME", defaults.worker_function_name
            ),
            job_deadline_seconds=_read_int(
                source, "CHAT_JOB_DEADLINE_SECONDS", defaults.job_deadline_seconds
            ),
            job_retention_seconds=_read_int(
                source, "CHAT_JOB_RETENTION_SECONDS", defaults.job_retention_seconds
            ),
            job_flush_interval_seconds=_read_float(
                source,
                "CHAT_JOB_FLUSH_INTERVAL_SECONDS",
                defaults.job_flush_interval_seconds,
            ),
            job_flush_chars=_read_int(
                source, "CHAT_JOB_FLUSH_CHARS", defaults.job_flush_chars
            ),
            job_poll_interval_ms=_read_int(
                source, "CHAT_JOB_POLL_INTERVAL_MS", defaults.job_poll_interval_ms
            ),
            job_max_reply_chars=_read_int(
                source, "CHAT_JOB_MAX_REPLY_CHARS", defaults.job_max_reply_chars
            ),
        )


def _read_int(source: Mapping[str, str], key: str, fallback: int) -> int:
    raw = source.get(key, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _read_float(source: Mapping[str, str], key: str, fallback: float) -> float:
    raw = source.get(key, "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _read_csv(
    source: Mapping[str, str], key: str, fallback: tuple[str, ...]
) -> tuple[str, ...]:
    """Comma-separated list; an explicitly *empty* value means no entries.

    That distinction carries weight: ``CHAT_CORS_ALLOW_ORIGINS=`` is how
    production disables CORS, so it must not fall back to the dev defaults.
    """
    if key not in source:
        return fallback
    return tuple(item.strip() for item in source[key].split(",") if item.strip())


def _read_str(source: Mapping[str, str], key: str, fallback: str) -> str:
    """Non-empty string, or the fallback.

    Unlike ``_read_csv``, an empty value here is treated as absent rather than as
    meaningful: there is no useful reading of a blank model ID or region, and
    falling back beats failing to start.
    """
    return source.get(key, "").strip() or fallback


def _read_enum[EnumT: StrEnum](
    source: Mapping[str, str],
    key: str,
    fallback: EnumT,
    member_type: type[EnumT],
) -> EnumT:
    """One of an enum's members, or the fallback if unset.

    An unrecognised value raises rather than falling back, and does so at
    import time. A typo in a deployment variable should stop the process
    outright, not silently select a behaviour nobody asked for.
    """
    raw = source.get(key, "").strip().lower()
    if not raw:
        return fallback
    try:
        return member_type(raw)
    except ValueError as exc:
        permitted = ", ".join(member.value for member in member_type)
        raise ValueError(f"{key} must be one of {permitted}, got {raw!r}") from exc
