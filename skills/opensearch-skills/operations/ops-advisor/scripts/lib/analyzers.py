"""Deterministic analyzer pack for OpenSearch self-analysis.

Each analyzer is a pure function: it reads live stats via an opensearch-py client
and emits zero or more findings conforming to reference/schemas.md. No LLM produces
a finding or its evidence. Analyzers degrade gracefully when a required plugin is
absent, returning a skipped marker instead of raising.
"""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timezone
from typing import Any

from opensearchpy import OpenSearch

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "oversharding_min_primary_shards": 8,
    "small_shard_bytes": 5 * 1024**3,        # shards below this are "small"
    "skew_ratio": 3.0,                         # flag when max > ratio × median
    "deleted_docs_ratio": 0.20,               # flag when deleted ≥ 20 % of total
    "lifecycle_min_index_bytes": 10 * 1024**3, # only flag unmanaged indices above this
    "query_cost_top_n": 5,
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finding(
    ftype: str,
    profile: dict,
    namespace: dict,
    severity: str,
    confidence: str,
    evidence: dict,
    impact: dict,
    action: dict,
) -> dict:
    ns = {"cluster_id": profile.get("cluster_id", "unknown"), **namespace}
    return {
        "finding_id": f"{ftype}-{uuid.uuid4().hex[:10]}",
        "type": ftype,
        "domain": profile.get("system", "opensearch"),
        "severity": severity,
        "confidence": confidence,
        "namespace": ns,
        "evidence": evidence,
        "impact": impact,
        "suggested_action": action,
        "safety": {"execution": "manual", "notes": "No automatic change is proposed."},
        "observed_at": _now(),
    }


def _index_pattern(profile: dict) -> str:
    return profile.get("index_pattern", "*")


def _primary_shards(client: OpenSearch, pattern: str) -> dict[str, list[int]]:
    """Return {index: [primary shard sizes in bytes]} for the pattern."""
    rows = client.cat.shards(
        index=pattern,
        params={"h": "index,prirep,store", "bytes": "b", "format": "json"},
    )
    by_index: dict[str, list[int]] = {}
    for r in rows:
        if r.get("prirep") != "p":
            continue
        size = int(r.get("store") or 0)
        by_index.setdefault(r["index"], []).append(size)
    return by_index


def analyze_oversharding(client: OpenSearch, profile: dict, t: dict) -> list[dict]:
    pattern = _index_pattern(profile)
    findings: list[dict] = []
    for index, sizes in _primary_shards(client, pattern).items():
        if not sizes:
            continue
        median = statistics.median(sizes)
        small = sum(1 for s in sizes if s < t["small_shard_bytes"])
        if len(sizes) >= t["oversharding_min_primary_shards"] and median < t["small_shard_bytes"]:
            findings.append(_finding(
                "opensearch_oversharding", profile,
                {"index_pattern": index},
                severity="medium",
                confidence="high" if small >= len(sizes) * 0.8 else "medium",
                evidence={
                    "primary_shards": len(sizes),
                    "median_primary_shard_size_bytes": int(median),
                    "small_shard_count": small,
                    "small_shard_threshold_bytes": t["small_shard_bytes"],
                },
                impact={"resource_types": ["heap", "cluster_state", "search_fanout"],
                        "estimated_direction": "reduce"},
                action={"type": "review_ism_rollover_and_shard_strategy",
                        "description": f"Review rollover thresholds and primary shard count for {index}."},
            ))
    return findings


def analyze_shard_skew(client: OpenSearch, profile: dict, t: dict) -> list[dict]:
    pattern = _index_pattern(profile)
    findings: list[dict] = []
    for index, sizes in _primary_shards(client, pattern).items():
        if len(sizes) < 2:
            continue
        median = statistics.median(sizes)
        biggest = max(sizes)
        if median > 0 and biggest > t["skew_ratio"] * median:
            findings.append(_finding(
                "opensearch_shard_skew", profile,
                {"index_pattern": index},
                severity="medium", confidence="medium",
                evidence={
                    "primary_shards": len(sizes),
                    "max_primary_shard_size_bytes": int(biggest),
                    "median_primary_shard_size_bytes": int(median),
                    "skew_ratio": round(biggest / median, 2),
                },
                impact={"resource_types": ["hot_node", "search_latency"],
                        "estimated_direction": "rebalance"},
                action={"type": "review_routing_and_shard_count",
                        "description": f"Investigate uneven shard sizing on {index}."},
            ))
    return findings


def analyze_segment_waste(client: OpenSearch, profile: dict, t: dict) -> list[dict]:
    pattern = _index_pattern(profile)
    stats = client.indices.stats(index=pattern, metric="docs,store", params={"level": "indices"})
    findings: list[dict] = []
    for index, body in (stats.get("indices") or {}).items():
        docs = body["primaries"]["docs"]
        count, deleted = docs.get("count", 0), docs.get("deleted", 0)
        total = count + deleted
        if total == 0:
            continue
        ratio = deleted / total
        if ratio >= t["deleted_docs_ratio"]:
            findings.append(_finding(
                "opensearch_segment_waste", profile,
                {"index_pattern": index},
                severity="low" if ratio < 0.4 else "medium",
                confidence="high",
                evidence={
                    "doc_count": count,
                    "deleted_docs": deleted,
                    "deleted_ratio": round(ratio, 3),
                    "store_size_bytes": body["primaries"]["store"].get("size_in_bytes", 0),
                },
                impact={"resource_types": ["disk", "search_latency"], "estimated_direction": "reduce"},
                action={"type": "consider_force_merge_or_rollover",
                        "description": f"{index} carries {ratio:.0%} deleted docs; review force-merge/rollover strategy."},
            ))
    return findings


def analyze_lifecycle_gap(client: OpenSearch, profile: dict, t: dict) -> list[dict]:
    pattern = _index_pattern(profile)
    try:
        explain = client.transport.perform_request("GET", f"/_plugins/_ism/explain/{pattern}")
    except Exception:
        return [{"skipped": "opensearch_lifecycle_gap", "reason": "ISM (index-management) plugin not available"}]
    sizes_raw = client.cat.indices(
        index=pattern,
        params={"h": "index,pri.store.size", "bytes": "b", "format": "json"},
    )
    size_by_index = {r["index"]: int(r.get("pri.store.size") or 0) for r in sizes_raw}
    findings: list[dict] = []
    for index, meta in explain.items():
        if index.startswith("total_") or not isinstance(meta, dict):
            continue
        policy_id = meta.get("policy_id")
        size = size_by_index.get(index, 0)
        if policy_id is None and size >= t["lifecycle_min_index_bytes"]:
            findings.append(_finding(
                "opensearch_lifecycle_gap", profile,
                {"index_pattern": index},
                severity="medium", confidence="high",
                evidence={"managed": False, "pri_store_size_bytes": size},
                impact={"resource_types": ["disk"], "estimated_direction": "control_growth"},
                action={"type": "attach_ism_policy",
                        "description": f"{index} ({size / 1024**3:.1f} GiB) has no ISM policy; growth is unmanaged."},
            ))
    return findings


def analyze_query_cost_hotspot(client: OpenSearch, profile: dict, t: dict) -> list[dict]:
    try:
        top = client.transport.perform_request(
            "GET", "/_insights/top_queries", params={"type": "latency"}
        )
    except Exception:
        return [{"skipped": "opensearch_query_cost_hotspot", "reason": "query-insights plugin not enabled"}]
    records = top.get("top_queries") or []
    if not records:
        return []

    def latency(rec: dict) -> float:
        m = rec.get("measurements", {}).get("latency", {})
        return float(m.get("number", rec.get("latency", 0)) or 0)

    hottest = sorted(records, key=latency, reverse=True)[: t["query_cost_top_n"]]
    findings: list[dict] = []
    for rec in hottest:
        indices = rec.get("indices") or ["unknown"]
        findings.append(_finding(
            "opensearch_query_cost_hotspot", profile,
            {"index_pattern": ",".join(indices)},
            severity="medium", confidence="medium",
            evidence={
                "latency_ms": latency(rec),
                "indices": indices,
                "search_type": rec.get("search_type"),
                "total_shards": rec.get("total_shards"),
            },
            impact={"resource_types": ["cpu", "search_latency"], "estimated_direction": "reduce"},
            action={"type": "review_query_shape",
                    "description": "High-cost query observed; review query shape, caching, and shard fan-out."},
        ))
    return findings


ANALYZERS = {
    "opensearch_oversharding": analyze_oversharding,
    "opensearch_shard_skew": analyze_shard_skew,
    "opensearch_segment_waste": analyze_segment_waste,
    "opensearch_lifecycle_gap": analyze_lifecycle_gap,
    "opensearch_query_cost_hotspot": analyze_query_cost_hotspot,
}


def run_all(client: OpenSearch, profile: dict) -> tuple[list[dict], list[dict]]:
    """Returns (findings, skipped_markers)."""
    t = {**DEFAULT_THRESHOLDS, **(profile.get("thresholds") or {})}
    findings: list[dict] = []
    skipped: list[dict] = []
    for fn in ANALYZERS.values():
        for item in fn(client, profile, t):
            (skipped if "skipped" in item else findings).append(item)
    return findings, skipped
