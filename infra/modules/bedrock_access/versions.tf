# A module declares which providers it needs but never configures one — the
# provider comes from whichever root stack calls it, so the same module works for
# the EC2 stack in one region and the serverless stack in another.
#
# The constraint matches both roots rather than being looser: a module that
# accepts a wider range than its callers is claiming a compatibility nobody has
# tested.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
