variable "env"                    { type = string }
variable "region"                 { type = string }
variable "project_id"             { type = string }
variable "scheduler_sa_email"     { type = string }
variable "scrape_job_id"          { type = string }
variable "retry_job_id"           { type = string }
variable "sql_instance_name"      { type = string }
variable "retry_scheduler_paused" {
  type    = bool
  default = true # disabled until auto-retry is validated
}
