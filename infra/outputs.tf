# Outputs are the handoff between `terraform apply` and everything done by hand
# afterwards — verification, a shell on the box, the next release. Each one is
# something a person actually needs; anything discoverable from the console is
# left out.

output "site_url" {
  description = "The application. No -k and no domain name: the padlock is a genuine Let's Encrypt certificate issued for this address."
  value       = "https://${aws_eip.web.public_ip}"
}

output "public_ip" {
  description = "The Elastic IP. Stable across reboots, stop/starts and redeploys, which is what allows a certificate to be issued for it."
  value       = aws_eip.web.public_ip
}

output "instance_id" {
  description = "Instance ID, needed for every SSM command below."
  value       = aws_instance.app.id
}

output "deployed_source" {
  description = "Repository and ref the instance builds from. A release is a push to this ref followed by redeploy_command."
  value       = "${var.repo_url}@${var.git_ref}"
}

output "ssm_session_command" {
  description = "Open a root-capable shell on the instance. There is no SSH and no key pair; this is the only way in."
  value       = "aws ssm start-session --target ${aws_instance.app.id} --region ${var.aws_region}"
}

output "redeploy_command" {
  description = "Ship the current head of the deployed ref: re-runs the same script user_data ran at first boot. Push first; Terraform is not involved in a release."
  value = join(" ", [
    "aws ssm send-command",
    "--document-name AWS-RunShellScript",
    "--targets Key=InstanceIds,Values=${aws_instance.app.id}",
    "--parameters commands=/usr/local/bin/aws-chatbot-deploy",
    "--region ${var.aws_region}",
  ])
}

output "bootstrap_log_command" {
  description = "Follow the first boot from a local terminal. Certificate issuance is 30-60s after the stack starts."
  value = join(" ", [
    "aws ssm start-session --target ${aws_instance.app.id} --region ${var.aws_region}",
    "--document-name AWS-StartInteractiveCommand",
    "--parameters command='sudo tail -f /var/log/aws-chatbot-bootstrap.log'",
  ])
}
