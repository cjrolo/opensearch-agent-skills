"""Supporting state: findings index, verdicts index, and verdict retrieval.

MVP stand-in for the proposal's agentic-memory layer. Retrieval is deterministic
scope-matching (finding_type + namespace). The upgrade path to semantic recall via
a k-NN / agentic-memory index is noted in reference/semantic-profile.md.
"""

from __future__ import annotations

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError

FINDINGS_INDEX = "ops-advisor-findings"
VERDICTS_INDEX = "ops-advisor-verdicts"

FINDINGS_MAPPING = {
    "mappings": {
        "properties": {
            "finding_id": {"type": "keyword"},
            "type": {"type": "keyword"},
            "domain": {"type": "keyword"},
            "severity": {"type": "keyword"},
            "confidence": {"type": "keyword"},
            "namespace": {
                "properties": {
                    "cluster_id": {"type": "keyword"},
                    "index_pattern": {"type": "keyword"},
                }
            },
            "evidence": {"type": "object", "enabled": True},
            "observed_at": {"type": "date"},
        }
    }
}

VERDICTS_MAPPING = {
    "mappings": {
        "properties": {
            "verdict_id": {"type": "keyword"},
            "finding_type": {"type": "keyword"},
            "judgment": {"type": "keyword"},
            "scope": {
                "properties": {
                    "level": {"type": "keyword"},
                    "cluster_id": {"type": "keyword"},
                    "index_pattern": {"type": "keyword"},
                }
            },
            "rationale": {"type": "text"},
            "evidence_snapshot": {"type": "object", "enabled": True},
            "reraise_policy": {"type": "object", "enabled": True},
            "created_by": {"type": "keyword"},
            "created_at": {"type": "date"},
        }
    }
}


def init_indices(client: OpenSearch) -> list[str]:
    created = []
    for name, mapping in ((FINDINGS_INDEX, FINDINGS_MAPPING), (VERDICTS_INDEX, VERDICTS_MAPPING)):
        if not client.indices.exists(index=name):
            client.indices.create(index=name, body=mapping)
            created.append(name)
    return created


def index_findings(client: OpenSearch, findings: list[dict]) -> int:
    for f in findings:
        client.index(
            index=FINDINGS_INDEX,
            id=f["finding_id"],
            body=f,
            params={"refresh": "true"},
        )
    return len(findings)


def retrieve_verdicts(client: OpenSearch, finding_type: str, namespace: dict) -> list[dict]:
    """Exact-scope retrieval: same finding type and overlapping namespace.

    Matches verdicts whose scope.cluster_id equals the finding's cluster_id and
    whose index_pattern is either identical to the finding's or absent (cluster-level
    scope applies to all patterns).
    """
    filters: list[dict] = [{"term": {"finding_type": finding_type}}]
    if namespace.get("cluster_id"):
        filters.append({"term": {"scope.cluster_id": namespace["cluster_id"]}})
    query: dict = {
        "size": 20,
        "query": {
            "bool": {
                "filter": filters,
                "should": [
                    {"term": {"scope.index_pattern": namespace.get("index_pattern", "")}},
                    {"bool": {"must_not": {"exists": {"field": "scope.index_pattern"}}}},
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [{"created_at": "desc"}],
    }
    try:
        resp = client.search(index=VERDICTS_INDEX, body=query)
    except (NotFoundError, Exception):
        return []
    return [h["_source"] for h in resp.get("hits", {}).get("hits", [])]


def record_verdict(client: OpenSearch, verdict: dict) -> str:
    vid = verdict["verdict_id"]
    client.index(
        index=VERDICTS_INDEX,
        id=vid,
        body=verdict,
        params={"refresh": "true"},
    )
    return vid


def evaluate_divergence(verdict: dict, current_evidence: dict) -> dict:
    """Decide whether a suppressed finding should be re-raised.

    reraise_policy schema:
      {"metric": "primary_shards", "type": "pct_increase", "threshold": 0.5}
      {"metric": "deleted_ratio",  "type": "abs_increase", "threshold": 0.1}

    Returns {"reraise": bool, "reason": str}.
    """
    policy = verdict.get("reraise_policy") or {}
    metric = policy.get("metric")
    snapshot = verdict.get("evidence_snapshot") or {}
    if not metric or metric not in snapshot or metric not in current_evidence:
        return {"reraise": False, "reason": "no comparable metric in policy/snapshot"}
    before, after = snapshot[metric], current_evidence[metric]
    ptype, thr = policy.get("type"), policy.get("threshold", 0)
    if ptype == "pct_increase" and before:
        delta = (after - before) / before
        if delta >= thr:
            return {"reraise": True, "reason": f"{metric} rose {delta:.0%} (>= {thr:.0%}) vs snapshot"}
    elif ptype == "abs_increase":
        if (after - before) >= thr:
            return {"reraise": True, "reason": f"{metric} rose by {after - before} (>= {thr}) vs snapshot"}
    return {"reraise": False, "reason": f"{metric} within suppression bounds"}
