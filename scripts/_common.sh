# Shared helpers for the release scripts. Sourced, never executed.
#
# POSIX sh, because on the Windows workstation this is developed on the available
# shell is Git Bash and infra/README.md already directs verification steps there.
# Nothing here uses a bashism.

# The serverless stack is the only source of truth for where a release goes. The
# scripts read it rather than holding their own copies of the registry URL,
# region, profile or architecture — a script with its own copy of the
# architecture is a script that will one day build amd64 for an arm64 function.
STACK_DIR="${STACK_DIR:-infra/serverless}"

log()  { printf '\033[1m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Fail early and by name, rather than midway through a build with a confusing
# error from a missing binary.
require_commands() {
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not on PATH."
    done
}

# Run from the repository root whatever directory the script was invoked from, so
# `docker buildx` gets the build context deploy/*.Dockerfile expects and
# -chdir=infra/serverless resolves.
cd_repo_root() {
    root=$(git rev-parse --show-toplevel 2>/dev/null) \
        || die "Not inside a git repository. These scripts release a commit, so they need one."
    cd "$root" || die "Could not enter $root."
}

# Read one output from the stack. Empty is a legitimate value (aws_profile), so
# the caller decides whether empty is a problem; a *missing* output is not.
tf_output() {
    terraform -chdir="$STACK_DIR" output -raw "$1" 2>/dev/null
}

require_tf_output() {
    value=$(tf_output "$1")
    [ -n "$value" ] || die "Stack output '$1' is empty or missing. Has '$STACK_DIR' been applied? See $STACK_DIR/README.md."
    printf '%s' "$value"
}

# Assembles the --region/--profile flags every aws call in a release needs, so a
# release cannot authenticate as a different account than the apply did.
aws_flags() {
    region=$(require_tf_output aws_region)
    profile=$(tf_output aws_profile)

    printf -- '--region %s' "$region"
    [ -n "$profile" ] && printf -- ' --profile %s' "$profile"
}

# The tag every artifact in a release is named by.
#
# A short commit SHA, so a deployed image can always be traced back to source.
# The dirty-tree check is a warning rather than an error: deploying uncommitted
# work is a normal thing to do while debugging, but it means the tag is a lie
# about what is running, and that is worth being told once.
release_tag() {
    sha=$(git rev-parse --short=12 HEAD 2>/dev/null) || die "Could not resolve HEAD."

    if ! git diff --quiet HEAD 2>/dev/null || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        warn "Working tree has uncommitted changes: the image will be tagged $sha but does not match that commit."
    fi

    printf '%s' "$sha"
}

# Docker needs a registry credential; ECR issues a 12-hour one on demand. Note
# that this is short-lived and derived from the caller's own session — there is
# no stored registry password anywhere in this repository.
ecr_login() {
    registry=$1
    shift
    log "Authenticating docker to ${registry%%/*}"
    aws ecr get-login-password "$@" \
        | docker login --username AWS --password-stdin "${registry%%/*}" >/dev/null \
        || die "docker login to ECR failed."
}

# Maps the Terraform architecture name onto the platform string buildx wants.
docker_platform() {
    case "$1" in
        x86_64) printf 'linux/amd64' ;;
        arm64)  printf 'linux/arm64' ;;
        *)      die "Unknown architecture '$1'. Expected x86_64 or arm64." ;;
    esac
}
