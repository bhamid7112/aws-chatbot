# The instance's identity. Two capabilities, and the list is exhaustive:
#
#   1. be an SSM managed node, which is what makes Session Manager and Run Command
#      work — and therefore what makes "no SSH, no key pair, no port 22" a
#      complete answer rather than a gap;
#   2. invoke one Bedrock model, which is how the API answers a chat.
#
# Nothing here can read or write S3, read a secret, or describe the account. The
# application source arrives by `git clone` from a public repository, which needs
# no AWS permission and no credential at all.
#
# This role is also the *entire* production credential story. There is no API key,
# no IAM user and no secret on the instance: the container reads short-lived
# credentials from IMDS, they rotate on their own, and every call is attributable
# to this role in CloudTrail.

data "aws_iam_policy_document" "assume_role" {
  statement {
    sid     = "AllowEC2ToAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name_prefix        = "${local.name}-instance-"
  description        = "Instance role for ${local.name}: SSM managed node access only."
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

# AWS-managed rather than hand-written: this policy is the contract the SSM agent
# is developed against, and it changes when the agent gains a capability. A
# hand-copied equivalent would drift and fail in ways that look like networking
# faults.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name_prefix = "${local.name}-instance-"
  role        = aws_iam_role.instance.name
}

# The one thing the application itself is allowed to do.
#
# Scoped to a single model ARN rather than "bedrock:InvokeModel on *", and that
# narrowness is doing real work: the metadata hop limit had to be raised to 2 for
# a container to reach IMDS at all (see compute.tf), so this policy is what bounds
# what a compromised container could actually do with the role. Running inference
# on one model is a bill; listing buckets or reading secrets would be a breach.
#
# Both actions are needed. The API streams, so it calls InvokeModelWithResponseStream
# via Converse's streaming variant; InvokeModel covers the non-streaming path and
# any future use that does not stream.
#
# No region in the ARN, and no account: foundation-model ARNs are
# arn:aws:bedrock:<region>::foundation-model/<id> — note the empty account field —
# and pinning the region here would silently break the day bedrock_region changes.
data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    sid    = "InvokeOneFoundationModel"
    effect = "Allow"

    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]

    resources = [
      "arn:aws:bedrock:${var.bedrock_region}::foundation-model/${var.bedrock_model_id}",
    ]
  }
}

# Inline rather than a managed policy: it exists only for this role, has no reuse
# value, and being inline means it cannot outlive the role or be attached
# elsewhere by accident.
resource "aws_iam_role_policy" "bedrock_invoke" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}
