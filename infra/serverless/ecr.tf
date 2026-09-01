# Where the api image lives.
#
# This registry is the biggest structural change from ../server. There, the
# instance clones the repository and builds both images itself, so a release
# passes through no registry and needs no credential. Here the build moves to the
# operator's workstation and its output has to be stored somewhere Lambda can
# read — which is this repository, and which is why Docker and buildx become
# prerequisites where Terraform and the AWS CLI used to suffice.

resource "aws_ecr_repository" "api" {
  name = "${local.name}-api"

  # Tags are git commit SHAs, so a tag already names exactly one build. Making
  # them immutable turns that convention into a guarantee: tag and digest become
  # interchangeable, and nobody can move :abc1234 to different bytes after the
  # fact. It also means a release script can safely refuse to overwrite.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # Without this, `terraform destroy` fails on a repository that still holds
  # images — which after the first release it always does. The alternative is
  # emptying the repository by hand before every destroy, which turns a teardown
  # into a two-step manual process for no gain: the images are rebuildable from
  # git, so nothing here is a durable artifact worth protecting.
  force_delete = true

  tags = { Name = "${local.name}-api" }
}

# buildx leaves untagged manifests behind on every push, and they are pure cost.
# The tagged limit is a safety margin for rollback: ten releases back is far more
# than anyone will reach for, and far less than unbounded growth.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after one day."
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the ten most recent tagged images."
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      },
    ]
  })
}

# Granting Lambda pull access *explicitly*, even though Lambda would arrange this
# itself.
#
# On function create, Lambda adds its own statement to the repository policy if
# one is missing. That works, and then shows up forever as drift in a resource
# Terraform owns: every subsequent plan proposes removing a statement Terraform
# did not write, and every apply provokes Lambda to add it back. Writing the
# statement here ends that loop.
#
# Scoped by SourceArn to functions in this account, so the grant cannot be used
# by another account's function that happens to know the repository URI.
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "ecr_lambda_pull" {
  statement {
    sid    = "AllowLambdaToPull"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    # The exhaustive list of what pulling an image needs. Notably not
    # ecr:GetAuthorizationToken, which is an account-level action and cannot be
    # granted by a repository policy.
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_ecr_repository_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy     = data.aws_iam_policy_document.ecr_lambda_pull.json
}
