# Instaclustr Cluster Management API

Official docs: `https://instaclustr.redoc.ly/Current/tag/OpenSearch-Provisioning-V2/`

The CLI (`scripts/instaclustr_ops.py`) is the only supported way to call the
API. Do not hand-write `curl` requests or JSON payloads; the shapes below
document what the CLI does so you can reason about failures.

## Authentication

- **HTTP Basic auth** using `INSTACLUSTR_API_USERNAME` and
  `INSTACLUSTR_API_KEY`, where the key is a **Provisioning** API key.
- The CLI sends `Authorization: Basic base64(username:apiKey)` on every
  request. Credentials come from the environment only — never place them in
  files, arguments, or emitted Terraform.

## Base URL

```
https://api.instaclustr.com/cluster-management/v2
```

## Endpoints used by the CLI

| Purpose | Method | Path |
|---|---|---|
| Compatible versions | `GET` | `/data-sources/applications/opensearch/versions/v2/` |
| Compatible node sizes | `GET` | `/data-sources/applications/opensearch/compatible-node-sizes/v2/` |
| Create cluster | `POST` | `/resources/applications/opensearch/clusters/v2/` |
| Get cluster | `GET` | `/resources/applications/opensearch/clusters/v2/{id}` |

There is **no delete endpoint in v1** of this skill. Tear-down is done by the
user via the Instaclustr console or `terraform destroy`.

## Rate limiting and polling

- The API enforces roughly a **1 request/second** style rate limit.
- `wait` polls `GET` at a default **1.1s** interval, which stays under that
  limit. Do not lower the interval to speed things up — it invites `429`s.
- Every urllib request has a **30-second socket timeout**.
- Socket, DNS, and TLS failures exit non-zero with a JSON `error`; the CLI
  does not replace them with a traceback or silently retry `create`.
- `wait --timeout-s` is a polling deadline checked after each completed
  request, not a hard process deadline. An in-flight request can therefore
  finish or hit its 30-second socket timeout after the polling deadline.
- `OPERATION_FAILED` and terminal cluster status `FAILED` stop `wait`
  immediately. The CLI prints a JSON error with the cluster id and both
  status fields instead of continuing until the polling deadline.

## Error handling

| Status | Meaning | Behavior |
|---|---|---|
| `401` / `403` | Bad credentials or non-Provisioning key | **Stop.** Ask the user to fix `INSTACLUSTR_API_USERNAME` / `INSTACLUSTR_API_KEY`. Do not retry. |
| `400` | Invalid payload (e.g. bad version, size, region) | Show the response body, re-run `compatible-versions` / `compatible-sizes`, correct the payload, then try again. |
| `429` | Rate limited | Read-only calls (`versions`, `sizes`, `get`) retry once after ~1s. **`create` is never retried** — report and let the user decide. |
| `5xx` | Server error | Report the error. **Never auto-retry `create`.** |

Successful create responses must be HTTP `200` or `202`. Any other response
status is emitted as a JSON error and is not treated as a created cluster.

Because `create` is never retried automatically, a create request that appears
to fail may still have created a cluster. Before creating again, use `get` (or
check the console) to confirm — never blindly issue a second `create`.
