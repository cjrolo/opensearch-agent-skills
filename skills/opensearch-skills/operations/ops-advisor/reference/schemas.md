# Finding & Verdict Schemas

These are the two net-new data contracts. They are identical whether the MVP runs as a
skill or the design is later upstreamed into ml-commons — only *where they live* changes
(local script vs. in-cluster tool).

## Finding

Emitted by an analyzer. Deterministic: every value comes from a stats API, never an LLM.

```json
{
  "finding_id": "opensearch_oversharding-1a2b3c4d5e",
  "type": "opensearch_oversharding",
  "domain": "opensearch",
  "severity": "medium",            // low | medium | high
  "confidence": "high",            // low | medium | high  (gates alerting)
  "namespace": {
    "cluster_id": "prod-eu-1",
    "index_pattern": "logs-2026.05.20"
  },
  "evidence": {
    "primary_shards": 480,
    "median_primary_shard_size_bytes": 2147483648,
    "small_shard_count": 391
  },
  "impact": {
    "resource_types": ["heap", "cluster_state", "search_fanout"],
    "estimated_direction": "reduce"
  },
  "suggested_action": {
    "type": "review_ism_rollover_and_shard_strategy",
    "description": "Review rollover thresholds and primary shard count for logs-*."
  },
  "safety": { "execution": "manual", "notes": "No automatic change is proposed." },
  "observed_at": "2026-05-20T10:00:00Z"
}
```

## Verdict

Written by the judgment agent after the operator responds. This is the learned context.

```json
{
  "verdict_id": "verdict-oversharding-logs-2026-05",
  "finding_type": "opensearch_oversharding",
  "judgment": "intentional",       // intentional | confirmed | deferred
  "scope": {
    "level": "index_pattern",      // index | index_pattern | cluster
    "cluster_id": "prod-eu-1",
    "index_pattern": "logs-*"      // omit for cluster-level scope
  },
  "rationale": "logs-* is intentionally oversharded for parallel bulk-ingest throughput.",
  "evidence_snapshot": { "primary_shards": 480, "small_shard_count": 391 },
  "reraise_policy": {
    "metric": "primary_shards",    // must exist in evidence_snapshot AND new evidence
    "type": "pct_increase",        // pct_increase | abs_increase
    "threshold": 0.5               // 0.5 = re-raise if metric rises >= 50%
  },
  "created_by": "operator:alice",
  "created_at": "2026-05-20T10:00:00Z"
}
```

## Scope and re-raise — the anti-blindness mechanics

- **`scope.level`** controls breadth: `index` (narrowest, safest) → `index_pattern` →
  `cluster` (broadest). Default to the narrowest scope that matches the operator's
  rationale. Broad suppression is how an advisor learns to go blind.
- **`evidence_snapshot` + `reraise_policy`** make suppression *revisable*, not permanent.
  `check-divergence` compares fresh evidence to the snapshot and re-raises when the
  metric moves past the threshold. Always pick a `metric` the analyzer actually emits.
