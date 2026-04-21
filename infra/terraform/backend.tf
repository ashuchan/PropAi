terraform {
  backend "gcs" {
    # bucket and prefix supplied via -backend-config at init time:
    # terraform init \
    #   -backend-config="bucket=jugnu-tfstate-staging" \
    #   -backend-config="prefix=terraform/state"
  }
}
