"""Report formatting helpers for PatternRadar."""

from __future__ import annotations

from typing import Any, Dict, List


def build_pattern_report(
    pattern_id: str,
    pattern_name: str,
    start_date: str,
    end_date: str,
    params: Dict[str, Any],
    matches: List[Dict[str, Any]],
) -> Dict[str, Any]:
    success_cases = [item for item in matches if item.get("success_label") is True]
    failure_cases = [item for item in matches if item.get("success_label") is False]
    current_candidates = [item for item in matches if item.get("success_label") is None]
    success_rate = len(success_cases) / len(success_cases + failure_cases) if success_cases or failure_cases else None

    return {
        "pattern_id": pattern_id,
        "pattern_name": pattern_name,
        "start_date": start_date,
        "end_date": end_date,
        "params": params,
        "summary": {
            "total_matches": len(matches),
            "success_cases": len(success_cases),
            "failure_cases": len(failure_cases),
            "current_candidates": len(current_candidates),
            "success_rate": success_rate,
        },
        "results": matches,
        "success_samples": success_cases,
        "failure_samples": failure_cases,
        "current_candidates": current_candidates,
        "research_boundary": "仅用于形态研究和样本统计，不构成买入、卖出或仓位建议。",
    }
