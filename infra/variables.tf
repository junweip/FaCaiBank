variable "env" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "ap-southeast-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for the three public subnets."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_cidrs) == 3
    error_message = "Exactly three public subnet CIDRs are required."
  }
}

variable "private_app_subnet_cidrs" {
  description = "CIDR blocks for the three private application subnets."
  type        = list(string)

  validation {
    condition     = length(var.private_app_subnet_cidrs) == 3
    error_message = "Exactly three private application subnet CIDRs are required."
  }
}

variable "private_data_subnet_cidrs" {
  description = "CIDR blocks for the three private data subnets."
  type        = list(string)

  validation {
    condition     = length(var.private_data_subnet_cidrs) == 3
    error_message = "Exactly three private data subnet CIDRs are required."
  }
}

variable "single_nat_gateway" {
  description = "Use one NAT Gateway for all private subnets instead of one per AZ."
  type        = bool
  default     = true
}

variable "rds_multi_az" {
  description = "Enable a Multi-AZ RDS deployment."
  type        = bool
  default     = false
}

variable "enable_interface_endpoints" {
  description = "Create cost-incurring interface VPC endpoints for private subnets."
  type        = bool
  default     = false
}

variable "db_instance_class" {
  description = "RDS PostgreSQL instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_name" {
  description = "Initial PostgreSQL database name."
  type        = string
  default     = "facaibank"
}

variable "db_username" {
  description = "Master PostgreSQL username. Its generated password is stored in Secrets Manager."
  type        = string
  default     = "facaibank_admin"
}

variable "db_allocated_storage" {
  description = "Allocated RDS storage in GiB."
  type        = number
  default     = 20
}

variable "lambda_timeout" {
  description = "Timeout in seconds for the notification Lambda."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Memory in MiB for the notification Lambda."
  type        = number
  default     = 128
}
