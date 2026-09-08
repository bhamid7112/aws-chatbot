# The two functions, both from one container image.
#
# The image is deploy/api.Dockerfile's `lambda` stage: the same venv, the same
# application code and the same `uvicorn app.main:app` CMD that EC2 runs, plus
# the Lambda Web Adapter extension. Nothing in backend/ knows it is on Lambda —
# no handler, no Mangum, no ASGI shim — which is what makes "one codebase, two
# targets" true rather than aspirational.
#
# One image deployed twice, differing only in configuration: the api function
# serves requests, the worker generates replies out of band. They share a
# codebase, a release and — enforced by local.common_environment — a model and a
# set of prompt rules. What differs is which routes are mounted, which is
# `CHAT_ROLE`, and how long each may run.
#
# **Both are updated by one release.** scripts/release-api.sh points every
# function at the same digest, because a worker left on an older image than the
# api function is the worst failure this stack can produce: the api half looks
# entirely correct while every job fails on a payload the worker cannot parse.

resource "aws_lambda_function" "api" {
  function_name = local.functions.api.name
  description   = "Streams chat replies over SSE and accepts asynchronous jobs. Same image and same process as the EC2 deployment."
  role          = aws_iam_role.api.arn

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"

  architectures = [var.lambda_architecture]
  memory_size   = var.memory_size_mb
  timeout       = var.timeout_seconds

  # Named explicitly so Lambda uses the group logs.tf already created, with
  # retention set, instead of creating its own.
  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.functions["api"].name
  }

  environment {
    variables = local.api_environment
  }

  # No image_config block. The Dockerfile's CMD is already correct for Lambda,
  # and overriding it here would mean the image and the infrastructure disagree
  # about how to start the process. The adapter supplies the runtime interface
  # client, so there is no handler to name either.

  # The direct analogue of `ignore_changes = [ami]` in ../server/compute.tf, and
  # it preserves the same property: Terraform owns the environment, and it has no
  # part in a release.
  #
  # `image_tag` is read when the function is created and never again. Afterwards
  # scripts/release-api.sh calls update-function-code with a digest, and without
  # this block the next plan would propose reverting the function to whatever tag
  # the tfvars still names — silently rolling back production to bootstrap.
  #
  # It is also needed for a subtler reason: a release passes a digest, so the API
  # reports image_uri as ...@sha256:... where this configuration says ...:tag.
  # Those never compare equal, so every plan would show a change even with no
  # release in between.
  lifecycle {
    ignore_changes = [image_uri]
  }

  # Terraform would otherwise create the function and its log group in parallel,
  # and a function that starts first creates the group itself.
  depends_on = [
    aws_cloudwatch_log_group.functions,
    aws_iam_role_policy.api_logs,
    aws_ecr_repository_policy.api,
  ]

  tags = { Name = local.functions.api.name }
}

resource "aws_lambda_function" "worker" {
  function_name = local.functions.worker.name
  description   = "Generates chat replies out of band, writing them to the job table as they arrive."
  role          = aws_iam_role.worker.arn

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"

  architectures = [var.lambda_architecture]
  memory_size   = var.worker_memory_size_mb

  # Longer than the api function's, and it is the only thing that can stop a
  # stalled reply — see variables.tf, where the reason is that a worker blocked
  # reading from Bedrock cannot be interrupted from inside the process.
  timeout = var.worker_timeout_seconds

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.functions["worker"].name
  }

  environment {
    variables = local.worker_environment
  }

  # No aws_lambda_function_url for this one, and no permission granting anyone
  # to invoke it but the api function's role. It is reachable only by an
  # asynchronous invocation from that function; nothing routes to it from
  # CloudFront, and it has no HTTP address at all.

  # Same reasoning as the api function above: image_tag is read at creation and
  # never again, because releases update the function directly by digest.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [
    aws_cloudwatch_log_group.functions,
    aws_iam_role_policy.worker_logs,
    aws_ecr_repository_policy.api,
  ]

  tags = { Name = local.functions.worker.name }
}

# How Lambda handles a job it cannot deliver or cannot run.
#
# This resource is what replaces a queue, an event source mapping, a redrive
# policy and a visibility timeout sized against the function. Everything a queue
# was in the design to provide, Lambda's own event queue already does — and the
# one thing it does not, replay, is restored by the destination below.
resource "aws_lambda_function_event_invoke_config" "worker" {
  function_name = aws_lambda_function.worker.function_name

  # Retries are left on, and that is a decision the *application* earns rather
  # than a default left alone.
  #
  # The instinct is to set this to 0 so a failed reply is never paid for twice.
  # It would be wrong twice over. First, it governs function errors only —
  # throttles and service errors are retried regardless, bounded by the event
  # age below, so it buys nothing for the failure this deployment is most likely
  # to see. Second, claiming a job is a conditional write: a retry after the
  # worker died *having claimed* loses the claim and does nothing, while a retry
  # after it died *before* claiming — a failed init, an image pull, a cold-start
  # timeout, which on a container image is the likeliest way to die — legitimately
  # runs the job. So retries can only help, and turning them off would discard
  # free recovery from the most probable failure.
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = var.worker_event_age_seconds

  destination_config {
    on_failure {
      destination = aws_sqs_queue.worker_failures.arn
    }
  }

  # The role needs sqs:SendMessage for this to deliver anything at all — Lambda
  # writes the record as the function, not as a service. iam.tf grants it; a
  # destination without it fails silently.
  depends_on = [aws_iam_role_policy.worker_failures]
}
