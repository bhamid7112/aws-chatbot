#!/bin/sh
#
# Release the frontend bundle: build, upload, then invalidate the shell.
#
#   ./scripts/release-web.sh
#   ./scripts/release-web.sh --no-build     upload whatever is already in .build/dist
#
# The counterpart of release-api.sh, and like it, Terraform is not involved.
#
# Requires: docker (with buildx), aws, git, terraform.
# Without Docker: `cd frontend && npm ci && npm run build`, then pass --no-build
# and point BUNDLE_DIR at frontend/dist. The same npm scripts run either way, so
# that fallback is not a second implementation of the build.

set -eu

. "$(dirname "$0")/_common.sh"

BUNDLE_DIR="${BUNDLE_DIR:-.build/dist}"

build=1
for arg in "$@"; do
    case "$arg" in
        --no-build) build=0 ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'
            exit 0
            ;;
        *) die "Unknown argument '$arg'. See --help." ;;
    esac
done

require_commands aws git terraform
[ "$build" -eq 1 ] && require_commands docker
cd_repo_root

bucket=$(require_tf_output site_bucket)
distribution=$(require_tf_output distribution_id)

flags=$(aws_flags) || die "Could not resolve the region from '$STACK_DIR'. Has it been applied?"
# shellcheck disable=SC2086 # deliberate word splitting: these are separate flags.
set -- $flags

if [ "$build" -eq 1 ]; then
    log "Building the bundle"

    # The `bundle` stage hangs off `build`, so the check:layers and `tsc -b` gates
    # in deploy/web.Dockerfile apply to what goes to S3 exactly as they do to the
    # Caddy image. A layering violation or a type error fails the release here.
    #
    # The output directory is emptied first: buildx writes into it rather than
    # replacing it, so a file deleted from the bundle between releases would
    # otherwise linger and be uploaded.
    rm -rf "$BUNDLE_DIR"
    docker buildx build \
        --file deploy/web.Dockerfile \
        --target bundle \
        --output "type=local,dest=$BUNDLE_DIR" \
        .
fi

[ -f "$BUNDLE_DIR/index.html" ] || die "No index.html in $BUNDLE_DIR — nothing to release."

# ── Upload order is the whole design of this script ───────────────────────────
#
# Assets first, index.html last. A browser loads the shell and then fetches the
# chunks it names, so uploading the shell first opens a window in which it asks
# for assets that are not there yet. Doing it in this order means the shell is
# never newer than the files it references.
#
# And never --delete. The previous release's content-hashed chunks must survive,
# because every tab still open on the old bundle is still asking for them —
# the same hazard the Caddyfile's SPA exclusion guards against, seen from the
# other side. ECR's lifecycle policy prunes old images; nothing prunes these, and
# a few stale chunks are far cheaper than breaking open sessions.

log "Uploading assets to s3://$bucket"

# Content-hashed filenames, so the content at a given URL can never change: a
# year of immutable caching is correct rather than aggressive, and it means a
# returning visitor revalidates nothing.
aws s3 sync "$BUNDLE_DIR" "s3://$bucket" \
    --exclude 'index.html' \
    --exclude '.gitkeep' \
    --cache-control 'public, max-age=31536000, immutable' \
    --no-progress "$@"

log "Uploading index.html"

# no-cache, not no-store: the browser may keep it but must revalidate every time.
# This is what makes a release visible — the shell is the only mutable URL in the
# bundle, and it names the new chunks.
aws s3 cp "$BUNDLE_DIR/index.html" "s3://$bucket/index.html" \
    --cache-control 'no-cache' \
    --content-type 'text/html; charset=utf-8' \
    --no-progress "$@"

# Only the shell. The assets are content-hashed, so their URLs are new and cannot
# be stale — invalidating /* would pay for wildcard invalidation to expire objects
# that nothing will ever request again.
#
# /index.html and / are both listed because the distribution's
# default_root_object makes them separate cache entries for the same object.
log "Invalidating the shell"
invalidation=$(aws cloudfront create-invalidation \
    --distribution-id "$distribution" \
    --paths '/' '/index.html' \
    --query 'Invalidation.Id' \
    --output text "$@")

log "Waiting for invalidation $invalidation"
aws cloudfront wait invalidation-completed \
    --distribution-id "$distribution" \
    --id "$invalidation" "$@" \
    || warn "Invalidation $invalidation did not complete in time; the shell may serve stale for a few minutes."

log "Released $(git rev-parse --short=12 HEAD) to $(tf_output site_url)"
