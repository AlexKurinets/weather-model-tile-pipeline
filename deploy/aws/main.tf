# Production shape of the stack on AWS. `terraform validate` runs in CI; nothing here is applied by
# the tests. Compose maps onto it one to one: SeaweedFS is S3, nginx is CloudFront, the tiler
# container is the Lambda image, the worker container runs on the Spot Auto Scaling group, and the
# upstream_events table is the SQS queue that NOAA's SNS topics feed.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  tags = { project = var.name }
}

data "aws_caller_identity" "me" {}

# ---------------------------------------------------------------------------- storage
resource "aws_s3_bucket" "products" {
  bucket = "${var.name}-products-${data.aws_caller_identity.me.account_id}"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "products" {
  bucket                  = aws_s3_bucket.products.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# COGs are only useful for a few days; scratch arrays live until their run is finalized.
resource "aws_s3_bucket_lifecycle_configuration" "products" {
  bucket = aws_s3_bucket.products.id
  rule {
    id     = "cogs"
    status = "Enabled"
    filter { prefix = "cogs/" }
    expiration { days = 10 }
  }
  rule {
    id     = "scratch"
    status = "Enabled"
    filter { prefix = "scratch/" }
    expiration { days = 2 }
  }
}

resource "aws_s3_bucket_policy" "products" {
  bucket = aws_s3_bucket.products.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CloudFrontReadsWebAndManifests"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = ["${aws_s3_bucket.products.arn}/web/*", "${aws_s3_bucket.products.arn}/manifests/*"]
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.maps.arn } }
    }]
  })
}

# ---------------------------------------------------------------------------- notifications
resource "aws_sqs_queue" "ingest_dlq" {
  name                      = "${var.name}-ingest-dlq"
  message_retention_seconds = 1209600
  tags                      = local.tags
}

resource "aws_sqs_queue" "ingest" {
  name                       = "${var.name}-ingest"
  visibility_timeout_seconds = 120
  message_retention_seconds  = 86400
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingest_dlq.arn
    maxReceiveCount     = 5
  })
  tags = local.tags
}

# NOAA's Open Data buckets publish an SNS message per new object; subscribe the queue to each.
resource "aws_sns_topic_subscription" "noaa" {
  for_each             = toset(var.noaa_topic_arns)
  topic_arn            = each.value
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.ingest.arn
  raw_message_delivery = false
}

resource "aws_sqs_queue_policy" "ingest" {
  queue_url = aws_sqs_queue.ingest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.ingest.arn
      Condition = { ArnLike = { "aws:SourceArn" = length(var.noaa_topic_arns) > 0 ? var.noaa_topic_arns : ["arn:aws:sns:*:*:none"] } }
    }]
  })
}

# ---------------------------------------------------------------------------- run state
resource "aws_db_subnet_group" "state" {
  name       = "${var.name}-state"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "workers" {
  name   = "${var.name}-workers"
  vpc_id = var.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name   = "${var.name}-db"
  vpc_id = var.vpc_id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.workers.id]
  }
}

resource "aws_db_instance" "state" {
  identifier              = "${var.name}-state"
  engine                  = "postgres"
  engine_version          = "16"
  instance_class          = "db.t4g.medium"
  allocated_storage       = 50
  db_name                 = "wx"
  username                = "wx"
  password                = var.db_password
  db_subnet_group_name    = aws_db_subnet_group.state.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  backup_retention_period = 3
  skip_final_snapshot     = true
  tags                    = local.tags
}

# ---------------------------------------------------------------------------- ingest workers
resource "aws_iam_role" "worker" {
  name = "${var.name}-worker"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "worker" {
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
      Resource = [aws_s3_bucket.products.arn, "${aws_s3_bucket.products.arn}/*"] },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
      Resource = aws_sqs_queue.ingest.arn },
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
      Resource = "*" },
      { Effect = "Allow", Action = "cloudwatch:PutMetricData", Resource = "*",
      Condition = { StringEquals = { "cloudwatch:namespace" = var.name } } },
    ]
  })
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.name}-worker"
  role = aws_iam_role.worker.name
}

locals {
  worker_env = join(" ", [
    "-e DATABASE_URL=postgresql://wx:${var.db_password}@${aws_db_instance.state.address}:5432/wx",
    "-e WX_STORAGE=s3 -e WX_S3_ENDPOINT= -e WX_HTTP_MIRROR_BASE=https://nomads.ncep.noaa.gov",
    "-e WX_PRODUCTS_BUCKET=${aws_s3_bucket.products.bucket} -e WX_CLOCK=system -e WX_MANIFEST_TTL=60",
    "-e WX_WARM_URL=https://${aws_cloudfront_distribution.maps.domain_name} -e WX_METRICS_NAMESPACE=${var.name}",
  ])
}

resource "aws_launch_template" "worker" {
  name_prefix   = "${var.name}-worker-"
  image_id      = var.worker_ami_id
  instance_type = "c7i.xlarge"
  iam_instance_profile { name = aws_iam_instance_profile.worker.name }
  vpc_security_group_ids = [aws_security_group.workers.id]
  user_data = base64encode(<<-EOT
    #!/bin/bash
    docker run -d --restart unless-stopped ${local.worker_env} ${var.worker_image} wxtiles worker --processes 4
  EOT
  )
}

# Every worker runs the scheduler tick too: it is idempotent (inserts are on-conflict-do-nothing,
# notifications are taken with SKIP LOCKED, jobs are de-duplicated), so losing any instance loses
# nothing, and a job on a reclaimed Spot instance is taken over when its lease expires.
resource "aws_autoscaling_group" "workers" {
  name                = "${var.name}-workers"
  min_size            = 1
  max_size            = 6
  desired_capacity    = 1
  vpc_zone_identifier = var.private_subnet_ids
  capacity_rebalance  = true
  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "price-capacity-optimized"
    }
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.worker.id
        version            = "$Latest"
      }
      override { instance_type = "c7i.xlarge" }
      override { instance_type = "c6i.xlarge" }
      override { instance_type = "m7i.xlarge" }
    }
  }
  tag {
    key                 = "project"
    value               = var.name
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_policy" "workers_queue_depth" {
  name                   = "${var.name}-queue-depth"
  autoscaling_group_name = aws_autoscaling_group.workers.name
  policy_type            = "TargetTrackingScaling"
  target_tracking_configuration {
    target_value = 40
    customized_metric_specification {
      metric_name = "ApproximateNumberOfMessagesVisible"
      namespace   = "AWS/SQS"
      statistic   = "Average"
      metric_dimension {
        name  = "QueueName"
        value = aws_sqs_queue.ingest.name
      }
    }
  }
}

# GPU group for Pangu-Weather: workers there take only ai_run jobs. It is scheduled up for half
# an hour after each GFS analysis is due (about 03:25, 09:25, 15:25 and 21:25 UTC).
resource "aws_launch_template" "gpu" {
  name_prefix   = "${var.name}-gpu-"
  image_id      = var.gpu_ami_id
  instance_type = "g5.xlarge"
  iam_instance_profile { name = aws_iam_instance_profile.worker.name }
  vpc_security_group_ids = [aws_security_group.workers.id]
  instance_market_options { market_type = "spot" }
  user_data = base64encode(<<-EOT
    #!/bin/bash
    docker run -d --gpus all --restart unless-stopped ${local.worker_env} ${var.worker_image} wxtiles worker --processes 1 --kinds ai_run
  EOT
  )
}

resource "aws_autoscaling_group" "gpu" {
  name                = "${var.name}-gpu"
  min_size            = 0
  max_size            = 1
  desired_capacity    = 0
  vpc_zone_identifier = var.private_subnet_ids
  launch_template {
    id      = aws_launch_template.gpu.id
    version = "$Latest"
  }
}

resource "aws_autoscaling_schedule" "gpu_up" {
  scheduled_action_name  = "${var.name}-gpu-up"
  autoscaling_group_name = aws_autoscaling_group.gpu.name
  recurrence             = "20 3,9,15,21 * * *"
  min_size               = 0
  max_size               = 1
  desired_capacity       = 1
}

resource "aws_autoscaling_schedule" "gpu_down" {
  scheduled_action_name  = "${var.name}-gpu-down"
  autoscaling_group_name = aws_autoscaling_group.gpu.name
  recurrence             = "55 3,9,15,21 * * *"
  min_size               = 0
  max_size               = 1
  desired_capacity       = 0
}

# ---------------------------------------------------------------------------- tiles
resource "aws_iam_role" "tiler" {
  name = "${var.name}-tiler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "tiler_logs" {
  role       = aws_iam_role.tiler.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "tiler" {
  role = aws_iam_role.tiler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["s3:GetObject", "s3:ListBucket"],
    Resource = [aws_s3_bucket.products.arn, "${aws_s3_bucket.products.arn}/*"] }]
  })
}

resource "aws_lambda_function" "tiler" {
  function_name = "${var.name}-tiler"
  role          = aws_iam_role.tiler.arn
  package_type  = "Image"
  image_uri     = var.tiler_image
  memory_size   = 2048
  timeout       = 15
  # Tiles read only S3; the status endpoints also read run state, hence the VPC.
  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.workers.id]
  }
  environment {
    variables = {
      DATABASE_URL                 = "postgresql://wx:${var.db_password}@${aws_db_instance.state.address}:5432/wx"
      WX_PRODUCTS_BUCKET           = aws_s3_bucket.products.bucket
      WX_STORAGE                   = "s3"
      WX_S3_ENDPOINT               = ""
      GDAL_DISABLE_READDIR_ON_OPEN = "EMPTY_DIR"
      VSI_CACHE                    = "TRUE"
      GDAL_CACHEMAX                = "512"
    }
  }
}

resource "aws_lambda_function_url" "tiler" {
  function_name      = aws_lambda_function.tiler.function_name
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront" {
  statement_id  = "AllowCloudFront"
  action        = "lambda:InvokeFunctionUrl"
  function_name = aws_lambda_function.tiler.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.maps.arn
}

resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = "${var.name}-s3"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "lambda" {
  name                              = "${var.name}-lambda"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Tile URLs name their run, so a tile never changes: cache for as long as the origin says (a year).
resource "aws_cloudfront_cache_policy" "tiles" {
  name        = "${var.name}-tiles"
  min_ttl     = 86400
  default_ttl = 31536000
  max_ttl     = 31536000
  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
    enable_accept_encoding_gzip = false
  }
}

# Manifests say which runs exist; they are the only thing that has to expire.
resource "aws_cloudfront_cache_policy" "manifests" {
  name        = "${var.name}-manifests"
  min_ttl     = 0
  default_ttl = 60
  max_ttl     = 60
  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
    enable_accept_encoding_gzip = true
  }
}

resource "aws_cloudfront_distribution" "maps" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"

  origin {
    origin_id                = "web"
    domain_name              = aws_s3_bucket.products.bucket_regional_domain_name
    origin_path              = "/web"
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }
  origin {
    origin_id                = "data"
    domain_name              = aws_s3_bucket.products.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }
  origin {
    origin_id                = "tiler"
    domain_name              = replace(replace(aws_lambda_function_url.tiler.function_url, "https://", ""), "/", "")
    origin_access_control_id = aws_cloudfront_origin_access_control.lambda.id
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  ordered_cache_behavior {
    path_pattern           = "/tiles/*"
    target_origin_id       = "tiler"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.tiles.id
  }
  ordered_cache_behavior {
    path_pattern           = "/manifests/*"
    target_origin_id       = "data"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.manifests.id
  }
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "tiler"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # Managed-CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # Managed-AllViewerExceptHostHeader
  }
  default_cache_behavior {
    target_origin_id       = "web"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
  tags = local.tags
}

# ---------------------------------------------------------------------------- alarms
resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Notifications waiting more than ten minutes means workers are not keeping up or are down.
resource "aws_cloudwatch_metric_alarm" "ingest_backlog" {
  alarm_name          = "${var.name}-ingest-backlog"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.ingest.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 10
  threshold           = 600
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# Feeds past their expected time plus grace, published by the scheduler as a custom metric.
resource "aws_cloudwatch_metric_alarm" "late_feeds" {
  alarm_name          = "${var.name}-late-feeds"
  namespace           = var.name
  metric_name         = "SourcesLate"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
