output "instance_connection_name" {
  value = google_sql_database_instance.jugnu_db.connection_name
}

output "private_ip" {
  value = google_sql_database_instance.jugnu_db.private_ip_address
}

output "database_name" {
  value = google_sql_database.jugnu.name
}

output "instance_name" {
  value = google_sql_database_instance.jugnu_db.name
}

output "vpc_connector_id" {
  value = google_vpc_access_connector.jugnu_connector.id
}
