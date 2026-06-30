---
name: operations
description: >
  Detect cost and operational risks in OpenSearch clusters. Use this skill when
  the user wants to find unknown operational blind spots, detect oversharding,
  shard skew, segment waste, missing lifecycle policies, or query-cost hotspots.
  Also use when the user mentions cost intelligence, ops advisor, finding, verdict,
  suppress alert, re-raise, ISM disk inflation, index lifecycle gap, or wants the
  cluster to proactively surface issues it hasn't been asked to watch.
compatibility: Requires a running OpenSearch cluster. Query-cost checks need the query-insights plugin. Lifecycle checks need the index-management plugin.
metadata:
  author: opensearch-project
  version: "1.0"
---

# Operations

Category skill for proactive cost and operational risk detection in OpenSearch.

## Skills

| Skill | Description |
|---|---|
| [ops-advisor](ops-advisor/SKILL.md) | Detect oversharding, shard skew, segment waste, lifecycle gaps, query-cost hotspots — and learn from operator feedback so alerts get quieter over time |

## When to Use

| User Intent | Skill |
|---|---|
| Detect unknown operational or cost risks; surface blind spots | [ops-advisor](ops-advisor/SKILL.md) |
| Review a specific finding, dismiss as intentional, or re-raise | [ops-advisor](ops-advisor/SKILL.md) |
| Reduce alert noise; capture why a known pattern is intentional | [ops-advisor](ops-advisor/SKILL.md) |
