# Every input the serverless deployment has. All have defaults, so
# `terraform apply -var-file=../shared.tfvars` is a complete environment.
#
# The five variables this shares with ../server (aws_region, aws_profile,
# project, bedrock_model_id, bedrock_region) are duplicated rather than shared:
# Terraform has no cross-directory tfvars mechanism, so the *invocation* carries
# them, via a gitignored ../shared.tfvars. See ../README.md.

# ── shared with ../server ─────────────────────────────────────────────────────

variable "aws_region" {
  description = <<-EOT
    Region for the ECR repository, the function and its log group. ECR must be in
    the function's own region — cross-account is allowed, cross-region is not —
    so these three cannot be separated.

    Defaults to us-east-2 to match `bedrock_region`, so inference costs no
    cross-region hop. Nothing in this stack pins the region the way the Elastic IP
    pins ../server; CloudFront and its certificate are global.
  EOT
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = <<-EOT
    Named profile from ~/.aws/config to authenticate with. Empty means the
    ambient credential chain — environment variables, then the default profile,
    then an instance role.

    Setting it earns its place on a workstation holding several profiles for
    several accounts: it pins which account this configuration builds in, so the
    answer is reviewable in a tfvars file rather than in whatever AWS_PROFILE
    happened to be exported. Creating this deployment in the wrong account is not
    an error Terraform can catch.
  EOT
  type        = string
  default     = ""
}

variable "project" {
  description = "Name prefix and Project tag for everything this configuration creates. Shared with ../server, which is safe because both stacks' resource types are disjoint."
  type        = string
  default     = "aws-chatbot"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,28}[a-z0-9]$", var.project))
    error_message = "project must be lowercase alphanumeric with hyphens, 3-30 characters, starting with a letter."
  }
}

variable "bedrock_model_id" {
  description = <<-EOT
    The Bedrock foundation model the API calls. Reaches the function as
    CHAT_BEDROCK_MODEL_ID *and* scopes the execution role's invoke permission, so
    the two cannot disagree.

    `google.gemma-3-27b-it` has no cross-region inference profile: there is no
    `us.`-prefixed variant, so `bedrock_region` must be a region that actually
    offers the model rather than merely a nearby one.
  EOT
  type        = string
  default     = "google.gemma-3-27b-it"
}

variable "bedrock_region" {
  description = "Region whose Bedrock endpoint the API calls. Independent of aws_region on purpose: model availability is patchier than the rest of AWS."
  type        = string
  default     = "us-east-2"
}

# ── this stack only ───────────────────────────────────────────────────────────

variable "reply_source" {
  description = <<-EOT
    Which reply generator the function uses: `bedrock` calls the real model,
    `canned` streams a fixed reply with no AWS call at all.

    `canned` is not only a fallback — it is the sharper instrument for verifying
    that the response streams end to end, because it emits words at a known,
    fixed cadence (see `word_delay_seconds`) while a model's cadence is unknown.
    A single burst arriving at the end therefore proves something is buffering,
    which is not a conclusion a real model's output could support.
  EOT
  type        = string
  default     = "bedrock"

  validation {
    condition     = contains(["bedrock", "canned"], var.reply_source)
    error_message = "reply_source must be either \"bedrock\" or \"canned\"."
  }
}

variable "word_delay_seconds" {
  description = "Seconds between words for the canned generator. Only consulted when reply_source is \"canned\"; it is the known cadence the streaming check measures against."
  type        = number
  default     = 0.06
}

variable "image_tag" {
  description = <<-EOT
    Tag of the ECR image the function runs — a git commit SHA, written by
    scripts/release-api.sh.

    Read only when the function is *created*. Afterwards releases update the
    function directly, by digest, and Terraform ignores image_uri (see
    lambda.tf), so changing this on an existing deployment does nothing. That is
    deliberate: Terraform owns the environment and has no part in a release.
  EOT
  type        = string
  default     = "bootstrap"
}

variable "lambda_architecture" {
  description = <<-EOT
    Instruction set for the function, which must match what the release script
    builds — a mismatch fails at function-create time with an architecture error,
    so one variable drives both.

    x86_64 is the default because this repository is developed on an x86
    workstation, where an arm64 image builds under QEMU emulation and `uv sync`
    under emulation is slow enough to hurt the edit-deploy loop. arm64 is cheaper
    per millisecond and slightly faster to start; flip this once the deployment
    is stable, or when releases run in CI on arm64 hardware.
  EOT
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.lambda_architecture)
    error_message = "lambda_architecture must be either \"x86_64\" or \"arm64\"."
  }
}

variable "memory_size_mb" {
  description = <<-EOT
    Function memory, which on Lambda also buys proportional CPU. 1536 MB is
    chosen for cold-start rather than for working set: importing FastAPI and
    boto3 is CPU-bound, so a larger size finishes init sooner and can cost less
    overall than a smaller one that runs longer.

    Measure `Init Duration` in the REPORT log lines before tuning this.
  EOT
  type        = number
  default     = 1536

  validation {
    condition     = var.memory_size_mb >= 512 && var.memory_size_mb <= 10240
    error_message = "memory_size_mb must be between 512 and 10240. Below 512 the Python import cost dominates every invocation."
  }
}

variable "timeout_seconds" {
  description = <<-EOT
    Maximum function duration. Must exceed botocore's 60 s read timeout plus init,
    or Lambda kills a request that the SDK was still willing to wait for and the
    client sees a truncated stream rather than an error.

    It is also a cost ceiling: Lambda bills the full duration and does *not* stop
    a stream when the viewer disconnects, so an abandoned chat bills until the
    generator finishes or this timeout fires.
  EOT
  type        = number
  default     = 120

  validation {
    condition     = var.timeout_seconds >= 70 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 70 and 900: below 70 it can cut off a request botocore is still waiting on."
  }
}

variable "log_retention_days" {
  description = "CloudWatch retention for the function's log group. Set explicitly because the default is \"never expire\", which makes logs the largest line item on a low-traffic deployment."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the values CloudWatch accepts (1, 3, 5, 7, 14, 30, 60, 90, ...)."
  }
}

variable "price_class" {
  description = "Which CloudFront edge locations serve the distribution. The cheapest class still covers North America and Europe; the expensive classes buy latency in regions this deployment has no users in."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be PriceClass_100, PriceClass_200 or PriceClass_All."
  }
}

variable "max_prompt_chars" {
  description = "Longest prompt the API accepts before returning 422. Matches the EC2 deployment's value so behaviour does not differ by target."
  type        = number
  default     = 4000
}

variable "max_history_messages" {
  description = "How many prior messages are sent to the model as context. Matches the EC2 deployment's value."
  type        = number
  default     = 20
}

variable "bedrock_max_output_tokens" {
  description = "Upper bound on generated tokens. Doubles as a cost control here in a way it is not on EC2, because it bounds how long a billed, possibly-abandoned stream can run."
  type        = number
  default     = 1024
}

variable "bedrock_temperature" {
  description = "Sampling temperature passed to the model. Matches the EC2 deployment's value."
  type        = number
  default     = 0.7
}
