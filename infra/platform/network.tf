# ---------------------------------------------------------------------------
# Network: VPC with everything private.
#
#   private-a / private-b  2 AZs — host RDS, ECS tasks, pilot Lambda
#   public                 1 subnet — single NAT gateway only
#
# A NAT gateway is required (not optional) because the pilot activity Lambda
# must reach the MediaConnect control plane, which has no VPC interface
# endpoint (see STEP_FUNCTIONS_MIGRATION.md "network paths"). NAT also covers
# Secrets Manager and ECR pulls, so paying for interface endpoints as well
# would be redundant spend.
# Cost note: this NAT is the ~$33/mo line from the pricing evaluation.
# ---------------------------------------------------------------------------
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs     = slice(data.aws_availability_zones.available.names, 0, 2)
  cidrs = {
    public    = "10.40.0.0/24"
    private_a = "10.40.10.0/24"
    private_b = "10.40.20.0/24"
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = merge(local.tags, { Name = local.name })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.cidrs.public
  availability_zone       = local.azs[0]
  map_public_ip_on_launch = false # even the public subnet does not auto-assign
  tags = merge(local.tags, {
    Name = "${local.name}-public"
    tier = "public"
  })
}

resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.cidrs.private_a
  availability_zone       = local.azs[0]
  map_public_ip_on_launch = false
  tags = merge(local.tags, {
    Name = "${local.name}-private-a"
    tier = "private"
  })
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.cidrs.private_b
  availability_zone       = local.azs[1]
  map_public_ip_on_launch = false
  tags = merge(local.tags, {
    Name = "${local.name}-private-b"
    tier = "private"
  })
}

# --- IGW + NAT -------------------------------------------------------------

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = local.name })
}

resource "aws_eip" "nat" {
  domain     = "vpc"
  depends_on = [aws_internet_gateway.main]
  tags       = merge(local.tags, { Name = "${local.name}-nat" })
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  depends_on    = [aws_internet_gateway.main]
  tags          = merge(local.tags, { Name = "${local.name}-nat" })
}

# --- Routing ----------------------------------------------------------------

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = merge(local.tags, { Name = "${local.name}-public" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# One private route table per AZ (via the shared NAT), so the RDS subnet group
# and any future multi-AZ app tier sit in correctly routed subnets.
resource "aws_route_table" "private_a" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  depends_on = [aws_nat_gateway.main]
  tags       = merge(local.tags, { Name = "${local.name}-private-a" })
}

resource "aws_route_table" "private_b" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  depends_on = [aws_nat_gateway.main]
  tags       = merge(local.tags, { Name = "${local.name}-private-b" })
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.private_a.id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private_b.id
}
