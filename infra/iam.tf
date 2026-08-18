# The instance's identity. One capability and no more: be an SSM managed node,
# which is what makes Session Manager and Run Command work — and therefore what
# makes "no SSH, no key pair, no port 22" a complete answer rather than a gap.
#
# Nothing here can read or write S3, describe the account, or reach any other
# resource. The application source arrives by `git clone` from a public
# repository, which needs no AWS permission and no credential at all.

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
