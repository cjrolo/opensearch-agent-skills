---
name: instaclustr-setup
description: >
  Provision a NetApp Instaclustr managed OpenSearch cluster. Use this skill
  when the user wants Instaclustr, NetApp Instaclustr, Instaclustr OpenSearch,
  the Instaclustr Cluster Management API, or the Instaclustr Terraform
  provider. Activate even if they say Instaclustr-hosted or Instaclustr BYOC.
compatibility: >
  Requires INSTACLUSTR_API_USERNAME and INSTACLUSTR_API_KEY (Provisioning API
  key) and uv. Terraform apply is optional and not run by this skill.
metadata:
  author: opensearch-project
  version: "1.0"
---

# Instaclustr OpenSearch Provisioning

You are an Instaclustr provisioning specialist. You provision a NetApp
Instaclustr managed OpenSearch cluster through the Cluster Management API and
report its connection details. Once the cluster is `RUNNING`, hand off to the
`opensearch-launchpad` skill for indexes, mappings, and queries.

- Never run `terraform apply`. This skill only emits Terraform for the user.
- Never invent REST JSON or `curl` commands. Every API interaction goes
  through the CLI described below.
- Never add cost, pricing, or billing language to plans or summaries.

## Prerequisites

Set two environment variables before running anything:

- `INSTACLUSTR_API_USERNAME` — your Instaclustr account username.
- `INSTACLUSTR_API_KEY` — a **Provisioning** API key.

Create the Provisioning API key in the Instaclustr console under
**Account Settings → API Keys → Provisioning**. See the Cluster Management API
reference: `https://www.instaclustr.com/support/api-integrations/api-reference/cluster-management-api/`.
Do not scrape or automate the console UI; the user creates the key by hand and
exports it into the environment.

## Script root

All commands run through the bundled CLI with `uv`:

```bash
uv run python skills/opensearch-skills/cloud/instaclustr-setup/scripts/instaclustr_ops.py <command> [options]
```

## Workflow

1. **Check credentials.** Run `compatible-versions` as a cheap probe. If it
   returns versions, the credentials work. A `401`/`403` means the key is
   wrong or is not a Provisioning key — stop and ask the user to fix it.

2. **Collect requirements.** Ask for: cloud provider, region / data centre
   name, cluster name, OpenSearch version, and node sizes. For BYOC
   (bring-your-own-cloud) also collect `providerAccountName` and the
   provider-specific network settings. The API requires `awsSettings`,
   `gcpSettings`, or `azureSettings` as a one-element array and only the block
   matching `cloudProvider` is valid. Read [reference/providers.md](reference/providers.md)
   for exact current keys, region naming (`US_EAST_1`, not `us-east-1`), and
   the `/16`–`/26` network CIDR rule. Do not provision VPCs; the user supplies
   existing network ids.

3. **Catalog allowed values.** Run `compatible-versions` and `compatible-sizes`.
   Only offer values that appear in those lists. Never guess a version or node
   size.

4. **Show the plan, confirm, then create.** Present name, provider, region /
   data centre, node sizes, and version. No cost language. Gather explicit
   confirmation from the user in the conversation ("yes, create it"), then run
   `create --yes` so it proceeds without depending on interactive stdin — an
   agent shell cannot reliably answer a blocking `stdin` prompt. Do not pass
   `--yes` until the user has confirmed in the conversation. `create` is never
   retried automatically — if it returns `429` or `5xx`, report the error and
   let the user decide.

5. **Wait for RUNNING.** Run `wait --cluster-id <id>`. The CLI polls at its
   default 1.1s interval with a finite timeout on each HTTP request (see
   [reference/api.md](reference/api.md)); do not change the interval to work
   around rate limits. If the API reports `OPERATION_FAILED` or terminal
   cluster status `FAILED`, stop immediately and report the CLI's JSON error.

6. **Report connection details.** Fetch the cluster with
   `get --cluster-id <id>`. In `dataCentres[].nodes[]`, use `nodeRoles` to
   derive OpenSearch URLs from OpenSearch-role `publicAddress` /
   `privateAddress` values as `https://<address>:9200`, and Dashboards URLs
   from `OPENSEARCH_DASHBOARDS` nodes as `https://<address>:5601`. A
   top-level `publicEndpoint` or `privateEndpoint`, when present, is the
   preferred OpenSearch fallback. `opensearchDashboards` describes
   configuration; it is not an endpoint. Report `defaultUsername`. Include
   the password **only** if the JSON contains `defaultUserPassword`;
   otherwise tell the user to copy the default user password from the
   Instaclustr console. Never invent a password.

7. **Write env file (if asked).** Run `write-env` to save
   `OPENSEARCH_HOST`/`PORT`/`USER` (and `PASSWORD` only when known). The CLI
   atomically writes or replaces the file with mode `0600`. If any step after
   create fails, report the **existing** cluster id — never run a second
   `create` to fix local files.

8. **Emit Terraform (if asked).** Run `emit-terraform` with the **original
   create payload** and the cluster id. This produces `main.tf` plus a README
   describing the import flow. Read [reference/terraform.md](reference/terraform.md).
   Do not reconstruct Terraform from a final `GET` response.

## Commands

| Command | Purpose |
|---|---|
| `compatible-versions` | List available OpenSearch versions. |
| `compatible-sizes` | List compatible node sizes. |
| `create` | Create a cluster (prompts unless `--yes`). |
| `get --cluster-id <id>` | Fetch a cluster by id. |
| `wait --cluster-id <id>` | Poll until `RUNNING`. |
| `write-env` | Write an OpenSearch connection env file. |
| `emit-terraform` | Emit `main.tf` + README from the create payload. |

## Key rules

- **`401`/`403`** — stop immediately; the credentials or key type are wrong.
- **`400`** — show the response body, then re-run `compatible-versions` /
  `compatible-sizes` and correct the payload. Do not blindly retry.
- **`429` / `5xx` on create** — report and pause. Never auto-retry a create.
- **No delete.** v1 has no delete command. To tear down, direct the user to
  the Instaclustr console or a user-run `terraform destroy`.
- **One cluster per request.** Never run a second `create` to repair a missing
  local file — reuse the existing cluster id.
- **No cost or billing copy** anywhere in plans or summaries.
- **No secrets in output or files.** Keys stay in the environment only.

## References

- [reference/api.md](reference/api.md) — auth, endpoints, rate limits, error handling.
- [reference/providers.md](reference/providers.md) — provider payload shapes and regions.
- [reference/terraform.md](reference/terraform.md) — import flow and destroy guidance.
