# Permission to invoke exactly one Bedrock foundation model.
#
# Extracted from the EC2 stack's iam.tf because both deployment targets need the
# identical grant — the EC2 instance role and the Lambda execution role — and the
# reasoning behind it is worth stating once rather than twice. The module emits a
# policy *document* rather than an IAM resource: the caller decides whether it
# becomes an inline role policy, a managed policy, or part of a larger one, and
# both current callers attach it inline.
#
# Nothing here calls AWS. `aws_iam_policy_document` is rendered locally by the
# provider, so this module adds no API call and no latency to a plan.
#
# ── Why the policy is shaped this way ────────────────────────────────────────
#
# Scoped to a single model ARN rather than "bedrock:InvokeModel on *", and that
# narrowness is doing real work. On EC2 the metadata hop limit had to be raised
# to 2 for a container to reach IMDS at all, so this policy is what bounds what a
# compromised container could actually do with the role. Running inference on one
# model is a bill; listing buckets or reading secrets would be a breach. On
# Lambda the same argument holds against a compromised dependency.
#
# Both actions are needed. The API streams, so it calls
# InvokeModelWithResponseStream via Converse's streaming variant; InvokeModel
# covers the non-streaming path and any future use that does not stream.
#
# No account field in the ARN, and that is not an omission: foundation-model ARNs
# are arn:aws:bedrock:<region>::foundation-model/<id> — the account segment is
# genuinely empty, because the model belongs to AWS rather than to the caller.

data "aws_iam_policy_document" "invoke" {
  statement {
    sid    = "InvokeOneFoundationModel"
    effect = "Allow"

    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]

    resources = [local.model_arn]
  }
}

locals {
  model_arn = "arn:aws:bedrock:${var.bedrock_region}::foundation-model/${var.bedrock_model_id}"
}
