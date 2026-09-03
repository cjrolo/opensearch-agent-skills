# Cloud provider payloads

Each cluster has one data centre whose shape depends on the cloud provider.
Collect the fields below, then let the CLI build the payload — do not
hand-write JSON.

## Common rules

- **`region`** uses Instaclustr region names in upper snake case, e.g.
  `US_EAST_1` (not `us-east-1`), `EU_WEST_1`, `US_WEST_2`.
- **`network`** is a CIDR block sized between `/16` and `/26`. It must not
  overlap ranges you intend to peer with.
- The agent **must not provision VPCs, subnets, or networks.** For BYOC, the
  user supplies existing network / account identifiers.

## Instaclustr-hosted (Instaclustr-run account)

Instaclustr runs the underlying cloud account. **Omit `providerAccountName`.**

- `cloudProvider`: `AWS_VPC`, `GCP`, or `AZURE` / `AZURE_AZ`
- `region`, `network`, node sizes, version — no provider-specific settings
  block required.

## AWS BYOC

Run the cluster in the user's own AWS account.

- `cloudProvider`: `AWS_VPC`
- `providerAccountName`: the name of the user's linked AWS provider account.
- `awsSettings` is a one-element array. Its supported optional keys are
  `ebsEncryptionKey`, `customVirtualNetworkId`, and `storageNetwork`:

```json
"awsSettings": [{
  "ebsEncryptionKey": "<configured KMS key id>",
  "customVirtualNetworkId": "vpc-...",
  "storageNetwork": "10.1.0.0/24"
}]
```

Pass the inner object to `--aws-settings-json`; the CLI normalizes it to the
required one-element array.

## GCP

- `cloudProvider`: `GCP`
- `providerAccountName`: the user's linked GCP provider account (BYOC).
- `gcpSettings` is a one-element array. Its supported optional key is
  `customVirtualNetworkId`:

```json
"gcpSettings": [{
  "customVirtualNetworkId": "projects/example/global/networks/opensearch"
}]
```

Pass the inner object to `--gcp-settings-json`.

## Azure

- `cloudProvider`: `AZURE` or `AZURE_AZ` (availability-zone variant).
- `providerAccountName`: the user's linked Azure provider account (BYOC).
- `azureSettings` is a one-element array. Its supported optional keys are
  `resourceGroup`, `customVirtualNetworkId`, and `storageNetwork`:

```json
"azureSettings": [{
  "resourceGroup": "opensearch-rg",
  "customVirtualNetworkId": "/subscriptions/.../virtualNetworks/opensearch",
  "storageNetwork": "10.2.0.0/24"
}]
```

Pass the inner object to `--azure-settings-json`.

## ON-PREMISES

The CLI also accepts `cloudProvider: ONPREMISES` for clusters running on
user-managed hardware. This is outside the v1 focus of this skill (managed
cloud provisioning) — support it only if the user explicitly asks, and expect
network / node details to be supplied entirely by the user.

## Provider settings validation

Only one provider settings array can appear in a data centre, and it must
match `cloudProvider`: `awsSettings` with `AWS_VPC`, `gcpSettings` with `GCP`,
or `azureSettings` with `AZURE` / `AZURE_AZ`. The CLI rejects mismatches,
arrays with other than one object, and keys outside the sets documented above.
If the current API documentation changes, update the CLI and tests before
using a new field; do not pass through an unverified key.

Official schema:
`https://instaclustr.redoc.ly/Current/tag/OpenSearch-Provisioning-V2/`

## Confirming allowed values

Region and node-size validity depends on provider. Always confirm choices
against `compatible-sizes` and `compatible-versions` before creating; a `400`
usually means a value is not offered for the chosen provider/region.
