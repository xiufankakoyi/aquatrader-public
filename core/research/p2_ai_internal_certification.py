"""AI-only P2 internal certification over a strict T-1 market replay.

This is intentionally separate from the institutional P2.10 acceptance path.
It proves that the existing engineering pipeline can replay twenty historical
trading days without using same-day or future daily bars.  It does not claim
external reviewer independence or institutional production certification.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


INSTITUTIONAL_DEFERRED_GATES = [
    "100_company_shadow",
    "30_complete_p1_models",
    "10_independently_reviewed_variant_perceptions",
    "trusted_external_attestation",
]


def _fingerprint(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        payload = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _passed_ai_symbols(p1_audit: dict[str, Any]) -> list[str]:
    if not isinstance(p1_audit, Mapping):
        return []
    decision = p1_audit.get("decision", {})
    if not isinstance(decision, Mapping):
        return []
    if not (
        decision.get("status") == "P1_AI_CERTIFIED"
        and decision.get("overall_passed") is True
        and decision.get("review_mode") == "ai_only_internal"
        and decision.get("human_independent_review") is False
        and decision.get("identity_independent") is False
    ):
        return []
    return sorted({
        str(item.get("symbol", ""))
        for item in decision.get("companies", [])
        if isinstance(item, Mapping) and item.get("passed") is True and item.get("symbol")
    })


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()[:10]


def _audit_companies(p1_audit: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(p1_audit, Mapping):
        return []
    companies = p1_audit.get("companies", [])
    if not isinstance(companies, (list, tuple)):
        return []
    return [item for item in companies if isinstance(item, Mapping)]


def certify_ai_internal_replay(
    *,
    p1_audit: dict[str, Any],
    trading_dates: list[str],
    market_rows_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Certify ten AI-reviewed companies over twenty strict T-1 observations."""
    symbols = _passed_ai_symbols(p1_audit)
    date_values = trading_dates if isinstance(trading_dates, (list, tuple)) else []
    dates = sorted({_date_text(value) for value in date_values if value})
    dates = [value for value in dates if value]
    market_rows = market_rows_by_symbol if isinstance(market_rows_by_symbol, Mapping) else {}
    failures: list[str] = []
    if len(symbols) < 10:
        failures.append("P1 AI-only 已通过公司不足 10 家")
    if len(dates) < 21:
        failures.append("严格 T-1 回放需要至少 21 个连续交易日期")

    symbols = symbols[:10]
    dates = dates[-21:]
    observation_runs: list[dict[str, Any]] = []
    missing_market_inputs = 0
    first_observation = dates[1] if len(dates) == 21 else ""
    p1_publication_dates = {
        str(item.get("symbol", "")): str(
            item.get("annual_report", {}).get("published_at", "")
        )[:10]
        for item in _audit_companies(p1_audit)
        if isinstance(item.get("annual_report"), Mapping)
    }
    future_p1_evidence_count = sum(
        1
        for symbol in symbols
        if not p1_publication_dates.get(symbol)
        or p1_publication_dates[symbol] > first_observation
    )
    if future_p1_evidence_count:
        failures.append(
            f"{future_p1_evidence_count} 家 P1 年报证据在回放首日尚不可见或缺少发布日期"
        )

    if len(dates) == 21:
        for index, observation_date in enumerate(dates[1:], start=1):
            cutoff = dates[index - 1]
            frozen_inputs: dict[str, list[dict[str, Any]]] = {}
            max_dates: list[str] = []
            missing_symbols: list[str] = []
            for symbol in symbols:
                rows = market_rows.get(symbol, [])
                if not isinstance(rows, (list, tuple)):
                    rows = []
                visible = [
                    row
                    for row in rows
                    if isinstance(row, Mapping)
                    and _date_text(row.get("trade_date")) <= cutoff
                ]
                if not visible:
                    missing_symbols.append(symbol)
                    missing_market_inputs += 1
                    continue
                visible.sort(key=lambda row: _date_text(row.get("trade_date")))
                frozen_inputs[symbol] = visible
                max_dates.append(_date_text(visible[-1].get("trade_date")))

            max_used = max(max_dates, default="")
            leakage = bool(max_used and max_used > cutoff)
            observation_runs.append({
                "observation_date": observation_date,
                "market_cutoff": cutoff,
                "max_market_date_used": max_used,
                "company_count": len(frozen_inputs),
                "missing_symbols": missing_symbols,
                "leakage_detected": leakage,
                "input_sha256": _fingerprint(frozen_inputs),
            })

    leakage_count = sum(
        int(item["leakage_detected"]) for item in observation_runs
    )
    if missing_market_inputs:
        failures.append(f"缺少 {missing_market_inputs} 个公司日次的 T-1 行情输入")
    if leakage_count:
        failures.append(f"检测到 {leakage_count} 个未来数据泄漏观察日")
    if len(observation_runs) != 20:
        failures.append(f"实际回放观察日为 {len(observation_runs)}，要求 20")

    passed = not failures
    report: dict[str, Any] = {
        "schema_version": "p2-ai-internal-v1",
        "status": (
            "P2_AI_INTERNAL_CERTIFIED"
            if passed
            else "P2_AI_INTERNAL_BLOCKED"
        ),
        "overall_passed": passed,
        "review_mode": "ai_only_internal",
        "human_independent_review": False,
        "identity_independent": False,
        "external_attestation_required": False,
        "institutional_certification": False,
        "institutional_deferred_gates": list(INSTITUTIONAL_DEFERRED_GATES),
        "company_count": len(symbols),
        "observation_count": len(observation_runs),
        "leakage_count": leakage_count,
        "future_p1_evidence_count": future_p1_evidence_count,
        "missing_market_inputs": missing_market_inputs,
        "market_visibility_rule": "observation D uses daily bars through previous trading date only",
        "observation_runs": observation_runs,
        "failures": failures,
    }
    report["evidence_sha256"] = _fingerprint(report)
    return report
