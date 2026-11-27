resource "aws_db_instance" "default" {
  allocated_storage    = 20
  storage_type         = "gp2"
  engine               = "postgres"
  engine_version       = "15.4"
  instance_class       = "db.t3.micro"
  db_name              = "buyback_tracker"
  username             = "postgres"
  password             = var.db_password # set via TF_VAR_db_password or Secrets Manager
  parameter_group_name = "default.postgres15"
  skip_final_snapshot  = true
  publicly_accessible  = true # For dev simplicity; false in prod
  
  vpc_security_group_ids = [aws_security_group.allow_db.id]
  db_subnet_group_name   = aws_db_subnet_group.default.name
}

resource "aws_db_subnet_group" "default" {
  name       = "main"
  subnet_ids = [aws_subnet.public_1.id, aws_subnet.public_2.id]

  tags = {
    Name = "Main"
  }
}

resource "aws_security_group" "allow_db" {
  name        = "allow_db"
  description = "Allow inbound traffic to DB"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "PostgreSQL from anywhere (Dev)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

variable "db_password" {
  type      = string
  sensitive = true
}
