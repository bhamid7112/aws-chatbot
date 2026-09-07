# Where the React bundle lives.
#
# The bucket is private and has no website configuration: CloudFront reaches it
# through Origin Access Control, so there is no public S3 endpoint to secure, no
# bucket ACL, and no second URL that serves the site unencrypted. Everything
# below either enforces that or supports it.

resource "aws_s3_bucket" "site" {
  # bucket_prefix rather than bucket, so a globally-unique name is generated
  # without pulling in the random provider just to salt one string.
  bucket_prefix = "${local.name}-site-"

  # The bundle is reproducible from any commit, so nothing in here is a durable
  # artifact. Without this, `terraform destroy` fails on a bucket that still
  # holds objects — which after the first release it always does.
  force_destroy = true

  tags = { Name = "${local.name}-site" }
}

# All four, not the usual two. The pair that blocks *policies* matters as much as
# the pair that blocks ACLs: it prevents a future edit from granting public read
# through the bucket policy, which is how "private" buckets usually stop being
# private.
resource "aws_s3_bucket_public_access_block" "site" {
  bucket = aws_s3_bucket.site.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# BucketOwnerEnforced disables ACLs entirely, which is why this configuration
# contains no aws_s3_bucket_acl resource at all. Access is decided by the bucket
# policy alone — one mechanism to read and reason about instead of two that can
# disagree.
resource "aws_s3_bucket_ownership_controls" "site" {
  bucket = aws_s3_bucket.site.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3 rather than SSE-KMS: the content is a public web bundle, so encryption
# at rest is hygiene rather than a control protecting a secret, and KMS would add
# per-request cost and a key to manage for no benefit. bucket_key_enabled cuts
# request costs and is free to set.
resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# No versioning, deliberately. Every object here is reproducible from git, and a
# bucket that is re-synced on every release would accumulate a noncurrent version
# of each build's content-hashed assets forever — paying storage to keep copies
# of files that are already immutable and already in version control.

# What lets CloudFront — and only this distribution — read the bundle.
#
# ── Why ListBucket is here and is not incidental ──────────────────────────────
#
# Without s3:ListBucket, S3 answers a request for a missing key with 403 rather
# than 404, because it will not confirm or deny the existence of an object the
# caller cannot list. That single difference breaks the property
# deploy/caddy/Caddyfile goes out of its way to preserve: a miss under /assets/*
# must be a genuine 404, because answering with the HTML shell makes the browser
# execute index.html as JavaScript and fail with a syntax error pointing nowhere
# near the cause — exactly what a redeploy does to a tab still fetching a chunk
# from the previous build.
#
# It cannot be recovered elsewhere. CloudFront's custom_error_response is
# distribution-level rather than per-behaviour, so mapping 403 to a 404 page
# would also rewrite the API's own 403s.
data "aws_iam_policy_document" "site" {
  statement {
    sid    = "AllowCloudFrontRead"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions = ["s3:GetObject"]

    resources = ["${aws_s3_bucket.site.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }

  statement {
    sid    = "AllowCloudFrontListForAccurate404s"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions = ["s3:ListBucket"]

    # The bucket itself, not its contents: ListBucket acts on the bucket.
    resources = [aws_s3_bucket.site.arn]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = data.aws_iam_policy_document.site.json

  # The policy names the distribution's ARN, and the distribution names the
  # bucket's domain — so the bucket must exist first, the distribution second,
  # and this policy last. Terraform infers that from the references, but the
  # public-access block is a separate ordering concern: applying a policy before
  # block_public_policy is in place would leave a momentary window in which a
  # broader policy could be accepted.
  depends_on = [aws_s3_bucket_public_access_block.site]
}
