"""Tests for the ops-advisor skill.

No running OpenSearch cluster is required — all client calls are monkeypatched.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Insert the ops-advisor scripts/lib onto the path so we can import it directly.
_OPS_LIB = Path(__file__).resolve().parents[1] / "skills" / "opensearch-skills" / "operations" / "ops-advisor" / "scripts"
sys.path.insert(0, str(_OPS_LIB))

from lib.analyzers import (
    DEFAULT_THRESHOLDS,
    analyze_oversharding,
    analyze_shard_skew,
    analyze_segment_waste,
    run_all,
)
from lib.store import evaluate_divergence, init_indices, retrieve_verdicts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_client():
    return MagicMock()


def _make_shards(*sizes: int, index: str = "test-index") -> list[dict]:
    """Build a fake _cat/shards response."""
    return [{"index": index, "prirep": "p", "store": str(s)} for s in sizes]


SAMPLE_PROFILE = {
    "profile_id": "opensearch-self-v1",
    "system": "opensearch",
    "cluster_id": "test-cluster",
    "index_pattern": "*",
}

# ---------------------------------------------------------------------------
# Oversharding
# ---------------------------------------------------------------------------

def test_oversharding_detected():
    client = _mock_client()
    # 10 tiny shards: each 512 MiB → well below the 5 GiB threshold
    client.cat.shards.return_value = _make_shards(*([512 * 1024**2] * 10))
    findings = analyze_oversharding(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert len(findings) == 1
    f = findings[0]
    assert f["type"] == "opensearch_oversharding"
    assert f["evidence"]["primary_shards"] == 10
    assert f["severity"] in ("low", "medium", "high")
    assert f["confidence"] in ("low", "medium", "high")
    assert f["safety"]["execution"] == "manual"


def test_oversharding_not_detected_large_shards():
    client = _mock_client()
    # 10 large shards: each 10 GiB → above the small-shard threshold
    client.cat.shards.return_value = _make_shards(*([10 * 1024**3] * 10))
    findings = analyze_oversharding(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert findings == []


def test_oversharding_not_detected_few_shards():
    client = _mock_client()
    # Only 3 tiny shards → below min_primary_shards
    client.cat.shards.return_value = _make_shards(*([512 * 1024**2] * 3))
    findings = analyze_oversharding(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert findings == []


# ---------------------------------------------------------------------------
# Shard skew
# ---------------------------------------------------------------------------

def test_shard_skew_detected():
    client = _mock_client()
    # One giant shard, several tiny ones
    sizes = [1 * 1024**3] + [100 * 1024**2] * 4   # max / median ≈ 10×
    client.cat.shards.return_value = _make_shards(*sizes)
    findings = analyze_shard_skew(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert len(findings) == 1
    assert findings[0]["type"] == "opensearch_shard_skew"
    assert findings[0]["evidence"]["skew_ratio"] > DEFAULT_THRESHOLDS["skew_ratio"]


def test_shard_skew_not_detected_uniform():
    client = _mock_client()
    client.cat.shards.return_value = _make_shards(*([1 * 1024**3] * 5))
    findings = analyze_shard_skew(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert findings == []


# ---------------------------------------------------------------------------
# Segment waste
# ---------------------------------------------------------------------------

def test_segment_waste_detected():
    client = _mock_client()
    client.indices.stats.return_value = {
        "indices": {
            "my-index": {
                "primaries": {
                    "docs": {"count": 100, "deleted": 50},
                    "store": {"size_in_bytes": 1024**3},
                }
            }
        }
    }
    findings = analyze_segment_waste(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert len(findings) == 1
    assert findings[0]["type"] == "opensearch_segment_waste"
    assert findings[0]["evidence"]["deleted_ratio"] == pytest.approx(50 / 150, abs=0.001)


def test_segment_waste_not_detected_below_threshold():
    client = _mock_client()
    client.indices.stats.return_value = {
        "indices": {
            "my-index": {
                "primaries": {
                    "docs": {"count": 900, "deleted": 10},
                    "store": {"size_in_bytes": 1024**3},
                }
            }
        }
    }
    findings = analyze_segment_waste(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert findings == []


# ---------------------------------------------------------------------------
# Finding schema contract
# ---------------------------------------------------------------------------

def test_finding_schema_fields():
    client = _mock_client()
    client.cat.shards.return_value = _make_shards(*([512 * 1024**2] * 10))
    findings = analyze_oversharding(client, SAMPLE_PROFILE, DEFAULT_THRESHOLDS)
    assert findings, "expected at least one finding"
    f = findings[0]
    for required in ("finding_id", "type", "domain", "severity", "confidence",
                     "namespace", "evidence", "impact", "suggested_action", "safety", "observed_at"):
        assert required in f, f"missing field: {required}"
    assert f["namespace"]["cluster_id"] == "test-cluster"


# ---------------------------------------------------------------------------
# run_all graceful degradation
# ---------------------------------------------------------------------------

def test_run_all_skips_missing_plugins():
    client = _mock_client()
    client.cat.shards.return_value = []
    client.indices.stats.return_value = {"indices": {}}
    client.transport.perform_request.side_effect = Exception("plugin not available")
    findings, skipped = run_all(client, SAMPLE_PROFILE)
    skipped_types = {s["skipped"] for s in skipped}
    assert "opensearch_lifecycle_gap" in skipped_types
    assert "opensearch_query_cost_hotspot" in skipped_types


# ---------------------------------------------------------------------------
# Store: divergence evaluation
# ---------------------------------------------------------------------------

def test_divergence_reraise_pct_increase():
    verdict = {
        "evidence_snapshot": {"primary_shards": 100},
        "reraise_policy": {"metric": "primary_shards", "type": "pct_increase", "threshold": 0.5},
    }
    result = evaluate_divergence(verdict, {"primary_shards": 160})
    assert result["reraise"] is True
    assert "60%" in result["reason"]


def test_divergence_no_reraise_within_bounds():
    verdict = {
        "evidence_snapshot": {"primary_shards": 100},
        "reraise_policy": {"metric": "primary_shards", "type": "pct_increase", "threshold": 0.5},
    }
    result = evaluate_divergence(verdict, {"primary_shards": 120})
    assert result["reraise"] is False


def test_divergence_abs_increase():
    verdict = {
        "evidence_snapshot": {"deleted_ratio": 0.2},
        "reraise_policy": {"metric": "deleted_ratio", "type": "abs_increase", "threshold": 0.1},
    }
    result = evaluate_divergence(verdict, {"deleted_ratio": 0.35})
    assert result["reraise"] is True


def test_divergence_missing_metric():
    verdict = {
        "evidence_snapshot": {},
        "reraise_policy": {"metric": "primary_shards", "type": "pct_increase", "threshold": 0.5},
    }
    result = evaluate_divergence(verdict, {"primary_shards": 200})
    assert result["reraise"] is False


# ---------------------------------------------------------------------------
# Store: init_indices is idempotent
# ---------------------------------------------------------------------------

def test_init_indices_idempotent():
    client = _mock_client()
    client.indices.exists.return_value = True
    created = init_indices(client)
    assert created == []
    client.indices.create.assert_not_called()


def test_init_indices_creates_when_missing():
    client = _mock_client()
    client.indices.exists.return_value = False
    created = init_indices(client)
    assert len(created) == 2
    assert client.indices.create.call_count == 2


# ---------------------------------------------------------------------------
# Store: retrieve_verdicts returns empty list when index missing
# ---------------------------------------------------------------------------

def test_retrieve_verdicts_handles_missing_index():
    from opensearchpy.exceptions import NotFoundError
    client = _mock_client()
    client.search.side_effect = NotFoundError(404, "index not found", {})
    verdicts = retrieve_verdicts(client, "opensearch_oversharding", {"cluster_id": "c1"})
    assert verdicts == []


# ---------------------------------------------------------------------------
# Profile JSON is valid
# ---------------------------------------------------------------------------

def test_profile_json_valid():
    profile_path = (
        Path(__file__).resolve().parents[1]
        / "skills" / "opensearch-skills" / "operations" / "ops-advisor"
        / "profiles" / "opensearch-self.json"
    )
    with open(profile_path) as f:
        profile = json.load(f)
    assert "profile_id" in profile
    assert "system" in profile
    assert "index_pattern" in profile
