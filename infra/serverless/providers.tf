# Credentials are deliberately not configured here. They come from the ambient
# environment — `aws configure`, AWS_PROFILE, environment variables or an SSO
# session — so this file holds nothing account-specific and nothing secret.
# Verify what will be used before applying:
#
#   aws sts get-caller-identity
#
provider "aws" {
  region = var.aws_region

  # null rather than "" when unset: null leaves the setting genuinely absent and
  # the default credential chain intact, instead of naming a profile whose name is
  # the empty string.
  profile = var.aws_profile != "" ? var.aws_profile : null

  # Applied to every resource that supports tagging, so no individual resource
  # has to repeat them and nothing created here is ever unattributed. Resources
  # still set their own `Name` where a human-readable one helps in the console.
  default_tags {
    tags = local.tags
  }
}
