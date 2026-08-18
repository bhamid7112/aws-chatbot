# syntax=docker/dockerfile:1
#
# The web image. Build context is the repository root (see docker-compose.yml),
# so every path below is repo-relative.
#
# A static bundle needs no server of its own, so this is a *build* container that
# ends as one: node compiles the bundle, and only the compiled output crosses into
# the Caddy image that serves it. No node_modules, no toolchain and no TypeScript
# source survive into the runtime layer.
#
# The bundle is baked in rather than written into a volume Caddy mounts. That
# makes a release atomic — the container either has a whole bundle or never
# starts — and leaves nothing behind between releases, whereas a named volume is
# never cleaned and would accumulate every past build's content-hashed assets.

ARG NODE_VERSION=24
# Pinned to a minor, not to `2`: `profile` on the ACME issuer is a recent and
# still-experimental directive, so the exact server the Caddyfile was validated
# against is worth naming. The build below re-validates on every image, so an
# incompatible bump fails here rather than on the server.
ARG CADDY_VERSION=2.11

# ── build ─────────────────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-alpine AS build

WORKDIR /build

# Manifest first, so editing application code does not reinstall dependencies.
# `npm ci` rather than `install`: it fails on a stale lockfile instead of quietly
# resolving something new, which is the same contract as uv's --locked.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# Build gates, not politeness. check:layers fails the image on a Clean
# Architecture violation, and `build` runs `tsc -b` before Vite, so a type error
# never reaches a bundle. Both are the same commands a developer runs locally.
RUN npm run check:layers && npm run build

# ── runtime ───────────────────────────────────────────────────────────────────
FROM caddy:${CADDY_VERSION}-alpine AS runtime

COPY deploy/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /build/dist /srv/www

# Fail the build on a malformed Caddyfile — in *both* shapes the deployment uses:
# an explicit http:// address (local, no TLS) and a bare IP literal (production,
# where default_sni attaches a TLS connection policy). Checking only one would let
# a change break the other silently, and the production path cannot be exercised
# locally in any other way.
#
# 198.51.100.10 is from RFC 5737's TEST-NET-2, reserved for documentation and
# never routable. `validate` builds and provisions the configuration without
# starting a server: no port is bound, no certificate is requested, and no ACME
# server is contacted.
RUN set -eu; \
    for addr in http://localhost 198.51.100.10; do \
        echo "validating SITE_ADDRESS=$addr"; \
        SITE_ADDRESS="$addr" ACME_EMAIL=build@invalid \
            caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile; \
    done

EXPOSE 80 443

# The admin API answers on the loopback interface regardless of which ports the
# site is listening on, so this healthcheck holds for both the local HTTP stack
# and the production HTTPS one.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["wget", "--quiet", "--spider", "http://127.0.0.1:2019/config/"]
