variable "env"             { type = string }
variable "region"          { type = string }
variable "db_tier"         { type = string }
variable "vpc_self_link"   { type = string }
variable "developer_emails" {
  type    = list(string)
  default = []
}
variable "worker_sa_email" { type = string }
