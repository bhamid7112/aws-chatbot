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
  description = "The api function, for log tailing and for reaching one function by name."
  value       = aws_lambda_function.api.function_name
}

output "worker_function_name" {
  description = "The worker function. It has no HTTP address at all — an asynchronous invocation from the api function is the only way in — so this name is the only handle on it."
  value       = aws_lambda_function.worker.function_name
}

output "function_names" {
  description = <<-EOT
    Every function one release must update, space-separated so a shell can split
    it after `terraform output -raw`.

    A list would be tidier and unusable: `output -raw` refuses a list, and the
    release script reads its settings this way precisely so it cannot keep its
    own copy of which functions exist. Adding a third function should change the
    release, and this is how it does.

    Leaving one behind is the worst failure this stack can produce — the api
    half looks entirely correct while every job fails on a payload the worker
    cannot parse — which is why the script iterates rather than naming one.
  EOT
  value       = join(" ", [aws_lambda_function.api.function_name, aws_lambda_function.worker.function_name])
}

output "jobs_table_name" {
  description = "Table holding asynchronous replies. Naming it in the functions' environment is what switches the transport on; an empty value there mounts no job routes and /api/health stops advertising it."
  value       = aws_dynamodb_table.jobs.name
}

output "worker_failures_queue_url" {
  description = "Queue holding jobs that failed outright. Nothing consumes it — it is an archive with an alarm on its depth, and the only place a failed job's request can be read back."
  value       = aws_sqs_queue.worker_failures.url
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
  description = "Follow the api function's logs. The REPORT lines carry Init Duration, which is the number to look at before tuning memory_size_mb."
  value       = "aws logs tail ${aws_cloudwatch_log_group.functions["api"].name} --follow --region ${var.aws_region}${local.cli_profile_flag}"
}

output "worker_log_command" {
  description = <<-EOT
    Follow the worker's logs — the only window onto the asynchronous path, since
    nothing about a job reaches the browser except its status.

    Its lines carry the job id, so a single job can be followed end to end:
    claimed, then done, failed or stopped.
  EOT
  value       = "aws logs tail ${aws_cloudwatch_log_group.functions["worker"].name} --follow --region ${var.aws_region}${local.cli_profile_flag}"
}

output "deployed_image_command" {
  description = <<-EOT
    Which image bytes are actually running — for *every* function, in one
    command, because the interesting question is whether they agree.

    This replaces `git -C /opt/aws-chatbot log -1` from the EC2 target, which has
    no equivalent here: there is no host to inspect and the deployed revision is
    no longer discoverable on a box. The truth is an image digest.

    Two digests that differ mean a release updated one function and not the
    other, which presents as every job failing while the API looks healthy. It
    is the first thing to check when that happens.
  EOT
  value       = "for f in ${aws_lambda_function.api.function_name} ${aws_lambda_function.worker.function_name}; do aws lambda get-function --function-name $f --query 'Code.ImageUri' --output text --region ${var.aws_region}${local.cli_profile_flag}; done"
}

output "worker_failures_read_command" {
  description = <<-EOT
    Read a failed job without removing it from the queue.

    The record carries the original request, so this is also the fastest way to
    reproduce a failure locally. Note it contains the prompt and the
    conversation history — the one place this design leaves them at rest.

    Reading does not delete: the message returns after its visibility timeout.
    Use `aws sqs delete-message` with the receipt handle once it is dealt with,
    or leave it to expire after worker_failure_retention_seconds.
  EOT
  value       = "aws sqs receive-message --queue-url ${aws_sqs_queue.worker_failures.url} --max-number-of-messages 10 --region ${var.aws_region}${local.cli_profile_flag}"
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
  value       = "aws cloudwatch set-alarm-state --alarm-name ${aws_cloudwatch_metric_alarm.log_errors["api"].alarm_name} --state-value ALARM --state-reason 'verifying alert delivery' --region ${var.aws_region}${local.cli_profile_flag}"
}

output "bedrock_model_arn" {
  description = "The single foundation model the execution role can invoke. Printed so a permission error can be checked against what was actually granted."
  value       = module.bedrock_access.model_arn
}
