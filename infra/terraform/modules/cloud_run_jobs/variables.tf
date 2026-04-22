variable "env"                         { type = string }
variable "region"                      { type = string }
variable "repository_url"              { type = string }
variable "image_tag"                   { type = string }
variable "worker_sa_email"             { type = string }
variable "scheduler_sa_email"          { type = string }
variable "vpc_connector_id"            { type = string }
variable "sql_private_ip"              { type = string }
variable "bucket_name"                 { type = string }
variable "openrouter_secret_id"        { type = string }
variable "proxy_credentials_secret_id" { type = string }
variable "default_task_count"          { 
    type = number
 default = 5 
 }
variable "browsers_per_task"           { 
    type = number
default = 10 
}
variable "task_cpu"                    { 
    type = string 
default = "2" 
}
variable "task_memory"                 { 
    type = string 
default = "4Gi" 
}
