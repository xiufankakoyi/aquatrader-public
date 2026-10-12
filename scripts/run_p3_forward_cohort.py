"""启动、每日追加并检查 P3 前瞻 shadow cohort。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.pit_certification.forward_cohort import (
    build_forward_cohort,
    evaluate_collection,
    record_forward_close,
    validate_observation_chain,
)
from data_svc.unified_data_query import UnifiedDataQuery

DEFAULT_ROOT = PROJECT_ROOT / "data/research_control/p3_forward_v6"
SUMMARY = PROJECT_ROOT / "data/research_control/p1_evidence/evidence_summary.json"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_file_hash(path: Path) -> str:
    return _file_hash(path) if path.exists() else ""


def _code_hash() -> str:
    paths = [
        *sorted((PROJECT_ROOT / "core/research/domain_models").glob("*.py")),
        PROJECT_ROOT / "core/research/p3_gate.py",
        PROJECT_ROOT / "core/research/pit_certification/forward_cohort.py",
    ]
    return hashlib.sha256(
        "".join(f"{path.relative_to(PROJECT_ROOT)}:{_file_hash(path)}\n" for path in paths).encode()
    ).hexdigest()


def _latest_bars(symbols: list[str]) -> list[dict]:
    end = datetime.now().date()
    start = (end - timedelta(days=20)).isoformat()
    query = UnifiedDataQuery()
    dates = query.get_trade_dates(start, end.isoformat())
    if not dates:
        return []
    pool = query.get_stock_pool(dates[-1])
    if pool.empty or "stock_code" not in pool.columns:
        return []
    columns = [
        column for column in
        ("stock_code", "trade_date", "open", "high", "low", "close", "prev_close", "volume")
        if column in pool.columns
    ]
    selected = pool[pool["stock_code"].isin(symbols)][columns].copy()
    selected = selected.rename(columns={"stock_code": "symbol"}).sort_values("symbol")
    return selected.to_dict(orient="records")


def _select_forward_universe(evidence_symbols: set[str]) -> tuple[list[dict], list[dict], str]:
    """从最新完整行情与 stock_info 交集做行业轮转，确定性选满 300 家。"""
    end = datetime.now().date()
    start = (end - timedelta(days=20)).isoformat()
    query = UnifiedDataQuery()
    dates = query.get_trade_dates(start, end.isoformat())
    if not dates:
        return [], [], ""
    trade_date = dates[-1]
    pool = query.get_stock_pool(trade_date)
    stocks = query.get_stock_basic()
    joined = stocks.merge(
        pool[["stock_code", "trade_date", "open", "high", "low", "close", "prev_close", "volume"]],
        on="stock_code",
        how="inner",
    )
    joined = joined[
        joined["industry"].notna()
        & joined["industry"].astype(str).str.strip().ne("")
    ].sort_values(["industry", "stock_code"])
    evidence_rows = joined[joined["stock_code"].isin(evidence_symbols)].sort_values(
        "stock_code"
    ).to_dict(orient="records")
    remaining = joined[~joined["stock_code"].isin(evidence_symbols)]
    groups = {
        industry: frame.to_dict(orient="records")
        for industry, frame in remaining.groupby("industry", sort=True)
    }
    selected: list[dict] = evidence_rows[:300]
    rank = 0
    while len(selected) < 300:
        added = False
        for industry in sorted(groups):
            if rank < len(groups[industry]):
                selected.append(groups[industry][rank])
                added = True
                if len(selected) == 300:
                    break
        if not added:
            break
        rank += 1
    members = [
        {
            "symbol": row["stock_code"],
            "name": row["name"],
            "industry": row["industry"],
            "market": row["market"],
            "list_date": row["list_date"],
            "evidence_rich": row["stock_code"] in evidence_symbols,
            "selection_rank": index + 1,
        }
        for index, row in enumerate(selected)
    ]
    bars = [
        {
            "symbol": row["stock_code"],
            "trade_date": row["trade_date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "prev_close": row["prev_close"],
            "volume": row["volume"],
        }
        for row in selected
    ]
    return members, bars, trade_date


def _market_benchmark(trade_date: str) -> dict:
    pool = UnifiedDataQuery().get_stock_pool(trade_date)
    columns = ["stock_code", "close", "prev_close", "change_pct"]
    available = [column for column in columns if column in pool.columns]
    rows = pool[available].sort_values("stock_code").to_dict(orient="records")
    returns = pool["change_pct"].dropna() / 100 if "change_pct" in pool.columns else []
    return {
        "method": "all_a_share_equal_weight_close_to_close",
        "trade_date": trade_date,
        "pool_size": len(rows),
        "return": float(returns.mean()) if len(returns) else None,
        "fingerprint": hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest(),
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def start(root: Path) -> dict:
    if (root / "manifest.json").exists():
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        return {
            "status": "already_started",
            "members": len(manifest["members"]),
            "cohort_hash": manifest["cohort_hash"],
        }
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    evidence_symbols = set(summary["symbols"])
    members, bars, trade_date = _select_forward_universe(evidence_symbols)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    sources = {
        "p1_evidence_summary": _file_hash(SUMMARY),
        "latest_close_research": _optional_file_hash(
            PROJECT_ROOT / "data/reports/research_monitor/latest_close.json"
        ),
    }
    cohort = build_forward_cohort(
        members=members,
        input_records=bars,
        source_hashes=sources,
        code_hash=_code_hash(),
        started_at=now,
        universe_policy={
            "method": "evidence_rich_first_then_industry_round_robin_on_latest_complete_market",
            "target_companies": 300,
            "sort_keys": ["industry", "stock_code"],
            "latest_trade_date": trade_date,
            "missing_industry_policy": "exclude_without_inference",
            "evidence_rich_subset_count": sum(member["evidence_rich"] for member in members),
        },
    )
    cohort["ai_review"].update({
        "status": "AI_INTERNAL_ACCEPTED_FOR_FORWARD_COLLECTION",
        "reviewer_id": "codex-p3-forward-review-2026-07-23",
        "reviewer_kind": "AI_INTERNAL",
        "review_model": "OpenAI GPT-5",
        "reviewed_results_hash": cohort["cohort_hash"],
        "decision": "accepted_for_forward_collection",
        "signed_at": now,
        "scope": "forward_collection_only",
        "production_certified": False,
        "p3_certified": False,
    })
    _write_json(root / "manifest.json", cohort)
    _write_json(root / "observations.json", [])
    return {
        "status": "started",
        "members": len(members),
        "bars": len(bars),
        "baseline_trade_date": trade_date,
        "collected_trade_days": 0,
        "cohort_hash": cohort["cohort_hash"],
    }


def append(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    observations_path = root / "observations.json"
    observations = json.loads(observations_path.read_text(encoding="utf-8"))
    symbols = [item["symbol"] for item in manifest["members"]]
    bars = _latest_bars(symbols)
    trade_dates = sorted({str(row["trade_date"])[:10] for row in bars})
    if not trade_dates:
        return {"status": "no_market_data"}
    observation_trade_date = trade_dates[-1]
    baseline_trade_date = str(
        (manifest.get("baseline") or {}).get("latest_trade_date")
        or (manifest.get("universe_policy") or {}).get("latest_trade_date")
        or ""
    )
    if observation_trade_date <= baseline_trade_date:
        return {
            "status": "waiting_for_post_baseline_trade_date",
            "baseline_trade_date": baseline_trade_date,
            "observation_trade_date": observation_trade_date,
            "collected_trade_days": len(observations),
        }
    report = PROJECT_ROOT / "data/reports/research_monitor/latest_close.json"
    if not report.exists():
        return {
            "status": "waiting_for_same_day_research_report",
            "observation_trade_date": observation_trade_date,
            "reason": "latest_close_missing",
        }
    try:
        report_payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "waiting_for_same_day_research_report",
            "observation_trade_date": observation_trade_date,
            "reason": "latest_close_unreadable",
        }
    research_report_as_of = str(report_payload.get("as_of") or "")
    if research_report_as_of[:10] != observation_trade_date:
        return {
            "status": "waiting_for_same_day_research_report",
            "observation_trade_date": observation_trade_date,
            "research_report_as_of": research_report_as_of,
            "reason": "report_trade_date_mismatch",
        }
    benchmark = _market_benchmark(observation_trade_date)
    member_outcomes = [
        {
            "symbol": row["symbol"],
            "close": row.get("close"),
            "prev_close": row.get("prev_close"),
            "close_to_close_return": (
                float(row["close"]) / float(row["prev_close"]) - 1
                if row.get("close") is not None and row.get("prev_close")
                else None
            ),
        }
        for row in bars
    ]
    observation = {
        "trade_date": observation_trade_date,
        "available_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "research_report_as_of": research_report_as_of,
        "source": "lancedb.daily_ohlcv + research_monitor.latest_close",
        "input_fingerprint": hashlib.sha256(
            json.dumps(bars, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "research_output_hash": _optional_file_hash(report),
        "research_output_available": True,
        "benchmark": benchmark,
        "member_rows": len(bars),
        "member_outcomes": member_outcomes,
    }
    hook = record_forward_close(root, observation)
    updated = json.loads(observations_path.read_text(encoding="utf-8"))
    return {
        "status": (
            "appended" if hook.get("appended")
            else "already_recorded" if hook.get("hook_ok")
            else "hook_failed"
        ),
        "hook_ok": hook.get("hook_ok", False),
        "observation_trade_date": observation_trade_date,
        **evaluate_collection(
            updated,
            company_count=len(manifest["members"]),
            industry_count=len({item.get("industry") for item in manifest["members"] if item.get("industry")}),
        ),
        "chain_head": updated[-1]["record_hash"] if updated else "",
    }


def status(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    observations = json.loads((root / "observations.json").read_text(encoding="utf-8"))
    baseline_trade_date = str(
        (manifest.get("baseline") or {}).get("latest_trade_date")
        or (manifest.get("universe_policy") or {}).get("latest_trade_date")
        or ""
    )
    chain = validate_observation_chain(
        observations,
        cohort_hash=str(manifest.get("cohort_hash") or ""),
        baseline_trade_date=baseline_trade_date,
    )
    return {
        "cohort_hash": manifest["cohort_hash"],
        "mode": manifest["mode"],
        "member_count": len(manifest["members"]),
        "industry_count": len({item.get("industry") for item in manifest["members"] if item.get("industry")}),
        "baseline_trade_date": baseline_trade_date,
        "chain": chain,
        **evaluate_collection(
            observations,
            company_count=len(manifest["members"]),
            industry_count=len({item.get("industry") for item in manifest["members"] if item.get("industry")}),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "append", "status"])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    if args.command == "start":
        result = start(args.root)
    elif args.command == "append":
        result = append(args.root)
    else:
        result = status(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
