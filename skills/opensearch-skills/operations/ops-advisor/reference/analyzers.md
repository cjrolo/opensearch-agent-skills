# Analyzer Pack Reference

Use this to explain any finding the operator asks about. Every analyzer reads live stats
and emits findings deterministically — **no LLM produces a number or a finding**.

| Finding type | What it detects | Key evidence | Source API | Default trigger |
| --- | --- | --- | --- | --- |
| `opensearch_oversharding` | Index has many primary shards that are mostly small | `primary_shards`, `median_primary_shard_size_bytes`, `small_shard_count` | `_cat/shards` | ≥ 8 primaries **and** median shard < 5 GiB |
| `opensearch_shard_skew` | One primary shard far larger than the median | `max/median_primary_shard_size_bytes`, `skew_ratio` | `_cat/shards` | max > 3× median |
| `opensearch_segment_waste` | High share of deleted docs wasting disk/search | `deleted_ratio`, `deleted_docs`, `store_size_bytes` | `_stats/docs,store` | deleted ratio ≥ 20% |
| `opensearch_lifecycle_gap` | Large index with no ISM policy (unmanaged growth) | `managed: false`, `pri_store_size_bytes` | `_plugins/_ism/explain` | unmanaged **and** ≥ 10 GiB |
| `opensearch_query_cost_hotspot` | Expensive recurring queries | `latency_ms`, `indices`, `total_shards` | `_insights/top_queries` | top-N by latency |

## Notes

- **Graceful skip.** `lifecycle_gap` needs index-management; `query_cost_hotspot` needs
  query-insights. If absent, the analyzer returns a `{"skipped": ..., "reason": ...}`
  marker (surfaced under `skipped` in `analyze` output) instead of failing. Tell the
  operator which checks were skipped and why.
- **Thresholds** live in the semantic profile under `thresholds` and override the
  defaults in `analyzers.py` (`DEFAULT_THRESHOLDS`). Tune per cluster; precision
  (false-positive rate) is the gating metric.
- **Aggregate-only.** Analyzers emit counts/sizes/ratios, never raw documents, so
  findings don't leak high-cardinality data.

## Adding an analyzer

1. Write `analyze_<name>(client, profile, thresholds) -> list[finding]` in
   `analyzers.py`, building findings via the `_finding(...)` helper.
2. Register it in the `ANALYZERS` dict.
3. Document its row above. Keep it deterministic and bounded (time range, index
   pattern, aggregation size).

## Trend / anomaly checks (next)

The proposal's purest "unknown unknowns" (disk-growth-after-ISM-change, post-deploy cost
regression) are trend-based. In this MVP they map to follow-up analyzers over PPL
`trendline` / Anomaly Detection results — added the same way as above. They are the
highest-value extension once the deterministic checks prove out.
