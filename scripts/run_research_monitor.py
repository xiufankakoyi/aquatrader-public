#!/usr/bin/env python
"""Run AquaTrade premarket/intraday/close research monitoring."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 国内行情、公告与 Tushare 直连；失效的本机代理会让 dry-run 与 daemon 行为不一致。
for _proxy_key in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"
):
    os.environ.pop(_proxy_key, None)

from core.runtime.research_monitor import ResearchMonitor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("premarket", "monitor", "tail_research", "close", "push"),
        required=True,
    )
    parser.add_argument("--as-of", default="")
    parser.add_argument(
        "--output-dir",
        default=os.getenv("RESEARCH_MONITOR_OUTPUT_DIR", "data/reports/research_monitor"),
        help="监控输出目录（默认从 RESEARCH_MONITOR_OUTPUT_DIR 环境变量读取）",
    )
    parser.add_argument("--push", action="store_true", help="同日 dry-run 成功后才允许外部推送")
    parser.add_argument("--no-external-push", action="store_true", help="显式禁止外部推送")
    parser.add_argument("--fail-on-empty", action="store_true")
    parser.add_argument("--fail-on-degraded", action="store_true")
    parser.add_argument("--max-research-tasks", type=int, default=3)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = ResearchMonitor(
        output_dir=args.output_dir,
        persist_quant_signals=True,
    ).run(
        mode=args.mode,
        as_of=args.as_of,
        external_push=bool(args.push and not args.no_external_push),
        fail_on_empty=args.fail_on_empty,
        fail_on_degraded=args.fail_on_degraded,
        max_research_tasks=max(0, args.max_research_tasks),
    )
    theme_rows = result.payload.get("themes") or result.payload.get("theme_strength") or []
    observation_rows = (
        result.payload.get("observations") or result.payload.get("close_watchlist") or []
    )
    discovery = result.payload.get("dynamic_discovery") or {}
    print(json.dumps({
        "mode": result.mode,
        "status": result.status,
        "run_id": result.run_id,
        "as_of": result.as_of,
        "report_path": result.report_path,
        "json_path": result.json_path,
        "errors": result.errors,
        "exit_code": result.exit_code,
        "theme_count": len(theme_rows),
        "observation_count": len(observation_rows),
        "validated_observation_count": result.payload.get("validated_observation_count", 0),
        "ready_watch_count": result.payload.get("ready_watch_count", 0),
        "snapshot_count": result.payload.get(
            "snapshot_count", discovery.get("discovered_snapshot_count", 0)
        ),
        "stale_quote_count": result.payload.get("stale_quote_count", 0),
        "monitor_cycles": result.payload.get("monitor_cycles", 0),
        "push": result.payload.get("push", {}),
    }, ensure_ascii=False, indent=2))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
