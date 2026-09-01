# syntax=docker/dockerfile:1
#
# The api image. Build context is the repository root (see docker-compose.yml),
# so every path below is repo-relative.
#
# Four stages, and the reason for each:
#   deps    — resolves the runtime environment only; cached until uv.lock changes
#   test    — the same environment plus the dev/lint groups, so tests run against
#             the image rather than against a developer's machine
#   runtime — a plain Python base with the venv copied in; no uv, no test tooling
#   lambda  — `runtime` plus the Lambda Web Adapter extension, for the serverless
#             deployment target. It adds one binary and changes no application
#             code, which is the whole point: the same uvicorn process serves both
#             EC2 and Lambda.
#
# Pinned to 3.12 to match requires-python, ruff's target-version and mypy's
# python_version — one consistent story rather than four.

ARG PYTHON_VERSION=3.12

# The adapter is the runtime interface between Lambda and this process, so an
# unreviewed bump is an unreviewed change to how every request arrives. Pinned
# for the same reason CADDY_VERSION is pinned in web.Dockerfile.
#
# 1.0.1 is a multi-arch manifest covering x86_64 and arm64, so `COPY --from`
# resolves the right binary from the build's own platform and no -aarch64 tag
# variant is needed. (Pre-1.0 releases required naming the architecture.)
ARG LWA_VERSION=1.0.1

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

# ── lwa ───────────────────────────────────────────────────────────────────────
# The adapter, named as a stage purely so its tag can come from LWA_VERSION:
# `COPY --from` does not interpolate variables, while `FROM` does interpolate a
# global-scope ARG. Nothing is built here — the upstream image is a single binary
# on `scratch`, and only that binary is copied out below.
FROM public.ecr.aws/awsguru/aws-lambda-adapter:${LWA_VERSION} AS lwa

# ── lambda ────────────────────────────────────────────────────────────────────
# The serverless target. `runtime` is the base, so this stage cannot drift from
# what EC2 runs: same venv, same application code, same CMD, same uid.
#
# The Lambda Web Adapter is a Lambda *extension* — a binary in /opt/extensions
# that Lambda starts alongside the function. It ships the runtime interface
# client, which is why a plain python:3.12-slim base works here with no AWS base
# image and no awslambdaric dependency. It starts the inherited CMD, waits for
# the readiness check below to pass, then translates each invocation into an HTTP
# request against uvicorn on 127.0.0.1.
FROM runtime AS lambda

# `runtime` ends as uid 1001 and /opt is root-owned, so the COPY needs root. The
# stage drops back to `app` immediately after — Lambda honours USER, and there is
# no reason for the adapter to run privileged. (If Lambda ever refuses to start
# the extension as non-root, removing the trailing USER is a change to this stage
# only and leaves the EC2 image untouched.)
USER root

COPY --from=lwa /lambda-adapter /opt/extensions/lambda-adapter

USER app

# Only the adapter settings that are facts about *this image* live here — they are
# derivable from the CMD above and from interfaces/routes.py, so Terraform cannot
# get them wrong and the image is self-describing. Deployment-shaped settings
# (invoke mode, which must agree with the Function URL; async init, a tuning
# knob; every CHAT_* variable) are set by Terraform instead, where the things
# they must agree with are defined.
#
# AWS_LWA_PORT is mandatory, not cosmetic: the adapter defaults to 8080 and
# uvicorn is told 8000 by the CMD, so omitting it means the readiness check never
# passes and every invocation times out.
ENV AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/api/health \
    AWS_LWA_READINESS_CHECK_HEALTHY_STATUS=200-299
