# Shared remote state. Create the bucket and lock table once with infra/bootstrap,
# then: terraform init -backend-config=backend.hcl   (see backend.hcl.example)
terraform {
  backend "s3" {}
}
