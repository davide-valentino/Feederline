# ---------------------------------------------------------------------------
# SSM bastion for direct DB verification (no public IP, no SSH keys):
#
#   * t4g.nano sleeps at ~$0.7/mo of EBS; Session Manager relays free of
#     charge over the NAT egress path while port-forwading.
#   * Access = SSO identity + ssm:StartSession. Nothing listens publicly.
#
# Tunnel pattern (from any workstation with the AWS CLI):
#   infra/db_tunnel.sh          # opens localhost:15432 -> RDS:5432
#   psql -h localhost -p 15432 -U feederline -d feederline
# Stop the instance when done; `aws ec2 stop-instances` (cost ~0 while stopped).
# ---------------------------------------------------------------------------
data "aws_ami" "al2023_arm" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-arm64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_security_group" "bastion" {
  name        = "${local.name}-bastion"
  description = "SSM bastion instance itself (all inbound relayed by SSM)"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "NAT egress: RDS, SSM endpoints"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-bastion" })
}

resource "aws_iam_role" "bastion" {
  name               = "${local.name}-bastion"
  description        = "SSM managed instance for the development bastion"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = merge(local.tags, { Name = "${local.name}-bastion" })
}

resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  name = "${local.name}-bastion"
  role = aws_iam_role.bastion.name
}

resource "aws_instance" "bastion" {
  ami                         = data.aws_ami.al2023_arm.id
  instance_type               = "t4g.nano"
  subnet_id                   = aws_subnet.private_a.id
  vpc_security_group_ids      = [aws_security_group.bastion.id]
  associate_public_ip_address = false
  iam_instance_profile        = aws_iam_instance_profile.bastion.name

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    encrypted   = true
  }

  tags = merge(local.tags, {
    Name       = "${local.name}-bastion"
    purpose    = "db-verification-ssm-tunnel"
    keep-stopped = "true" # running is charged; stop when not in use
  })
}
