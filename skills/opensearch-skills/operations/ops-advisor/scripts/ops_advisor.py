# /// script
# requires-python = ">=3.11"
# dependencies = ["opensearch-py>=2.0"]
# ///
"""Ops Advisor CLI — the deterministic half of the skill.

The judgment loop is driven by the agent following SKILL.md; this CLI provides
the building blocks the agent calls. All output is JSON so the agent can consume it.

Commands:
  preflight        check connectivity and which optional plugins are present
  init             create the findings/verdicts indices (run once per cluster)
  analyze          run the analyzer pack, store findings, print results
  verdicts         retrieve prior operator verdicts for a finding type + namespace
  record-verdict   persist an operator verdict
  check-divergence evaluate whether a suppressed finding should be re-raised
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from any cwd: insert the shared scripts lib and the local lib.
_SHARED_LIB = Path(__file__).resolve().parents[4] / "scripts"
_LOCAL_LIB = Path(__file__).resolve().parent / "lib"
for _p in (_SHARED_LIB, _LOCAL_LIB.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from lib.client import create_client, preflight_check_cluster  # shared upstream client
from lib.analyzers import run_all  # noqa: E402 — local lib
from lib.store import (  # noqa: E402
    evaluate_divergence,
    index_findings,
    init_indices,
    record_verdict,
    retrieve_verdicts,
)


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_preflight(args) -> None:
    result = preflight_check_cluster()
    if result["status"] != "available":
        _out(result)
        return
    client = create_client()
    info = client.info()
    result["cluster_name"] = info.get("cluster_name")
    result["version"] = info.get("version", {}).get("number")
    result["plugins"] = {}
    try:
        client.transport.perform_request("GET", "/_plugins/_ism/policies", params={"size": "0"})
        result["plugins"]["index-management"] = True
    except Exception:
        result["plugins"]["index-management"] = False
    try:
        client.transport.perform_request("GET", "/_insights/top_queries", params={"type": "latency"})
        result["plugins"]["query-insights"] = True
    except Exception:
        result["plugins"]["query-insights"] = False
    _out(result)


def cmd_init(args) -> None:
    client = create_client()
    _out({"created": init_indices(client)})


def cmd_analyze(args) -> None:
    with open(args.profile) as f:
        profile = json.load(f)
    client = create_client()
    findings, skipped = run_all(client, profile)
    if not args.dry_run:
        init_indices(client)
        index_findings(client, findings)
    _out({
        "profile": profile.get("profile_id"),
        "finding_count": len(findings),
        "findings": findings,
        "skipped": skipped,
        "stored": not args.dry_run,
    })


def cmd_verdicts(args) -> None:
    client = create_client()
    ns = {"cluster_id": args.cluster_id, "index_pattern": args.index_pattern}
    _out({"verdicts": retrieve_verdicts(client, args.finding_type, ns)})


def cmd_record_verdict(args) -> None:
    with open(args.file) as f:
        verdict = json.load(f)
    client = create_client()
    init_indices(client)
    _out({"recorded": record_verdict(client, verdict)})


def cmd_check_divergence(args) -> None:
    with open(args.verdict) as f:
        verdict = json.load(f)
    with open(args.evidence) as f:
        evidence = json.load(f)
    _out(evaluate_divergence(verdict, evidence))


def main() -> int:
    p = argparse.ArgumentParser(description="OpenSearch Ops Advisor MVP")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight")
    sub.add_parser("init")

    a = sub.add_parser("analyze")
    a.add_argument("--profile", required=True, help="path to semantic profile JSON")
    a.add_argument("--dry-run", action="store_true", help="do not write findings to index")

    v = sub.add_parser("verdicts")
    v.add_argument("--finding-type", required=True)
    v.add_argument("--cluster-id", required=True)
    v.add_argument("--index-pattern", default="")

    rv = sub.add_parser("record-verdict")
    rv.add_argument("--file", required=True, help="path to verdict JSON file")

    cd = sub.add_parser("check-divergence")
    cd.add_argument("--verdict", required=True, help="path to verdict JSON file")
    cd.add_argument("--evidence", required=True, help="path to current evidence JSON file")

    args = p.parse_args()
    handlers = {
        "preflight": cmd_preflight,
        "init": cmd_init,
        "analyze": cmd_analyze,
        "verdicts": cmd_verdicts,
        "record-verdict": cmd_record_verdict,
        "check-divergence": cmd_check_divergence,
    }
    try:
        handlers[args.command](args)
    except (RuntimeError, FileNotFoundError, json.JSONDecodeError) as e:
        _out({"error": str(e)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
