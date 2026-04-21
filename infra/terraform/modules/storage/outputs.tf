output "bucket_name" {
  value = google_storage_bucket.jugnu_raw.name
}

output "bucket_url" {
  value = "gs://${google_storage_bucket.jugnu_raw.name}"
}
