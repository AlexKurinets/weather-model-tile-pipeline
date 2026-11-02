variable "region" {
  type    = string
  default = "us-east-1" # where NOAA's Open Data buckets and SNS topics live
}

variable "name" {
  type    = string
  default = "wxtiles"
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "worker_ami_id" {
  type        = string
  description = "An AMI with Docker (for example Amazon Linux 2023 ECS-optimized)."
}

variable "gpu_ami_id" {
  type        = string
  description = "A GPU AMI with Docker and the NVIDIA container toolkit."
}

variable "worker_image" {
  type        = string
  description = "ECR URI of the image this repository builds."
}

variable "tiler_image" {
  type        = string
  description = "ECR URI of the same image with a Lambda handler entry point."
}

variable "noaa_topic_arns" {
  type        = list(string)
  default     = []
  description = "SNS new-object topics of the NOAA Open Data buckets to ingest from (see each bucket's Registry of Open Data page)."
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "alert_email" {
  type    = string
  default = ""
}
