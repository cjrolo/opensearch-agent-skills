# Semantic Profile

A semantic profile declares, for an index pattern, **which fields carry resource usage,
identity, and dimensions, and what system produced them** — so analyzers are written
against meaning, not raw field names. This is the bridge from "OpenSearch cost tool" to
"operations agent": a second system (e.g. Cassandra) ships its own profile and inherits
the same analysis and learning loop.

## OpenSearch self-profile

`profiles/opensearch-self.json` is the v1 reference. The domain analyzers in this MVP use
OpenSearch-specific APIs (`_cat/shards`, `_stats`, `_plugins/_ism/explain`,
`_insights/top_queries`), so the `metrics` map is mostly documentation here. Tune
behavior via `cluster_id`, `index_pattern`, and `thresholds`.

```json
{
  "profile_id": "opensearch-self-v1",
  "system": "opensearch",
  "cluster_id": "local-dev",
  "index_pattern": "*",
  "thresholds": { "oversharding_min_primary_shards": 8 }
}
```

## Stage-2 profile (portable analyzers)

For systems whose metrics already live in OpenSearch indices, the `metrics` map points at
real document fields and the **generic** analyzers (many-small-partitions, skew,
growth-anomaly) run purely against the profile — no system-specific code:

```json
{
  "profile_id": "cassandra-metrics-v1",
  "system": "cassandra",
  "index_pattern": "cassandra-metrics-*",
  "timestamp_field": "@timestamp",
  "identity": { "node_field": "host.name", "partition_field": "table.name" },
  "metrics": {
    "disk_usage_bytes": "cassandra.table.live_disk_space_used",
    "partition_count": "cassandra.table.sstable_count"
  },
  "dimensions": ["keyspace", "datacenter"]
}
```

## Two analyzer layers

- **Generic analyzers** — expressed against the profile's `metrics`/`identity`/
  `dimensions` via PPL aggregations. Portable across systems. (Stage 2.)
- **Domain analyzers** — use system-specific APIs and knowledge (the OpenSearch shard/ISM
  logic in this MVP). Bound to one `system`.

Keeping the OpenSearch checks as domain analyzers now, while shaping findings around a
profile, is what lets the generic layer be lifted out later without reworking the loop.
