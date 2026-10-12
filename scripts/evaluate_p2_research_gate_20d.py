"""Evaluate the reconstructed 20-day research gate on local real bars."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.research.p2_research_gate_performance import evaluate_research_gate
from data_svc.unified_data_query import UnifiedDataQuery


P1_AUDIT = (
    PROJECT_ROOT / "data" / "research_control" / "p1_annual_reports"
    / "2025" / "p1_ai_review_audit.json"
)
P0_SHADOW = (
    PROJECT_ROOT / "data" / "research_value_runs"
    / "shadow_20260718T170623Z" / "shadow_acceptance.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "data" / "research_control" / "p2_ai_internal"
    / "research_gate_performance_20d.json"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default="2026-07-22")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    p1 = json.loads(P1_AUDIT.read_text(encoding="utf-8"))
    p0 = json.loads(P0_SHADOW.read_text(encoding="utf-8"))
    gated = [
        item["symbol"]
        for item in p1["decision"]["companies"]
        if item.get("passed") is True
    ]
    shadow_symbols = [
        item["symbol"] for item in p0["rows"] if item.get("symbol")
    ]
    all_symbols = sorted(set(shadow_symbols + gated))

    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    start = (end_date - timedelta(days=120)).isoformat()
    query = UnifiedDataQuery()
    trading_dates = query.get_trade_dates(start, args.end)
    bars = {}
    for symbol in all_symbols:
        frame = query.get_stock_history(
            symbol, start, args.end, columns=["trade_date", "open", "close"]
        )
        bars[symbol] = frame.to_dict(orient="records")

    report = evaluate_research_gate(
        bars_by_symbol=bars,
        all_symbols=all_symbols,
        gated_symbols=gated,
        trading_dates=trading_dates,
        top_k=3,
        evaluation_days=20,
        momentum_lookback=5,
    )
    report.pop("evidence_sha256", None)
    report.update({
        "research_gate_definition": (
            "P1_AI_CERTIFIED company universe plus P2 strict PIT replay"
        ),
        "selection_timestamp_bias": True,
        "selection_timestamp_bias_note": (
            "AI review was completed after the evaluation window; this is a "
            "retrospective reconstruction, not contemporaneously stored signals."
        ),
        "production_signal_history_present": False,
        "source": "local AquaTrade daily_ohlcv",
    })
    canonical = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    report["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        key: report[key]
        for key in (
            "overall_valid",
            "evaluation_days",
            "all_company_count",
            "research_gated_company_count",
            "leakage_count",
            "unresolved_execution_count",
            "research_gated",
            "active_baseline",
            "passive_equal_weight",
            "selection_timestamp_bias",
            "evidence_sha256",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["overall_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
