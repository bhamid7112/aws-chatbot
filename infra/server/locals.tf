locals {
  name = var.project

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
  }

  # Where the repository is cloned and the stack is run from, on the instance.
  app_dir = "/opt/${var.project}"

  # Appended to the CLI commands in outputs.tf so they are runnable as printed.
  # Without it a copied command authenticates as the default profile, which on a
  # workstation with several profiles is either a permission error or — worse — the
  # wrong account.
  cli_profile_flag = var.aws_profile != "" ? " --profile ${var.aws_profile}" : ""

  docker_compose_url = (
    var.docker_compose_version == ""
    ? "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64"
    : "https://github.com/docker/compose/releases/download/${var.docker_compose_version}/docker-compose-linux-x86_64"
  )
}
