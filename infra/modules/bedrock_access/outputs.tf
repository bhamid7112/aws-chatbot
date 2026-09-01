output "policy_json" {
  description = <<-EOT
    The policy document, ready to attach. Both callers pass it straight to an
    `aws_iam_role_policy` — inline rather than managed, because it exists only
    for that one role, has no reuse value beyond this module, and being inline
    means it cannot outlive the role or be attached elsewhere by accident.
  EOT
  value       = data.aws_iam_policy_document.invoke.json
}

output "model_arn" {
  description = "The single foundation-model ARN this policy grants. Exposed for outputs and for error messages that need to say which model a role can reach."
  value       = local.model_arn
}
