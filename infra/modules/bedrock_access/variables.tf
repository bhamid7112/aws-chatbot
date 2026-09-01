# The two inputs that decide which model the caller may invoke. Both are
# deliberately required: a default here would let a stack silently grant access
# to a model nobody chose.

variable "bedrock_model_id" {
  description = <<-EOT
    The Bedrock foundation model the caller is allowed to invoke, e.g.
    `google.gemma-3-27b-it`. This is the same value the application reads as
    CHAT_BEDROCK_MODEL_ID, and the two must agree — the role grants exactly one
    model, so a mismatch is an AccessDeniedException at the first chat rather
    than a permission that quietly covers more than intended.
  EOT
  type        = string

  validation {
    condition     = length(trimspace(var.bedrock_model_id)) > 0
    error_message = "bedrock_model_id must name a model."
  }

  validation {
    # A wildcard would defeat the entire point of this module.
    condition     = !can(regex("[*?]", var.bedrock_model_id))
    error_message = "bedrock_model_id must be a literal model id, not a pattern: this policy is meant to name exactly one model."
  }
}

variable "bedrock_region" {
  description = <<-EOT
    Region whose Bedrock endpoint the caller uses. It appears in the ARN, so it
    scopes the grant to one region as well as one model.

    Note that `google.gemma-3-27b-it` has no cross-region inference profile —
    there is no `us.`-prefixed variant — so this must be a region that actually
    offers the model rather than merely a nearby one.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.bedrock_region))
    error_message = "bedrock_region must be a region name like us-east-2."
  }
}
