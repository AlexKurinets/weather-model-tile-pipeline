output "map_url" {
  value = "https://${aws_cloudfront_distribution.maps.domain_name}"
}

output "products_bucket" {
  value = aws_s3_bucket.products.bucket
}

output "ingest_queue_url" {
  value = aws_sqs_queue.ingest.id
}
