# The two functions' identities, and — as on ../server — the *entire* production
# credential story. There is no API key, no IAM user and no secret in the image
# or the environment: boto3 reads short-lived credentials Lambda injects for
# these roles, they rotate on their own, and every call is attributable in
# CloudTrail.
#
# Two roles rather than one, and the split is the point. Each function gets only
# what its own job needs, and the two jobs need almost opposite things:
#
#   api     write to its own log group
#           invoke one Bedrock model                (the streamed transport)
#           put, get and update a job               (submit, poll, cancel)
#           invoke the worker
#
#   worker  write to its own log group
#           invoke one Bedrock model
#           *update* a job — it cannot read one
#           write to the failure queue
#
# Nothing in either can read or write S3, read a secret, or describe the
# account. Note what is absent besides: no VPC access policy, because neither
# function is in a VPC — they call only public AWS endpoints, so there is no NAT
# gateway and no interface endpoint to pay for, and a VPC would break Function
# URL response streaming outright.

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    sid     = "AllowLambdaToAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name_prefix        = "${local.name}-api-"
  description        = "Execution role for ${local.name}-api: own log group, one Bedrock model, the job table, and the worker."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role" "worker" {
  name_prefix        = "${local.name}-worker-"
  description        = "Execution role for ${local.name}-worker: own log group, one Bedrock model, job writes, and the failure queue."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# ── logs ──────────────────────────────────────────────────────────────────────
#
# Deliberately *not* the AWS-managed AWSLambdaBasicExecutionRole.
#
# That policy grants logs:CreateLogGroup on "*" — permission to create a log
# group anywhere in the account, with no retention — which is precisely the
# outcome logs.tf exists to prevent. Here the actions are the two a function
# actually performs, on the one group it actually writes to.
#
# The `:*` suffix on the resource is required, not decorative: PutLogEvents acts
# on log *streams*, whose ARNs are the group ARN followed by a stream segment.
# Granting the bare group ARN would deny every write.

data "aws_iam_policy_document" "logs" {
  for_each = local.functions

  statement {
    sid    = "WriteOwnLogStreams"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.functions[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "api_logs" {
  name   = "logs"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.logs["api"].json
}

resource "aws_iam_role_policy" "worker_logs" {
  name   = "logs"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.logs["worker"].json
}

# ── the model ─────────────────────────────────────────────────────────────────

# The same grant the EC2 instance role gets, from the same module, so no
# deployment target and no function can drift into a different level of access
# to the model.
module "bedrock_access" {
  source = "../modules/bedrock_access"

  bedrock_model_id = var.bedrock_model_id
  bedrock_region   = var.bedrock_region
}

# Attached even when reply_source is "canned". The canned generator makes no AWS
# call, so the permission is unused rather than harmful — and making a role's
# shape depend on a reply-source flag would mean flipping to `bedrock` requires a
# Terraform apply as well as an environment change, which is a worse trade than
# an unused statement.
#
# Both functions get it because both generate replies: the api function for the
# streamed transport, the worker for the asynchronous one.
resource "aws_iam_role_policy" "bedrock_invoke" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.api.id
  policy = module.bedrock_access.policy_json
}

resource "aws_iam_role_policy" "worker_bedrock_invoke" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.worker.id
  policy = module.bedrock_access.policy_json
}

# ── the job table ─────────────────────────────────────────────────────────────

# What the api function does with a job: create it, read it back for a polling
# client, and update it to cancel.
#
# No DeleteItem: nothing deletes a job, because the TTL does. No Query or Scan:
# every access is by primary key, and a caller holding one job id has no
# business enumerating the rest.
data "aws_iam_policy_document" "api_jobs" {
  statement {
    sid    = "SubmitReadAndCancelJobs"
    effect = "Allow"

    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      # Cancellation. Easy to think this belongs only to the worker — the
      # cancel endpoint is served here, and removing it would break the Stop
      # button and, with it, the only way to stop paying for an abandoned reply.
      "dynamodb:UpdateItem",
    ]

    resources = [aws_dynamodb_table.jobs.arn]
  }
}

resource "aws_iam_role_policy" "api_jobs" {
  name   = "jobs-table"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_jobs.json
}

# What the worker does with a job: update it. That is the whole list.
#
# **No GetItem, and its absence is a designed property rather than an
# oversight.** Every step the worker takes is a conditional update — claim,
# append, finish, fail — and each one learns what it needs from whether the
# condition held, with the prior item returned on failure by the same call. So
# the worker never reads, which means it cannot read a reply it did not write,
# and cannot read another job at all.
data "aws_iam_policy_document" "worker_jobs" {
  statement {
    sid    = "WriteRepliesOnly"
    effect = "Allow"

    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.jobs.arn]
  }
}

resource "aws_iam_role_policy" "worker_jobs" {
  name   = "jobs-table"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_jobs.json
}

# ── the hand-off ──────────────────────────────────────────────────────────────

# Scoped to the worker's ARN, not "*". An asynchronous invocation is a way to
# spend money, so the api function may start exactly one thing.
data "aws_iam_policy_document" "api_invoke_worker" {
  statement {
    sid    = "DispatchJobsToTheWorker"
    effect = "Allow"

    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn]
  }
}

resource "aws_iam_role_policy" "api_invoke_worker" {
  name   = "invoke-worker"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_invoke_worker.json
}

# ── the failure destination ───────────────────────────────────────────────────

# Easy to miss, and it fails silently when missed.
#
# Lambda delivers a failure record using the *function's own execution role*,
# not a service principal of its own. Without this statement the destination is
# configured, looks correct in the console, and delivers nothing — the only
# evidence being a DestinationDeliveryFailures metric, which is why alerts.tf
# watches that metric specifically.
data "aws_iam_policy_document" "worker_failures" {
  statement {
    sid    = "ArchiveFailedJobs"
    effect = "Allow"

    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.worker_failures.arn]
  }
}

resource "aws_iam_role_policy" "worker_failures" {
  name   = "failure-destination"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_failures.json
}
