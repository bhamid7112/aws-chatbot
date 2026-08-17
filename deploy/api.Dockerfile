# syntax=docker/dockerfile:1
#
# The api image. Build context is the repository root (see docker-compose.yml),
# so every path below is repo-relative.
#
# Three stages, and the reason for each:
#   deps    — resolves the runtime environment only; cached until uv.lock changes
#   test    — the same environment plus the dev/lint groups, so tests run against
#             the image rather than against a developer's machine
#   runtime — a plain Python base with the venv copied in; no uv, no test tooling
#
# Pinned to 3.12 to match requires-python, ruff's target-version and mypy's
# python_version — one consistent story rather than four.

ARG PYTHON_VERSION=3.12

# ── deps ──────────────────────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS deps

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Only the lockfile and manifest are mounted, so editing application code does
# not invalidate this layer. --locked fails the build if uv.lock is stale rather
# than silently resolving something new.
#
# --no-default-groups, not --no-dev: default-groups is ["dev", "lint"], and
# --no-dev would exclude only the first of them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-default-groups

# ── test ──────────────────────────────────────────────────────────────────────
FROM deps AS test

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    uv sync --locked

# pytest reads its configuration (pythonpath, asyncio_mode) from pyproject.toml,
# so the manifest is copied in rather than only mounted.
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/app ./app
COPY backend/tests ./tests

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["pytest"]

# ── runtime ───────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Unprivileged, and created before the COPY so ownership is set in one layer.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=deps --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app backend/app ./app

USER app

EXPOSE 8000

# stdlib only — installing curl for a healthcheck would widen the attack surface
# of the runtime image for no gain.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200 else 1)"]

# One worker: replies are streamed, and Caddy fronts this with a single upstream.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
