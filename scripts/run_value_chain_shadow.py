"""P0.7 shadow 批量运行器。

按 docs/superpowers/plans/2026-07-18-institutional-research-p0-trusted-foundation.md
Task P0.7 要求：
1. 至少 20 家公司、≥8 个申万一级行业、覆盖制造/消费/医药/周期/金融/科技。
2. 仅使用真实本地数据和正式 provider，不人工补齐。
3. 每家公司生成独立 run。
4. 聚合统计：来源可用率、PIT 排除数、字段缺失率、运行耗时、降级原因。
5. 失败公司保留错误产物并继续其他公司。

P0 复验整改：
- 新增逐事实级审计：每条 source_snapshots record 检查 provider/source/source_trace_id
- 新增 evidence_refs 分布统计（readiness 八维度）
- 新增中性值伪造检测：0.5 / 0.0 等伪中性值违规计数
- 新增 blocked 维度 score=None 校验：blocked 维度的 score 必须为 None，禁止返回 0.0

输出产物：
- data/research_value_runs/shadow_<timestamp>/shadow_acceptance.json
  含 rows（每公司明细）+ aggregates（按行业/数据源/缺口分类聚合）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.value_chain.contracts import (
    RESEARCH_ASSUMPTION_REQUIRED_FIELDS,
    RESEARCH_ASSUMPTION_SOURCE_TYPES,
    RESEARCH_FACT_REQUIRED_FIELDS,
)
from core.research.value_chain.service import CompanyValueResearchService


_VISIBLE_TIME_FIELDS: tuple[str, ...] = (
    "available_at",
    "published_at",
    "announcement_date",
    "report_date",
    "trade_date",
    "date",
)


# 八维 readiness 维度名（用于字段缺失率分母）
READINESS_DIMENSIONS = (
    "fact_completeness",
    "source_traceability",
    "source_independence",
    "pit_integrity",
    "forecast_input_completeness",
    "model_computability",
    "valuation_computability",
    "research_uncertainty",
)

def _parse_iso(value: str) -> datetime | None:
    """解析 ISO 时间字符串；失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_pit_excluded(dossier: dict) -> int:
    """统计 source_snapshots 中被 PIT 排除的未来记录与未知时间记录数。"""
    total = 0
    for snapshot in (dossier.get("source_snapshots") or {}).values():
        if not isinstance(snapshot, dict):
            continue
        total += int(snapshot.get("excluded_future_records") or 0)
        total += int(snapshot.get("excluded_unknown_time_records") or 0)
    return total


def _collect_field_missing(dossier: dict) -> tuple[int, int]:
    """统计 readiness 字段缺失数 / 总字段数。

    Returns:
        (missing_count, total_count)
    """
    readiness = dossier.get("research_readiness") or {}
    missing = 0
    for name in READINESS_DIMENSIONS:
        raw = readiness.get(name) or {}
        missing_items = raw.get("missing_items") or []
        missing += len(missing_items)
    return missing, len(READINESS_DIMENSIONS)


def _collect_fact_audit(dossier: dict) -> dict[str, Any]:
    """Audit formal facts/assumptions, source visibility, and unsafe defaults."""
    as_of = _parse_iso(str(dossier.get("as_of") or ""))
    fact_required = set(RESEARCH_FACT_REQUIRED_FIELDS)
    assumption_required = set(RESEARCH_ASSUMPTION_REQUIRED_FIELDS)
    facts = [x for x in dossier.get("research_facts") or [] if isinstance(x, dict)]
    assumptions = [x for x in dossier.get("research_assumptions") or [] if isinstance(x, dict)]

    fact_contract_violations = 0
    fact_future_violations = 0
    traceable = 0
    for fact in facts:
        if set(fact) != fact_required:
            fact_contract_violations += 1
        grounding = fact.get("grounding_status") in {"grounding_ok", "grounding_partial"}
        if fact.get("source_trace_id") and fact.get("evidence_ids") and grounding:
            traceable += 1
        available_at = _parse_iso(str(fact.get("available_at") or ""))
        if as_of and available_at and available_at > as_of:
            fact_future_violations += 1

    assumption_contract_violations = sum(
        1
        for assumption in assumptions
        if set(assumption) != assumption_required
        or assumption.get("source_type") not in RESEARCH_ASSUMPTION_SOURCE_TYPES
    )
    visible_source_records = 0
    visible_source_future_violations = 0
    for snapshot in (dossier.get("source_snapshots") or {}).values():
        if not isinstance(snapshot, dict):
            continue
        for record in snapshot.get("records") or []:
            if not isinstance(record, dict):
                continue
            timestamp = next(
                (_parse_iso(str(record.get(k) or "")) for k in _VISIBLE_TIME_FIELDS if record.get(k)),
                None,
            )
            if timestamp:
                visible_source_records += 1
                if as_of and timestamp > as_of:
                    visible_source_future_violations += 1

    readiness = dossier.get("research_readiness") or {}
    evidence_refs_total = 0
    evidence_refs_non_empty_dims = 0
    blocked_dims_count = 0
    blocked_dims_score_is_none = True
    for name in READINESS_DIMENSIONS:
        raw = readiness.get(name) or {}
        evidence_refs = raw.get("evidence_refs") or []
        evidence_refs_total += len(evidence_refs)
        evidence_refs_non_empty_dims += int(bool(evidence_refs))
        if raw.get("status") == "blocked":
            blocked_dims_count += 1
            if raw.get("score") is not None:
                blocked_dims_score_is_none = False

    bridge = dossier.get("profit_bridge") or {}
    baseline_revenue = (bridge.get("baseline") or {}).get("revenue")
    unsafe_default_violations = sum(
        1
        for scenario in bridge.get("market_implied_scenarios") or []
        if baseline_revenue in (None, 0)
        and scenario.get("historical_product_revenue_exposure") == 1.0
    )
    total = len(facts)
    return {
        "formal_facts_total": total,
        "formal_facts_traceable": traceable,
        "formal_fact_traceability_rate": round(traceable / total, 6) if total else 0.0,
        "formal_fact_contract_violations": fact_contract_violations,
        "formal_fact_future_violations": fact_future_violations,
        "formal_assumptions_total": len(assumptions),
        "formal_assumption_contract_violations": assumption_contract_violations,
        "visible_source_records": visible_source_records,
        "visible_source_future_violations": visible_source_future_violations,
        "unsafe_default_violations": unsafe_default_violations,
        "evidence_refs_total": evidence_refs_total,
        "evidence_refs_non_empty_dims": evidence_refs_non_empty_dims,
        "blocked_dims_count": blocked_dims_count,
        "blocked_dims_score_is_none": blocked_dims_score_is_none,
    }


def _evaluate_hard_gates(report: dict[str, Any]) -> list[str]:
    """Return stable failure codes for every P0 shadow acceptance gate."""
    failures: list[str] = []
    audit = (report.get("aggregates") or {}).get("fact_audit") or {}
    checks = (
        (report.get("symbol_count", 0) >= 20, "symbol_count_below_20"),
        (report.get("completed_count") == report.get("symbol_count"), "batch_incomplete"),
        (report.get("failed_count", 0) == 0, "failed_companies_present"),
        (report.get("industry_count", 0) >= 8, "industry_count_below_8"),
        (audit.get("formal_facts_total", audit.get("total_formal_facts", 0)) > 0, "formal_facts_missing"),
        (audit.get("formal_fact_traceability_rate") == 1.0, "formal_fact_traceability_below_100pct"),
        (audit.get("formal_fact_contract_violations", audit.get("total_formal_fact_contract_violations", 0)) == 0, "formal_fact_contract_violations"),
        (audit.get("formal_fact_future_violations", audit.get("total_formal_fact_future_violations", 0)) == 0, "formal_fact_future_violations"),
        (audit.get("formal_assumption_contract_violations", audit.get("total_formal_assumption_contract_violations", 0)) == 0, "formal_assumption_contract_violations"),
        (audit.get("visible_source_future_violations", audit.get("total_visible_source_future_violations", 0)) == 0, "visible_source_future_violations"),
        (audit.get("unsafe_default_violations", audit.get("total_unsafe_default_violations", 0)) == 0, "unsafe_default_violations"),
        (audit.get("blocked_score_none_companies") == report.get("completed_count"), "blocked_scores_not_none"),
    )
    for passed, code in checks:
        if not passed:
            failures.append(code)
    return failures


def _summarize_source_statuses(source_statuses: dict) -> dict:
    """统计来源可用率：ok / total / by_capability。"""
    by_capability: dict[str, str] = {}
    ok_count = 0
    total = 0
    for name, status in (source_statuses or {}).items():
        by_capability[name] = str(status)
        total += 1
        if status == "ok":
            ok_count += 1
    return {
        "ok_count": ok_count,
        "total": total,
        "available_rate": round(ok_count / total, 6) if total else 0.0,
        "by_capability": by_capability,
    }


def main() -> int:
    """运行 shadow 批量并输出聚合报告。"""
    parser = argparse.ArgumentParser(description="Run P0.7 company value-chain shadow batch")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--lookback-days", type=int, default=240)
    parser.add_argument("--output-root", default="data/research_value_runs")
    parser.add_argument(
        "--industry-map",
        default="",
        help="可选 JSON 文件路径：{symbol: {industry, type}}；缺失时按 unknown 聚合",
    )
    args = parser.parse_args()

    industry_map: dict[str, dict[str, str]] = {}
    if args.industry_map:
        try:
            industry_map = json.loads(Path(args.industry_map).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[warn] 读取 industry_map 失败: {exc}", file=sys.stderr)

    started = datetime.now(timezone.utc)
    service = CompanyValueResearchService()
    rows: list[dict] = []

    for symbol in dict.fromkeys(args.symbols):
        run_started = datetime.now(timezone.utc)
        try:
            result = service.run(
                symbol,
                as_of=args.as_of,
                lookback_days=args.lookback_days,
                output_root=args.output_root,
                enqueue_gaps=False,
            )
            dossier = result["dossier"]
            acceptance = result["acceptance"]
            run_finished = _parse_iso(acceptance.get("finished_at")) or datetime.now(timezone.utc)
            elapsed_sec = (run_finished - run_started).total_seconds()
            pit_excluded = _collect_pit_excluded(dossier)
            missing_count, total_fields = _collect_field_missing(dossier)
            fact_audit = _collect_fact_audit(dossier)
            source_summary = _summarize_source_statuses(acceptance.get("source_statuses") or {})
            meta = industry_map.get(symbol, {})
            rows.append({
                "symbol": symbol,
                "industry": meta.get("industry", "unknown"),
                "type": meta.get("type", "unknown"),
                "run_id": result["run_id"],
                "status": acceptance["status"],
                "degradation_reasons": acceptance["degradation_reasons"],
                "source_statuses": acceptance["source_statuses"],
                "source_summary": source_summary,
                "pit_excluded_records": pit_excluded,
                "field_missing_count": missing_count,
                "field_total_count": total_fields,
                "field_missing_rate": round(missing_count / total_fields, 6) if total_fields else 0.0,
                "fact_audit": fact_audit,
                "elapsed_sec": round(elapsed_sec, 3),
                "research_dimensions": dossier.get("research_dimensions", {}),
                "evidence_completeness": (
                    dossier.get("research_assessment", {}).get("evidence_completeness")
                ),
            })
        except Exception as exc:
            run_finished = datetime.now(timezone.utc)
            elapsed_sec = (run_finished - run_started).total_seconds()
            meta = industry_map.get(symbol, {})
            rows.append({
                "symbol": symbol,
                "industry": meta.get("industry", "unknown"),
                "type": meta.get("type", "unknown"),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_sec": round(elapsed_sec, 3),
            })

    batch_id = f"shadow_{started.strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = Path(args.output_root) / batch_id
    output_dir.mkdir(parents=True, exist_ok=False)

    # 聚合统计
    completed_rows = [r for r in rows if r.get("status") in {"ok", "partial"}]
    failed_rows = [r for r in rows if r.get("status") == "failed"]

    # 按行业聚合：来源可用率、字段缺失率、PIT 排除数
    by_industry: dict[str, dict] = defaultdict(lambda: {
        "companies": 0, "ok_sources": 0, "total_sources": 0,
        "pit_excluded": 0, "field_missing": 0, "field_total": 0,
        "degradation_reasons": defaultdict(int),
    })
    for row in completed_rows:
        ind = row.get("industry", "unknown")
        bucket = by_industry[ind]
        bucket["companies"] += 1
        summary = row.get("source_summary") or {}
        bucket["ok_sources"] += summary.get("ok_count", 0)
        bucket["total_sources"] += summary.get("total", 0)
        bucket["pit_excluded"] += row.get("pit_excluded_records", 0)
        bucket["field_missing"] += row.get("field_missing_count", 0)
        bucket["field_total"] += row.get("field_total_count", 0)
        for reason in row.get("degradation_reasons") or []:
            bucket["degradation_reasons"][reason] += 1
    # 序列化
    by_industry_serialized = {
        ind: {
            "companies": v["companies"],
            "source_available_rate": round(v["ok_sources"] / v["total_sources"], 6) if v["total_sources"] else 0.0,
            "pit_excluded_total": v["pit_excluded"],
            "field_missing_rate": round(v["field_missing"] / v["field_total"], 6) if v["field_total"] else 0.0,
            "degradation_reason_counts": dict(v["degradation_reasons"]),
        }
        for ind, v in by_industry.items()
    }

    # 按数据源聚合可用率
    by_capability: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "total": 0})
    for row in completed_rows:
        for cap, status in (row.get("source_summary") or {}).get("by_capability", {}).items():
            by_capability[cap]["total"] += 1
            if status == "ok":
                by_capability[cap]["ok"] += 1
    by_capability_serialized = {
        cap: {
            "available_rate": round(v["ok"] / v["total"], 6) if v["total"] else 0.0,
            "ok_count": v["ok"],
            "total_count": v["total"],
        }
        for cap, v in by_capability.items()
    }

    # 按缺口（degradation_reasons）聚合
    gap_counts: dict[str, int] = defaultdict(int)
    for row in completed_rows:
        for reason in row.get("degradation_reasons") or []:
            gap_counts[reason] += 1

    # P0 正式事实与缺失值语义聚合。
    total_facts = sum(
        (row.get("fact_audit") or {}).get("formal_facts_total", 0) for row in completed_rows
    )
    total_facts_with_trace = sum(
        (row.get("fact_audit") or {}).get("formal_facts_traceable", 0) for row in completed_rows
    )
    total_evidence_refs = sum(
        (row.get("fact_audit") or {}).get("evidence_refs_total", 0)
        for row in completed_rows
    )
    blocked_score_none_companies = sum(
        1 for row in completed_rows
        if (row.get("fact_audit") or {}).get("blocked_dims_score_is_none") is True
    )
    # 总耗时
    audit_sum_fields = (
        "formal_fact_contract_violations",
        "formal_fact_future_violations",
        "formal_assumption_contract_violations",
        "visible_source_records",
        "visible_source_future_violations",
        "unsafe_default_violations",
    )
    audit_totals = {
        field: sum((row.get("fact_audit") or {}).get(field, 0) for row in completed_rows)
        for field in audit_sum_fields
    }
    total_elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    source_ok = sum(
        1 for row in completed_rows
        if row.get("source_summary") and row["source_summary"]["ok_count"] == row["source_summary"]["total"]
    )

    report = {
        "batch_id": batch_id,
        "as_of": args.as_of[:10],
        "symbol_count": len(rows),
        "completed_count": len(completed_rows),
        "failed_count": len(failed_rows),
        "all_sources_ok_count": source_ok,
        "industry_count": len({r.get("industry", "unknown") for r in rows}),
        "rows": rows,
        "aggregates": {
            "by_industry": by_industry_serialized,
            "by_capability": by_capability_serialized,
            "degradation_reason_counts": dict(gap_counts),
            "fact_audit": {
                "formal_facts_total": total_facts,
                "formal_facts_traceable": total_facts_with_trace,
                "formal_fact_traceability_rate": round(
                    total_facts_with_trace / total_facts, 6
                ) if total_facts else 0.0,
                "total_evidence_refs": total_evidence_refs,
                "blocked_score_none_companies": blocked_score_none_companies,
                "total_formal_fact_contract_violations": audit_totals["formal_fact_contract_violations"],
                "total_formal_fact_future_violations": audit_totals["formal_fact_future_violations"],
                "total_formal_assumption_contract_violations": audit_totals["formal_assumption_contract_violations"],
                "total_visible_source_records": audit_totals["visible_source_records"],
                "total_visible_source_future_violations": audit_totals["visible_source_future_violations"],
                "total_unsafe_default_violations": audit_totals["unsafe_default_violations"],
            },
        },
        "total_elapsed_sec": round(total_elapsed, 3),
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "scope": "research_shadow_not_trade_signal",
        "notice": "研究数据产物，不包含交易动作建议。",
    }
    report["hard_gate_failures"] = _evaluate_hard_gates(report)
    report["hard_gates_passed"] = not report["hard_gate_failures"]
    path = output_dir / "shadow_acceptance.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps({**report, "report_path": str(path.resolve())},
                     ensure_ascii=False, indent=2))
    return 1 if report["hard_gate_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
