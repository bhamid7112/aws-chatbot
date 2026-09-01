# The function's identity, and — as on ../server — the *entire* production
# credential story. There is no API key, no IAM user and no secret in the image
# or the environment: boto3 reads short-lived credentials Lambda injects for this
# role, they rotate on their own, and every call is attributable to this role in
# CloudTrail.
#
# Two capabilities, and the list is exhaustive:
#
#   1. write to its own log group;
#   2. invoke one Bedrock model.
#
# Nothing here can read or write S3, read a secret, or describe the account. Note
# what is absent besides: no VPC access policy, because the function is not in a
# VPC — it calls only public AWS endpoints, so there is no NAT gateway and no
# interface endpoint to pay for, and a VPC would break Function URL response
# streaming outright.

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
  description        = "Execution role for ${local.name}-api: own log group and one Bedrock model."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Deliberately *not* the AWS-managed AWSLambdaBasicExecutionRole.
#
# That policy grants logs:CreateLogGroup on "*" — permission to create a log
# group anywhere in the account, with no retention — which is precisely the
# outcome logs.tf exists to prevent. Here the actions are the two the function
# actually performs, on the one group it actually writes to.
#
# The `:*` suffix on the resource is required, not decorative: PutLogEvents acts
# on log *streams*, whose ARNs are the group ARN followed by a stream segment.
# Granting the bare group ARN would deny every write.
data "aws_iam_policy_document" "api_logs" {
  statement {
    sid    = "WriteOwnLogStreams"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]

    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "api_logs" {
  name   = "logs"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_logs.json
}

# The same grant the EC2 instance role gets, from the same module, so the two
# deployment targets cannot drift into different levels of access to the model.
module "bedrock_access" {
  source = "../modules/bedrock_access"

  bedrock_model_id = var.bedrock_model_id
  bedrock_region   = var.bedrock_region
}

# Attached even when reply_source is "canned". The canned generator makes no AWS
# call, so the permission is unused rather than harmful — and making the role's
# shape depend on a reply-source flag would mean flipping to `bedrock` requires a
# Terraform apply as well as an environment change, which is a worse trade than
# an unused statement.
resource "aws_iam_role_policy" "bedrock_invoke" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.api.id
  policy = module.bedrock_access.policy_json
}
