# Version constraints for the whole configuration.
#
# State is local by design. There is one operator and one environment, and a
# remote backend would need its own bucket and lock table created *before* this
# configuration can run — which is exactly the bootstrapping problem a one-VM
# deployment does not need. `terraform.tfstate` in this directory is gitignored;
# it is also the only record of what exists, so back it up before anything
# destructive. To move to S3 later, add a `backend "s3"` block here and
# `terraform init -migrate-state`.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.x is the current major. Pinned to it rather than to a patch so
      # `terraform init -upgrade` can pick up fixes, while a breaking 7.0 cannot
      # arrive unannounced.
      version = "~> 6.0"
    }
  }
}
