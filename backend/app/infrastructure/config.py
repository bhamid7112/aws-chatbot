"""Runtime configuration, read from the environment.

Reading the environment is an infrastructure detail, so it lives here and not in
the layers that consume the values. Inner layers receive plain arguments and
never learn that an environment variable was involved.

Plain stdlib rather than ``pydantic-settings``: three values do not justify a
dependency. Defaults are imported from the components that own them, so no
number is written down twice.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.application.chat_service import DEFAULT_MAX_PROMPT_CHARS
from app.infrastructure.canned_reply_generator import DEFAULT_WORD_DELAY_SECONDS

DEFAULT_CORS_ALLOW_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


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
