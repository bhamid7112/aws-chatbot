# Everything needed to release to, reach, and debug this deployment. Commands are
# printed with the profile flag already attached so they are runnable as-is:
# without it a copied command authenticates as the default profile, which on a
# workstation with several profiles is either a permission error or the wrong
# account.

output "site_url" {
  description = "The deployment's address — the bundle and the API behind one hostname, which is why the frontend needs no API base URL."
  value       = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "distribution_id" {
  description = "CloudFront distribution id, needed to invalidate the cache after a bundle upload."
  value       = aws_cloudfront_distribution.site.id
}

output "site_bucket" {
  description = "Bucket holding the React bundle. Private — reachable only through the distribution, so `aws s3 ls` works for the operator but there is no public URL."
  value       = aws_s3_bucket.site.id
}

output "ecr_repository_url" {
  description = "Where scripts/release-api.sh pushes the api image."
  value       = aws_ecr_repository.api.repository_url
}

# The next two exist so the release script reads its settings from the same place
# Terraform does, instead of keeping its own copies that can silently diverge.
# Both derive from variables rather than from resources, so they resolve even
# during the bootstrap apply, when only the ECR repository exists yet.

output "lambda_architecture" {
  description = "Instruction set the image must be built for. A mismatch between this and what buildx produces fails at function-create time with an architecture error."
  value       = var.lambda_architecture
}

output "aws_region" {
  description = "Region holding the registry, the function and its log group — the region every CLI call in a release needs."
  value       = var.aws_region
}

output "aws_profile" {
  description = "Profile the release should authenticate with, so a release cannot target a different account than the apply did. Empty means the ambient credential chain."
  value       = var.aws_profile
}

output "function_name" {
  description = "The api function, for update-function-code and log tailing."
  value       = aws_lambda_function.api.function_name
}

output "function_url" {
  description = <<-EOT
    The Function URL. Debug use only: it is AWS_IAM-authorized, so a plain curl
    gets 403 — reach it with a SigV4-signing client such as awscurl. Useful for
    exactly one question, which is whether a streaming problem is CloudFront's or
    the function's.
  EOT
  value       = aws_lambda_function_url.api.function_url
}

output "release_command" {
  description = "Build, push and deploy a new api image."
  value       = "./scripts/release-api.sh"
}

output "api_log_command" {
  description = "Follow the function's logs. The REPORT lines carry Init Duration, which is the number to look at before tuning memory_size_mb."
  value       = "aws logs tail ${aws_cloudwatch_log_group.api.name} --follow --region ${var.aws_region}${local.cli_profile_flag}"
}

output "deployed_image_command" {
  description = <<-EOT
    Which image bytes are actually running.

    This replaces `git -C /opt/aws-chatbot log -1` from the EC2 target, which has
    no equivalent here: there is no host to inspect and the deployed revision is
    no longer discoverable on a box. The truth is an image digest, so this is the
    only way to answer "what is deployed".
  EOT
  value       = "aws lambda get-function --function-name ${aws_lambda_function.api.function_name} --query 'Code.ImageUri' --output text --region ${var.aws_region}${local.cli_profile_flag}"
}

output "alerts_topic_arn" {
  description = <<-EOT
    Topic the two error alarms publish to. Printed because it is what a manual
    subscription needs, when `alert_email` is left empty:

      aws sns subscribe --topic-arn <this> --protocol email --notification-endpoint you@example.com
  EOT
  value       = aws_sns_topic.alerts.arn
}

output "alerts_subscription_check_command" {
  description = "Whether anyone is actually listening. A PendingConfirmation subscription delivers nothing, and that is the state Terraform leaves it in until the confirmation link is clicked."
  value       = "aws sns list-subscriptions-by-topic --topic-arn ${aws_sns_topic.alerts.arn} --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output text --region ${var.aws_region}${local.cli_profile_flag}"
}

output "alerts_test_command" {
  description = <<-EOT
    Drive the alarm into ALARM by hand to prove the topic, the subscription and
    the mail delivery all work — without having to make the function fail.

    The state is overridden, not faked: CloudWatch fires the alarm actions
    exactly as it would for a real breach, then re-evaluates against the metric
    within a minute or two and mails the recovery as well.
  EOT
  value       = "aws cloudwatch set-alarm-state --alarm-name ${aws_cloudwatch_metric_alarm.api_log_errors.alarm_name} --state-value ALARM --state-reason 'verifying alert delivery' --region ${var.aws_region}${local.cli_profile_flag}"
}

output "bedrock_model_arn" {
  description = "The single foundation model the execution role can invoke. Printed so a permission error can be checked against what was actually granted."
  value       = module.bedrock_access.model_arn
}
