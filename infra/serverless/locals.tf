locals {
  name = var.project

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
    # Distinguishes the two stacks in a shared account, where every other tag on
    # a serverless resource and an EC2 resource is identical. Cost Explorer can
    # then answer "what does each deployment target cost" without guessing from
    # resource types.
    Target = "serverless"
  }

  # Appended to the CLI commands in outputs.tf so they are runnable as printed.
  # Without it a copied command authenticates as the default profile, which on a
  # workstation with several profiles is either a permission error or — worse —
  # the wrong account.
  cli_profile_flag = var.aws_profile != "" ? " --profile ${var.aws_profile}" : ""

  # The two functions this stack runs, both from the same image.
  #
  # Written as a map because everything mechanical about a function — its log
  # group, its metric filter, its two alarms — is identical in shape and differs
  # only in which function it names. Iterating one map is what stops the api
  # half from being alerted on while the worker half quietly is not.
  #
  # The names are plain strings, deliberately not references to the function
  # resources: the log groups must be created *before* the functions, and a
  # group whose name depended on the function it belongs to could not be.
  #
  # `metric_name` is per function because a metric is identified by name and
  # namespace together; keeping the api's spelling unchanged means this
  # refactoring does not orphan the metric its alarm has been watching.
  functions = {
    api = {
      name        = "${local.name}-api"
      metric_name = "ApiErrorCount"
    }
    worker = {
      name        = "${local.name}-worker"
      metric_name = "WorkerErrorCount"
    }
  }

  # A custom namespace per function rather than AWS/Lambda: that one is
  # reserved, and PutMetricFilter into it is rejected.
  error_metric_namespace = { for key in keys(local.functions) : key => "${local.name}/${key}" }

  # How long after submission a job is presumed lost.
  #
  # Computed rather than configured, because it is not an independent choice: it
  # has to outlast everything that can legitimately delay a reply. Lambda may
  # spend up to `worker_event_age_seconds` retrying delivery, the worker may then
  # run for `worker_timeout_seconds`, and the sum needs slack for the clock skew
  # between whoever wrote the deadline and whoever reads it.
  #
  # Set below that sum and a slow-but-healthy job is reported as failed while it
  # is still running. Deriving it means the three numbers cannot disagree.
  job_deadline_seconds = var.worker_event_age_seconds + var.worker_timeout_seconds + 60

  # Settings both functions must agree on, byte for byte.
  #
  # Two rules govern what is allowed in here, and both are absolute:
  #
  #   1. No credential, ever. boto3's default chain resolves the execution role,
  #      exactly as backend/app/infrastructure/config.py's docstring promises.
  #   2. Never AWS_REGION — it is reserved, and Lambda rejects a function that
  #      sets it. The application reads CHAT_BEDROCK_REGION instead, which is why
  #      that setting exists separately. The job table and the worker are always
  #      in the function's own region, so boto3 resolves those from AWS_REGION,
  #      which Lambda sets for us.
  #
  # Merged into each function's environment rather than copied, and the reason is
  # a specific bug rather than tidiness. Submit validates a prompt with the api's
  # CHAT_MAX_PROMPT_CHARS and the worker re-validates it with its own; if the two
  # ever differed, a prompt the API accepted would be rejected by the worker, and
  # a reply nobody could explain would fail. The same goes for every Bedrock
  # setting: the two functions must be answering with the same model, on the same
  # terms. `merge` makes divergence impossible rather than unlikely.
  common_environment = {
    # Lets the adapter report readiness before uvicorn has finished importing,
    # moving part of a slow Python init out of the request's billed latency.
    AWS_LWA_ASYNC_INIT = "true"

    # Empty, exactly as on EC2: CloudFront makes the browser's origin and the
    # API's origin the same one, so no CORS middleware is installed at all.
    # config.py's _read_csv distinguishes present-but-empty from absent, and this
    # is the case it exists for.
    CHAT_CORS_ALLOW_ORIGINS = ""

    CHAT_REPLY_SOURCE       = var.reply_source
    CHAT_WORD_DELAY_SECONDS = tostring(var.word_delay_seconds)
    CHAT_MAX_PROMPT_CHARS   = tostring(var.max_prompt_chars)

    CHAT_BEDROCK_MODEL_ID             = var.bedrock_model_id
    CHAT_BEDROCK_REGION               = var.bedrock_region
    CHAT_BEDROCK_MAX_OUTPUT_TOKENS    = tostring(var.bedrock_max_output_tokens)
    CHAT_BEDROCK_TEMPERATURE          = tostring(var.bedrock_temperature)
    CHAT_BEDROCK_READ_TIMEOUT_SECONDS = tostring(var.bedrock_read_timeout_seconds)
    CHAT_MAX_HISTORY_MESSAGES         = tostring(var.max_history_messages)

    # Naming the table is what switches the asynchronous transport on: the
    # application derives everything else from whether this is set, including
    # whether the job routes are mounted at all and what /api/health advertises.
    CHAT_JOBS_TABLE            = aws_dynamodb_table.jobs.name
    CHAT_JOB_DEADLINE_SECONDS  = tostring(local.job_deadline_seconds)
    CHAT_JOB_RETENTION_SECONDS = tostring(var.job_retention_seconds)
  }

  api_environment = merge(local.common_environment, {
    # Must agree with aws_lambda_function_url.api.invoke_mode. If the two
    # disagree the response is buffered and returned whole, which looks exactly
    # like CloudFront buffering — so they are set from the same place on purpose.
    AWS_LWA_INVOKE_MODE = "response_stream"

    CHAT_ROLE = "api"

    # Where submitted jobs are sent. The permission to do so is granted
    # separately in iam.tf against the same function.
    CHAT_WORKER_FUNCTION_NAME = local.functions.worker.name
  })

  worker_environment = merge(local.common_environment, {
    CHAT_ROLE = "worker"

    # Deliberately no AWS_LWA_INVOKE_MODE: the worker returns a small
    # acknowledgement, not a stream, and buffered is the adapter's default.

    # **This one variable is what makes every failure path in this stack work.**
    #
    # The adapter reports whatever the application returns as a *successful*
    # invocation unless told otherwise — 500 included. Left unset, an unhandled
    # error in the worker is reported to Lambda as a job well done: no retry, no
    # record on the failure queue, and no alarm. The alerting in alerts.tf would
    # be silent for exactly the failures it exists to catch.
    #
    # Three ranges, for three different situations:
    #
    #   500-599  a defect in the application — retry it, then archive it
    #   422      an event the worker cannot parse. It will never succeed, so the
    #            two retries are wasted but harmless, and archiving it is how
    #            anyone finds out the two functions disagree about the payload
    #   404      the worker's own route is not mounted, which means CHAT_ROLE is
    #            wrong. Silent success here would look exactly like a job that
    #            ran, so it has to be loud
    AWS_LWA_ERROR_STATUS_CODES = "404,422,500-599"
  })
}
