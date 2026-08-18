# The instance, and the Elastic IP whose address becomes the certificate's name.

data "aws_ssm_parameter" "ami" {
  name = var.ami_ssm_parameter
}

# Declared independently of the instance, and that independence is the whole
# trick.
#
# The certificate is issued for an IP address, so the address has to be known
# before the software that requests it starts — it goes into user_data. An EIP
# created *by* the instance (or one whose association Terraform reads back from
# it) cannot provide that: user_data would depend on the instance, and the
# instance on its own user_data. Allocating the address as a standalone resource
# breaks the cycle, so a single apply produces a host that already knows which
# address it will answer on.
resource "aws_eip" "web" {
  domain = "vpc"

  tags = { Name = local.name }
}

resource "aws_instance" "app" {
  # nonsensitive(): the provider marks every SSM parameter value sensitive, which
  # is right in general and unhelpful for a public AMI ID — it would redact the
  # one field worth reading in `terraform plan`.
  ami           = nonsensitive(data.aws_ssm_parameter.ami.value)
  instance_type = var.instance_type

  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    app_dir            = local.app_dir
    repo_url           = var.repo_url
    git_ref            = var.git_ref
    acme_email         = var.acme_email
    site_address       = aws_eip.web.public_ip
    swap_size_gb       = var.swap_size_gb
    docker_compose_url = local.docker_compose_url

    # Passed through rather than resolved into a URL here: the buildx asset's
    # filename embeds its version, so an empty value has to be resolved on the
    # instance at boot.
    docker_buildx_version = var.docker_buildx_version
  })

  # Changing user_data must not build a new host: this one holds the Elastic IP,
  # the Caddy certificate volume and the Docker cache. With replacement off, the
  # provider stops the instance, updates the data and starts it again — visible in
  # the plan, disruptive for a minute, and non-destructive. The Elastic IP
  # association survives a stop/start.
  #
  # Redeploying the *application* changes nothing here at all: user_data names a
  # repository and a ref, not a revision, so every release leaves it
  # byte-identical and Terraform has no part in shipping code.
  user_data_replace_on_change = false

  metadata_options {
    http_endpoint = "enabled"
    # IMDSv2 only. Session-oriented, so the SSRF-style request that trivially
    # reads credentials from IMDSv1 does not work.
    http_tokens = "required"
    # Deliberately 1, the default. user_data queries IMDS from the host, which
    # this allows, while one hop is not enough to reach it from inside a
    # container on the bridge network — so neither the API nor Caddy can read the
    # instance role's credentials, and neither has any reason to.
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_size_gb
    encrypted   = true
    # This disk holds no state worth keeping: the certificate and the ACME account
    # key live in a Docker volume on it, and both are re-obtainable. Anything that
    # must outlive the instance belongs in S3.
    delete_on_termination = true

    tags = { Name = "${local.name}-root" }
  }

  lifecycle {
    # `ami` resolves to "latest Amazon Linux 2023", so it changes whenever Amazon
    # publishes a release — and an unignored change here means destroy-and-replace
    # of a healthy host on an unrelated apply, losing the certificate and the
    # image cache to a plan nobody read closely. Rebuilding on a newer AMI stays
    # possible and stays explicit: `terraform taint`, or remove this line.
    ignore_changes = [ami]
  }

  # What first boot needs and Terraform cannot infer from the arguments above:
  # membership of SSM, so a failed bootstrap is still debuggable rather than a
  # black box, and a route to the internet for GitHub, the package repositories
  # and Let's Encrypt.
  depends_on = [
    aws_iam_role_policy_attachment.ssm_core,
    aws_route_table_association.public,
  ]

  tags = { Name = local.name }
}

# The association Terraform performs moments after the instance starts booting.
# user_data polls IMDS until it observes this, because requesting a certificate
# for an address that does not yet reach this host fails validation and spends
# rate limit.
resource "aws_eip_association" "web" {
  allocation_id = aws_eip.web.id
  instance_id   = aws_instance.app.id
}
