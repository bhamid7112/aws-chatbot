# A dedicated VPC rather than the account's default one.
#
# The default VPC is convenient right up to the point where it is missing, shared
# with something else, or differently configured in another region — and it is
# never deleted by `terraform destroy`, so a deployment built on it leaves the
# question "is this account clean now?" unanswerable. Everything below is created
# and destroyed with the application, which is worth four extra resources.

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

# Not every availability zone offers every instance type, and the mismatch
# surfaces only at launch as an Unsupported error naming neither the zone nor a
# usable alternative. Asking which zones actually offer this type — rather than
# taking the first zone and hoping — is what makes `us-west-1` and `t3.micro`
# work together without a manual retry.
data "aws_ec2_instance_type_offerings" "supported" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }

  filter {
    name   = "location"
    values = data.aws_availability_zones.available.names
  }
}

locals {
  # Sorted, so the chosen zone is stable across applies and a re-plan never
  # proposes moving the subnet.
  availability_zone = sort(data.aws_ec2_instance_type_offerings.supported.locations)[0]
}

resource "aws_vpc" "this" {
  cidr_block = var.vpc_cidr

  # Both are needed for the instance to resolve names at all — package
  # repositories, the Docker registry, GitHub, and the S3 and SSM endpoints the
  # instance role talks to.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = local.name }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.public_subnet_cidr
  availability_zone = local.availability_zone

  # The instance needs a public address the moment it boots, before Terraform has
  # associated the Elastic IP: without one it cannot reach S3 to fetch the
  # artifact or SSM to register itself. The auto-assigned address is transient —
  # associating the Elastic IP replaces it — and that swap is precisely the race
  # user_data waits out before requesting a certificate.
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
