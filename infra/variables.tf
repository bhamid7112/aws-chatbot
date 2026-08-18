# Every input the deployment has. Only `acme_email` is mandatory: the defaults
# describe the deployment the plan approved, so `terraform apply` with a single
# variable set is a complete, working environment.

variable "acme_email" {
  description = <<-EOT
    Contact address registered with Let's Encrypt. It is the only channel the CA
    has to warn about expiring or failing certificates, which matters more here
    than usual: these certificates live about six days, so a renewal that starts
    failing takes the site down within one.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.acme_email))
    error_message = "acme_email must be a single, complete email address."
  }

  validation {
    # A CA cannot reach a reserved-for-documentation domain, so this would
    # silently produce an unreachable account.
    condition     = !can(regex("(?i)(example\\.(com|org|net)|\\.invalid)$", var.acme_email))
    error_message = "acme_email must be a real, deliverable address — not an example or .invalid domain."
  }
}

variable "aws_region" {
  description = "Region for every resource. The Elastic IP, and therefore the certificate's identity, lives here."
  type        = string
  default     = "us-west-1"
}

variable "aws_profile" {
  description = <<-EOT
    Named profile from ~/.aws/config to authenticate with. Empty means the
    ambient credential chain — environment variables, then the default profile,
    then an instance role — which is what CI wants and what a machine with one
    set of credentials already does.

    Setting it earns its place on a workstation holding several profiles for
    several accounts: it pins which account this configuration builds in, so the
    answer is in terraform.tfvars and reviewable, rather than in whatever
    AWS_PROFILE happened to be exported in the shell that ran apply. Creating
    this deployment in the wrong account is not an error Terraform can catch.
  EOT
  type        = string
  default     = ""
}

variable "project" {
  description = "Name prefix and Project tag for everything this configuration creates."
  type        = string
  default     = "aws-chatbot"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,28}[a-z0-9]$", var.project))
    error_message = "project must be lowercase alphanumeric with hyphens, 3-30 characters, starting with a letter."
  }
}

variable "repo_url" {
  description = <<-EOT
    Public HTTPS clone URL of the application repository. The instance clones this
    and builds both images from it, so nothing about a release passes through
    Terraform and no credential is needed on the host.

    Public is a load-bearing word. If this repository is made private, cloning
    fails at the next redeploy with an authentication prompt, and the fix is a
    deploy token on the instance — read from SSM Parameter Store, not baked into
    user_data.
  EOT
  type        = string
  default     = "https://github.com/bhamid7112/aws-chatbot.git"

  validation {
    condition     = startswith(var.repo_url, "https://")
    error_message = "repo_url must be an https:// clone URL. SSH would need a key on the instance, which is what this design avoids."
  }
}

variable "git_ref" {
  description = <<-EOT
    Branch, tag or commit SHA to deploy. The fetch is by ref rather than a clone
    of a branch, so all three work — a tag or a SHA pins a release, while a branch
    means "whatever was last pushed".
  EOT
  type        = string
  default     = "main"

  validation {
    condition     = length(trimspace(var.git_ref)) > 0
    error_message = "git_ref must name a branch, tag or commit."
  }
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. t3.micro is free-tier eligible and sufficient to *serve*
    this application; the tight spot is the Vite build, which the swapfile in
    user_data covers. If builds are slow enough to annoy, t3.small is the one
    change needed — nothing else in this configuration assumes the size.
  EOT
  type        = string
  default     = "t3.micro"
}

variable "root_volume_size_gb" {
  description = "Root gp3 volume size. Holds the OS, Docker's image cache and both build contexts, so it is sized for repeated rebuilds rather than for a single image."
  type        = number
  default     = 20

  validation {
    condition     = var.root_volume_size_gb >= 12
    error_message = "root_volume_size_gb must be at least 12: below that, Docker's build cache fills the disk within a few redeploys."
  }
}

variable "swap_size_gb" {
  description = "Size of the swapfile created on first boot. Exists so `npm run build` cannot be OOM-killed on a 1 GiB instance."
  type        = number
  default     = 2

  validation {
    condition     = var.swap_size_gb >= 1 && var.swap_size_gb <= 8
    error_message = "swap_size_gb must be between 1 and 8."
  }
}

variable "allowed_web_cidrs" {
  description = <<-EOT
    IPv4 ranges allowed to reach ports 80 and 443. The default is the whole
    internet, which a public site needs — and note that port 80 cannot be
    narrowed to your own address even for a private deployment, because Let's
    Encrypt validates HTTP-01 from its own unpublished source addresses.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.allowed_web_cidrs) > 0
    error_message = "allowed_web_cidrs must contain at least one CIDR block, or nothing can reach the site."
  }
}

variable "vpc_cidr" {
  description = "CIDR for the dedicated VPC. A private range that does not need to coexist with anything, since this VPC is created and destroyed with the deployment."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet the instance lives in."
  type        = string
  default     = "10.0.1.0/24"
}

variable "ami_ssm_parameter" {
  description = <<-EOT
    Public SSM parameter naming the AMI to launch. The `-latest-` path resolves
    to whatever Amazon Linux 2023 release is current at apply time, which is why
    the instance ignores subsequent `ami` changes (see compute.tf): a fresh AMI
    must not silently replace a running host holding a certificate.
  EOT
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

variable "docker_compose_version" {
  description = <<-EOT
    Compose plugin version to install, as a release tag such as "v2.40.0".
    Amazon Linux 2023 packages the Docker engine but not the Compose plugin, so
    it is fetched from GitHub. Empty means the latest release, which keeps this
    from rotting; set a tag when a deployment needs to be reproducible.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.docker_compose_version == "" || can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.docker_compose_version))
    error_message = "docker_compose_version must be empty or a release tag like v2.40.0."
  }
}
