# The API, as a container-image function.
#
# The image is deploy/api.Dockerfile's `lambda` stage: the same venv, the same
# application code and the same `uvicorn app.main:app` CMD that EC2 runs, plus
# the Lambda Web Adapter extension. Nothing in backend/ knows it is on Lambda —
# no handler, no Mangum, no ASGI shim — which is what makes "one codebase, two
# targets" true rather than aspirational.

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  description   = "Streams chat replies over SSE. Same image and same process as the EC2 deployment."
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
    log_group  = aws_cloudwatch_log_group.api.name
  }

  environment {
    variables = local.environment
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
    aws_cloudwatch_log_group.api,
    aws_iam_role_policy.api_logs,
    aws_ecr_repository_policy.api,
  ]

  tags = { Name = "${local.name}-api" }
}
