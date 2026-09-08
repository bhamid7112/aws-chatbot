#!/bin/sh
#
# Release the api image: build, push, then point every function at the new bytes.
#
#   ./scripts/release-api.sh
#   ./scripts/release-api.sh --skip-update    build and push only
#
# One image, two functions. The API and the worker that generates replies out of
# band run the same bytes, so both are updated here — leaving one behind means
# the API keeps working while every asynchronous job fails, which is a symptom
# that points nowhere near a release.
#
# Terraform is not involved. It owns the environment and has no part in a
# release, which is the same division ../infra/server keeps — there, a release is
# `ssm:SendCommand` and a `git pull` on the instance; here it is a registry push
# and an update-function-code. In both cases `terraform apply` is for changing
# infrastructure, not for shipping code.
#
# Requires: docker (with buildx), aws, git, terraform.

set -eu

. "$(dirname "$0")/_common.sh"

skip_update=0
for arg in "$@"; do
    case "$arg" in
        --skip-update) skip_update=1 ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'
            exit 0
            ;;
        *) die "Unknown argument '$arg'. See --help." ;;
    esac
done

require_commands docker aws git terraform
cd_repo_root

registry=$(require_tf_output ecr_repository_url)

architecture=$(tf_setting lambda_architecture)
[ -n "$architecture" ] || die "Could not resolve lambda_architecture from '$STACK_DIR'. Has it been applied, and does $STACK_DIR/$VAR_FILE exist?"

platform=$(docker_platform "$architecture")
tag=$(release_tag)
image="$registry:$tag"

flags=$(aws_flags) || die "Could not resolve the region from '$STACK_DIR'. Has it been applied, and does $STACK_DIR/$VAR_FILE exist?"
# shellcheck disable=SC2086 # deliberate word splitting: these are separate flags.
set -- $flags

log "Releasing $tag for $architecture"

ecr_login "$registry" "$@"

# Refuse to rebuild a tag that already exists. The repository is IMMUTABLE, so
# the push would fail anyway — but it would fail after a full build, and the
# message would be about a manifest rather than about the actual situation, which
# is that this commit has already been released.
if aws ecr describe-images --repository-name "${registry##*/}" \
        --image-ids "imageTag=$tag" "$@" >/dev/null 2>&1; then
    die "$tag is already in the registry. Commit your changes, or delete the tag to rebuild it."
fi

log "Building $image"

# --provenance=false --sbom=false is mandatory, not tidiness.
#
# buildx defaults to emitting an OCI image *index* with attestation manifests
# attached. Lambda does not support manifest lists and rejects such an image with
# an opaque "image manifest, config or layer media type ... is not supported" —
# which is the most common first-deploy failure with buildx and Lambda, and gives
# no hint that attestations are the cause.
#
# --platform is passed explicitly rather than left to the host, because the
# function's architecture is fixed in Terraform and the two must agree.
docker buildx build \
    --file deploy/api.Dockerfile \
    --target lambda \
    --platform "$platform" \
    --provenance=false \
    --sbom=false \
    --tag "$image" \
    --push \
    .

# The digest is what actually gets deployed. Read it back from the registry
# rather than parsing it out of the build output: after this point the release is
# describing bytes that exist, not bytes buildx said it pushed.
digest=$(aws ecr describe-images \
    --repository-name "${registry##*/}" \
    --image-ids "imageTag=$tag" \
    --query 'imageDetails[0].imageDigest' \
    --output text "$@")

[ -n "$digest" ] && [ "$digest" != "None" ] || die "Pushed $tag but could not read its digest back."

log "Pushed $tag ($digest)"

if [ "$skip_update" -eq 1 ]; then
    log "Skipping the function update, as asked."
    printf '\nBootstrap the function with this image:\n\n  terraform -chdir=%s apply -var-file=../shared.tfvars -var="image_tag=%s"\n\n' \
        "$STACK_DIR" "$tag"
    exit 0
fi

# Every function built from this image, not just the api one.
#
# One image runs twice — the API and the worker that generates replies out of
# band — and both must move together. A release that updates one and not the
# other is the worst failure this deployment can produce: the API keeps
# answering, health checks keep passing, and every asynchronous job fails on a
# payload the stale half cannot parse. Nothing about that points at the release.
#
# Read from the stack rather than listed here, so adding a function changes the
# release without anyone having to remember to.
function_names=$(require_tf_output function_names)

# By digest, not by tag. It records exactly which bytes run — and it is also why
# lambda.tf must ignore changes to image_uri, since the API will now report a
# digest where the configuration says a tag.
#
# Updated in one pass and waited on in another, rather than update-and-wait per
# function. Lambda re-optimises a container image on update and that takes tens
# of seconds; starting both first means the two happen concurrently, and the
# window in which the functions disagree is as short as it can be.
# shellcheck disable=SC2086 # deliberate word splitting: one name per function.
for function_name in $function_names; do
    log "Pointing $function_name at $digest"
    aws lambda update-function-code \
        --function-name "$function_name" \
        --image-uri "$registry@$digest" \
        --no-cli-pager \
        --query 'LastUpdateStatus' \
        --output text "$@"
done

# A container-image update is not instant: Lambda re-optimises the image and goes
# Pending, rejecting invocations until it is Active again. Without this wait the
# script would exit "successfully" while the deployment is still broken, and the
# next curl would fail for a reason that has nothing to do with the code.
# shellcheck disable=SC2086 # deliberate word splitting: one name per function.
for function_name in $function_names; do
    log "Waiting for $function_name to become Active"
    aws lambda wait function-updated --function-name "$function_name" "$@" \
        || die "$function_name did not become Active. Check: $(tf_output api_log_command)"
done

log "Released $tag to $function_names"
