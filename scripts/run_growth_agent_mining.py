#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research_graph.growth_mining import GrowthMiningEngine, GrowthMiningRequest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run generic Growth Agent mining into staging artifacts.")
    parser.add_argument("--topic", default="")
    parser.add_argument("--root-theme", default="")
    parser.add_argument("--node", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--min-evidence", type=int, default=1)
    parser.add_argument("--live-mode", action="store_true")
    parser.add_argument("--use-system-search", action="store_true")
    parser.add_argument("--allow-llm-assist", action="store_true")
    parser.add_argument("--apply-candidate", action="store_true")
    parser.add_argument("--acceptance-profile", default="data/config/research_graph_growth_acceptance.json")
    parser.add_argument("--run-root", default="data/research_runs")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--max-search-results", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request = GrowthMiningRequest(
        topic=args.topic,
        root_theme=args.root_theme,
        node=args.node,
        company=args.company,
        symbol=args.symbol,
        min_evidence=args.min_evidence,
        live_mode=args.live_mode,
        use_system_search=args.use_system_search,
        allow_llm_assist=args.allow_llm_assist,
        apply_candidate=args.apply_candidate,
        acceptance_profile_path=Path(args.acceptance_profile),
        run_root=Path(args.run_root),
        run_name=args.run_name,
        max_search_results=args.max_search_results,
    )
    result = GrowthMiningEngine(request).run()
    print(json.dumps({
        "run_dir": str(result.run_dir),
        "run_status": result.run_status,
        "support_evidence_count": result.support_evidence_count,
        "validation_status": result.validation_summary.get("status"),
    }, ensure_ascii=False, indent=2))
    return 0 if result.validation_summary.get("status") == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
