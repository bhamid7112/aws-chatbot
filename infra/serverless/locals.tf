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

  # Named once because three resources need it and one of them (the log group)
  # must create it *before* the function exists.
  log_group_name = "/aws/lambda/${local.name}-api"

  # The metric the log filter publishes and the alarm watches — two resources
  # describing one metric, so the identity is written once. A custom namespace
  # rather than AWS/Lambda: that one is reserved, and PutMetricFilter into it is
  # rejected.
  error_metric_namespace = "${local.name}/api"
  error_metric_name      = "ApiErrorCount"

  # The environment the function runs with.
  #
  # Two rules govern what is allowed in here, and both are absolute:
  #
  #   1. No credential, ever. boto3's default chain resolves the execution role,
  #      exactly as backend/app/infrastructure/config.py's docstring promises.
  #   2. Never AWS_REGION — it is reserved, and Lambda rejects a function that
  #      sets it. The application reads CHAT_BEDROCK_REGION instead, which is why
  #      that setting exists separately.
  #
  # The AWS_LWA_* settings here are the deployment-shaped half; the ones that are
  # facts about the image (port, readiness path) are baked into the image by
  # deploy/api.Dockerfile so Terraform cannot contradict it.
  environment = {
    # Must agree with aws_lambda_function_url.api.invoke_mode. If the two
    # disagree the response is buffered and returned whole, which looks exactly
    # like CloudFront buffering — so they are set from the same place on purpose.
    AWS_LWA_INVOKE_MODE = "response_stream"

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

    CHAT_BEDROCK_MODEL_ID          = var.bedrock_model_id
    CHAT_BEDROCK_REGION            = var.bedrock_region
    CHAT_BEDROCK_MAX_OUTPUT_TOKENS = tostring(var.bedrock_max_output_tokens)
    CHAT_BEDROCK_TEMPERATURE       = tostring(var.bedrock_temperature)
    CHAT_MAX_HISTORY_MESSAGES      = tostring(var.max_history_messages)
  }
}
