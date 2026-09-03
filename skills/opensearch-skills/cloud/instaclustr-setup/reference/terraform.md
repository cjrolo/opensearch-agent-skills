# Terraform emission

`emit-terraform` writes `main.tf` and a `README.md` so the user can manage a
cluster that already exists. **This skill never runs Terraform.**

## What gets emitted

Run after the cluster is created, using the **original create payload** and the
cluster id — not a reconstruction from a final `GET`:

```bash
uv run python skills/opensearch-skills/cloud/instaclustr-setup/scripts/instaclustr_ops.py \
  emit-terraform --cluster-id <id> --payload-json '<original create payload>'
```

This produces:

- `main.tf` — a `provider "instaclustr"` block plus an
  `instaclustr_opensearch_cluster_v2` resource whose body mirrors the create
  payload.
- `README.md` — the import and destroy instructions below.

## Import flow

The cluster already exists via the API, so Terraform must **import** it before
any apply, or Terraform would try to create a duplicate:

```bash
terraform init
terraform import instaclustr_opensearch_cluster_v2.this <cluster-id>
terraform plan   # then apply only for later changes
```

## Provider key

The emitted HCL declares sensitive variables and configures the provider
without embedding a credential:

```hcl
variable "instaclustr_username" {
  type      = string
  sensitive = true
}

variable "instaclustr_api_key" {
  type      = string
  sensitive = true
}

provider "instaclustr" {
  terraform_key = "Instaclustr-Terraform ${var.instaclustr_username}:${var.instaclustr_api_key}"
}
```

Set both values in the shell before Terraform runs:

```bash
export TF_VAR_instaclustr_username="$INSTACLUSTR_API_USERNAME"
export TF_VAR_instaclustr_api_key="$INSTACLUSTR_API_KEY"
```

Never put either literal value in `.tf`, `.tfvars`, command history, or
version control.

## Destroy

This skill has no delete command. To tear the cluster down, the user runs it
themselves via the Instaclustr console or `terraform destroy`. The agent never
runs `terraform apply` or `terraform destroy`.

## Secrets

The emitter refuses to serialize payloads containing an `apiKey` field or the
API key value. Credentials stay in the environment only; emitted files contain
no secrets.
