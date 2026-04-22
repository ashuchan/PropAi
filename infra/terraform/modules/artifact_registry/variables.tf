variable "env" { type = string }
variable "project_id" { type = string }
variable "region" { type = string }
variable "deployer_sa" {
  type        = string
  description = "Service account email granted artifactregistry.writer"
}
