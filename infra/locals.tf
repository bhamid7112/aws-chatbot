locals {
  name = var.project

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
  }

  # Where the repository is cloned and the stack is run from, on the instance.
  app_dir = "/opt/${var.project}"

  docker_compose_url = (
    var.docker_compose_version == ""
    ? "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64"
    : "https://github.com/docker/compose/releases/download/${var.docker_compose_version}/docker-compose-linux-x86_64"
  )
}
