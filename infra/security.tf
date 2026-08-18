# The security group, and nothing about SSH.
#
# There is no port 22 rule and no key pair anywhere in this configuration. Shell
# access is SSM Session Manager, which is an outbound connection from the
# instance to an AWS endpoint — so the host has no inbound administrative
# surface at all, and there is no private key whose loss matters.
#
# Rules are separate `aws_vpc_security_group_*_rule` resources rather than inline
# `ingress`/`egress` blocks. Inline blocks make the group the sole owner of its
# entire rule set, so any rule added out-of-band is silently deleted on the next
# apply and a single rule change re-computes the whole group.

resource "aws_security_group" "web" {
  # name_prefix, not name: with create_before_destroy a replacement group must
  # exist alongside the old one for a moment, and two groups cannot share a name.
  name_prefix = "${local.name}-web-"
  description = "Public web edge for ${local.name}: HTTP for ACME and redirects, HTTPS for the application."
  vpc_id      = aws_vpc.this.id

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${local.name}-web" }
}

# Port 80 is load-bearing, not a courtesy redirect. HTTP-01 is the only ACME
# challenge type that can validate an IP address identifier, so this port carries
# the initial issuance *and* every renewal for the life of the deployment. Closing
# it after the site comes up looks harmless and takes the site down roughly four
# days later.
resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each = toset(var.allowed_web_cidrs)

  security_group_id = aws_security_group.web.id
  description       = "HTTP: ACME HTTP-01 validation and the redirect to HTTPS"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80

  tags = { Name = "${local.name}-http" }
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  for_each = toset(var.allowed_web_cidrs)

  security_group_id = aws_security_group.web.id
  description       = "HTTPS: the application"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443

  tags = { Name = "${local.name}-https" }
}

# HTTP/3 is QUIC over UDP, and Caddy publishes 443/udp in docker-compose.yml.
# Without this rule the advertised Alt-Svc leads nowhere: browsers retry over
# TCP, so the only visible symptom is a slower first load on some clients.
resource "aws_vpc_security_group_ingress_rule" "https_quic" {
  for_each = toset(var.allowed_web_cidrs)

  security_group_id = aws_security_group.web.id
  description       = "HTTP/3 (QUIC)"
  cidr_ipv4         = each.value
  ip_protocol       = "udp"
  from_port         = 443
  to_port           = 443

  tags = { Name = "${local.name}-https-quic" }
}

# Unrestricted egress. The instance initiates a lot: package repositories, the
# Docker registry and npm during a build, Let's Encrypt during issuance, and the
# S3 and SSM endpoints. Narrowing this to prefix lists is possible but would need
# revisiting on every dependency change, and it protects little on a host whose
# only inbound service is the one deliberately published.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.web.id
  description       = "All outbound traffic"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"

  tags = { Name = "${local.name}-egress" }
}
