"""Run the AI-only P2 internal gate against local historical daily bars."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.research.p2_ai_internal_certification import certify_ai_internal_replay
from data_svc.unified_data_query import UnifiedDataQuery


DEFAULT_P1_AUDIT = (
    PROJECT_ROOT
    / "data"
    / "research_control"
    / "p1_annual_reports"
    / "2025"
    / "p1_ai_review_audit.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "data"
    / "research_control"
    / "p2_ai_internal"
    / "replay_report.json"
)


def run(*, p1_audit_path: Path, output_path: Path, end: str) -> dict:
    audit = json.loads(p1_audit_path.read_text(encoding="utf-8"))
    symbols = [
        item["symbol"]
        for item in audit.get("decision", {}).get("companies", [])
        if item.get("passed") is True and item.get("symbol")
    ][:10]
    end_date = datetime.strptime(end, "%Y-%m-%d").date()
    scan_start = (end_date - timedelta(days=120)).isoformat()
    query = UnifiedDataQuery()
    trading_dates = query.get_trade_dates(scan_start, end)
    market_rows = {}
    for symbol in symbols:
        frame = query.get_stock_history(
            symbol,
            scan_start,
            end,
            columns=["trade_date", "open", "high", "low", "close", "volume"],
        )
        market_rows[symbol] = frame.to_dict(orient="records")

    report = certify_ai_internal_replay(
        p1_audit=audit,
        trading_dates=trading_dates,
        market_rows_by_symbol=market_rows,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--p1-audit", type=Path, default=DEFAULT_P1_AUDIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--end", default=date.today().isoformat())
    args = parser.parse_args()
    report = run(
        p1_audit_path=args.p1_audit,
        output_path=args.output,
        end=args.end,
    )
    print(json.dumps({
        "status": report["status"],
        "company_count": report["company_count"],
        "observation_count": report["observation_count"],
        "leakage_count": report["leakage_count"],
        "missing_market_inputs": report["missing_market_inputs"],
        "evidence_sha256": report["evidence_sha256"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))
    return 0 if report["overall_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
