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

from app.application.chat_service import DEFAULT_MAX_PROMPT_CHARS
from app.infrastructure.bedrock_reply_generator import (
    DEFAULT_MAX_HISTORY_MESSAGES,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL_ID,
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
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_history_messages: int = DEFAULT_MAX_HISTORY_MESSAGES

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
            reply_source=_read_reply_source(
                source, "CHAT_REPLY_SOURCE", defaults.reply_source
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
            max_history_messages=_read_int(
                source, "CHAT_MAX_HISTORY_MESSAGES", defaults.max_history_messages
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


def _read_reply_source(
    source: Mapping[str, str], key: str, fallback: ReplySource
) -> ReplySource:
    raw = source.get(key, "").strip().lower()
    if not raw:
        return fallback
    try:
        return ReplySource(raw)
    except ValueError as exc:
        permitted = ", ".join(member.value for member in ReplySource)
        raise ValueError(f"{key} must be one of {permitted}, got {raw!r}") from exc
