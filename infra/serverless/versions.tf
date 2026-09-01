# Version constraints for the serverless stack.
#
# State is local by design, and per-stack. There is one operator and one
# environment, and a remote backend would need its own bucket and lock table
# created *before* this configuration can run — the bootstrapping problem this
# deployment does not need. `terraform.tfstate` in this directory is gitignored;
# it is also the only record of what exists, so back it up before anything
# destructive.
#
# Separate state from ../server is the point of the two-root layout: it is what
# makes "destroy one target without touching the other" a plain `terraform
# destroy` rather than a -target exercise, and it means neither stack can be
# broken by a mistake in the other.
#
# No us-east-1 provider alias, and that is worth noting because CloudFront
# usually needs one: ACM certificates for a distribution must live in us-east-1.
# This stack uses `cloudfront_default_certificate`, so there is no certificate to
# place anywhere. The day a custom domain arrives, an aliased provider comes with
# it.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.x is the current major. Pinned to it rather than to a patch so
      # `terraform init -upgrade` can pick up fixes, while a breaking 7.0 cannot
      # arrive unannounced. Same constraint as ../server, so both roots and the
      # shared module agree.
      version = "~> 6.0"
    }
  }
}
