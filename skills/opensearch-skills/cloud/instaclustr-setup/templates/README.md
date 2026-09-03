# Instaclustr OpenSearch (emitted)

This cluster was created with the Instaclustr Cluster Management API.
The skill does not run Terraform.

1. Set the sensitive Terraform input variables in your shell:
   - `export TF_VAR_instaclustr_username="$INSTACLUSTR_API_USERNAME"`
   - `export TF_VAR_instaclustr_api_key="$INSTACLUSTR_API_KEY"`
   Do not put either value in Terraform files or commit them.
2. `terraform init`
3. `terraform import instaclustr_opensearch_cluster_v2.this <cluster-id>`
4. `terraform plan` then `terraform apply` only for later changes.

To tear down, use the Instaclustr console or `terraform destroy` yourself.
