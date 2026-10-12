"""Domain firewall, walk-forward metrics, portfolio diagnostics, calibration gates."""
from __future__ import annotations

from math import isfinite, sqrt
from collections.abc import Mapping
from typing import Iterable

from .contracts import (
    AttributionReviewTask, CalibrationVersion, DomainFeedback, LearningDomain,
    PortfolioDiagnostics,
)


ALLOWED_ATTRIBUTIONS = {
    LearningDomain.KNOWLEDGE.value: {"claim_verified", "claim_rejected", "claim_retracted", "claim_expired"},
    LearningDomain.RESEARCH.value: {"forecast_error", "interval_coverage", "scenario_calibration"},
    LearningDomain.ALPHA.value: {"out_of_sample_ic", "excess_return", "signal_decay"},
    LearningDomain.EXECUTION.value: {"slippage", "transaction_cost", "fill_quality", "capacity_rejection"},
}


class DomainFeedbackFirewall:
    """Persist feedback in its own domain and emit review tasks for cross-domain learning."""

    def route(self, feedback: DomainFeedback) -> AttributionReviewTask | None:
        allowed = ALLOWED_ATTRIBUTIONS.get(feedback.domain, set())
        if feedback.attribution_type not in allowed:
            raise ValueError(f"attribution {feedback.attribution_type!r} is invalid for {feedback.domain!r}")
        # No feedback event carries target-domain parameter mutations. Cross-domain
        # consequences are represented only as review tasks with explicit provenance.
        return AttributionReviewTask(
            source_domain=feedback.domain, attribution_type=feedback.attribution_type,
            entity_id=feedback.entity_id,
            reason=f"review_from_{feedback.domain}:{feedback.attribution_type}",
        )


def pearson_ic(signals: list[float], outcomes: list[float]) -> float | None:
    if not isinstance(signals, (list, tuple)) or not isinstance(outcomes, (list, tuple)):
        return None
    if len(signals) != len(outcomes) or len(signals) < 2:
        return None
    pairs: list[tuple[float, float]] = []
    for signal, outcome in zip(signals, outcomes):
        try:
            signal_value, outcome_value = float(signal), float(outcome)
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(signal_value) and isfinite(outcome_value):
            pairs.append((signal_value, outcome_value))
    if len(pairs) < 2:
        return None
    signals = [pair[0] for pair in pairs]
    outcomes = [pair[1] for pair in pairs]
    sx, sy = sum(signals) / len(signals), sum(outcomes) / len(outcomes)
    numerator = sum((x - sx) * (y - sy) for x, y in zip(signals, outcomes))
    dx = sum((x - sx) ** 2 for x in signals)
    dy = sum((y - sy) ** 2 for y in outcomes)
    return numerator / sqrt(dx * dy) if dx > 0 and dy > 0 else None


def _ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    result = [0.0] * len(values)
    pos = 0
    while pos < len(ordered):
        end = pos + 1
        while end < len(ordered) and ordered[end][1] == ordered[pos][1]:
            end += 1
        rank = (pos + end - 1) / 2 + 1
        for idx in range(pos, end):
            result[ordered[idx][0]] = rank
        pos = end
    return result


def rank_ic(signals: list[float], outcomes: list[float]) -> float | None:
    if not isinstance(signals, (list, tuple)) or not isinstance(outcomes, (list, tuple)):
        return None
    if len(signals) != len(outcomes):
        return None
    pairs: list[tuple[float, float]] = []
    for signal, outcome in zip(signals, outcomes):
        try:
            signal_value, outcome_value = float(signal), float(outcome)
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(signal_value) and isfinite(outcome_value):
            pairs.append((signal_value, outcome_value))
    if len(pairs) < 2:
        return None
    return pearson_ic(
        _ranks([item[0] for item in pairs]),
        _ranks([item[1] for item in pairs]),
    )


def grouped_returns(signals: list[float], outcomes: list[float], groups: int = 5) -> dict[str, float]:
    if not isinstance(signals, (list, tuple)) or not isinstance(outcomes, (list, tuple)):
        return {}
    if len(signals) != len(outcomes) or not signals:
        return {}
    try:
        groups = int(groups)
    except (TypeError, ValueError, OverflowError):
        return {}
    if groups < 1:
        return {}
    pairs = []
    for signal, outcome in zip(signals, outcomes):
        try:
            signal_value, outcome_value = float(signal), float(outcome)
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(signal_value) and isfinite(outcome_value):
            pairs.append((signal_value, outcome_value))
    if not pairs:
        return {}
    ordered = sorted(pairs, key=lambda pair: pair[0])
    result: dict[str, float] = {}
    for group in range(groups):
        lo = group * len(ordered) // groups
        hi = (group + 1) * len(ordered) // groups
        bucket = [outcome for _, outcome in ordered[lo:hi]]
        if bucket:
            result[f"Q{group + 1}"] = sum(bucket) / len(bucket)
    return result


def walk_forward_windows(dates: list[str], train_size: int, test_size: int) -> list[dict[str, str]]:
    if not isinstance(dates, (list, tuple)):
        return []
    unique = sorted({date for date in dates if isinstance(date, str) and date})
    try:
        train_size, test_size = int(train_size), int(test_size)
    except (TypeError, ValueError, OverflowError):
        return []
    if train_size < 1 or test_size < 1:
        return []
    windows: list[dict[str, str]] = []
    start = 0
    while start + train_size + test_size <= len(unique):
        train = unique[start:start + train_size]
        test = unique[start + train_size:start + train_size + test_size]
        if train[-1] >= test[0]:
            raise ValueError("walk-forward leakage: train must precede test")
        windows.append({"train_start": train[0], "train_end": train[-1], "test_start": test[0], "test_end": test[-1]})
        start += test_size
    return windows


def diagnose_portfolio(
    rows: Iterable[dict], *, as_of: str, max_industry: float = .35,
    max_position: float = .1, max_liquidity: float = .2, max_capacity: float = .2,
) -> PortfolioDiagnostics:
    def safe_threshold(value: object, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if isfinite(parsed) else default

    max_industry = safe_threshold(max_industry, 0.35)
    max_position = safe_threshold(max_position, 0.1)
    max_liquidity = safe_threshold(max_liquidity, 0.2)
    max_capacity = safe_threshold(max_capacity, 0.2)
    industry: dict[str, float] = {}; style: dict[str, float] = {}
    beta = size = hhi = liquidity = capacity = 0.0; violations: list[str] = []
    for row in rows if rows is not None else []:
        if not isinstance(row, Mapping):
            continue
        try:
            weight = float(row.get("weight", 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if not isfinite(weight):
            continue
        industry[str(row.get("industry") or "未知")] = industry.get(str(row.get("industry") or "未知"), 0.0) + weight
        style_exposure = row.get("style_exposure") if isinstance(row.get("style_exposure"), Mapping) else {}
        for key, exposure in style_exposure.items():
            try:
                exposure_value = float(exposure)
            except (TypeError, ValueError, OverflowError):
                continue
            if isfinite(exposure_value):
                style[key] = style.get(key, 0.0) + weight * exposure_value
        def numeric(field: str) -> float:
            try:
                value = float(row.get(field, 0.0))
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return value if isfinite(value) else 0.0
        beta += weight * numeric("beta"); size += weight * numeric("size"); hhi += weight ** 2
        liquidity = max(liquidity, numeric("participation_rate"))
        capacity = max(capacity, numeric("capacity_utilization"))
        if abs(weight) > max_position: violations.append(f"position:{row.get('symbol', '')}")
    violations += [f"industry:{name}" for name, value in industry.items() if abs(value) > max_industry]
    if liquidity > max_liquidity: violations.append("liquidity")
    if capacity > max_capacity: violations.append("capacity")
    return PortfolioDiagnostics(as_of, industry, style, beta, size, hhi, liquidity, capacity, sorted(set(violations)))


def neutralize_signals(rows: list[dict], *, signal_key: str = "signal", style_keys: list[str] | None = None) -> list[float]:
    """Return industry-demeaned, style-residualized cross-sectional signals.

    This is a diagnostic transformation only; it does not produce positions.
    Missing industries are grouped as ``未知`` and zero-variance style columns
    are skipped safely.
    """
    if not rows:
        return []
    safe_rows = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, (list, tuple)) else []
    if not safe_rows:
        return []
    values = []
    for row in safe_rows:
        try:
            value = float(row.get(signal_key, 0.0))
        except (TypeError, ValueError, OverflowError):
            value = 0.0
        values.append(value if isfinite(value) else 0.0)
    groups: dict[str, list[int]] = {}
    for idx, row in enumerate(safe_rows): groups.setdefault(str(row.get("industry") or "未知"), []).append(idx)
    residual = list(values)
    for indexes in groups.values():
        mean = sum(values[i] for i in indexes) / len(indexes)
        for i in indexes: residual[i] -= mean
    safe_style_keys = (
        [key for key in style_keys if isinstance(key, str) and key]
        if isinstance(style_keys, (list, tuple)) else []
    )
    for key in safe_style_keys:
        style = []
        for row in safe_rows:
            exposure = row.get("style_exposure") if isinstance(row.get("style_exposure"), Mapping) else {}
            try:
                value = float(exposure.get(key, 0.0))
            except (TypeError, ValueError, OverflowError):
                value = 0.0
            style.append(value if isfinite(value) else 0.0)
        mean_x = sum(style) / len(style); mean_y = sum(residual) / len(residual)
        denominator = sum((x - mean_x) ** 2 for x in style)
        if denominator <= 0: continue
        beta = sum((x - mean_x) * (y - mean_y) for x, y in zip(style, residual)) / denominator
        residual = [y - beta * (x - mean_x) for x, y in zip(style, residual)]
    return residual


class CalibrationGate:
    def validate(self, version: CalibrationVersion) -> list[str]:
        if not isinstance(version, CalibrationVersion):
            return ["invalid_version"]
        reasons: list[str] = []
        if version.sample_size < version.minimum_sample_size: reasons.append("minimum_sample_not_met")
        parameters = version.parameters if isinstance(version.parameters, Mapping) else {}
        bounds_map = version.bounds if isinstance(version.bounds, Mapping) else {}
        for key, value in parameters.items():
            bounds = bounds_map.get(key)
            try:
                if bounds and not bounds[0] <= value <= bounds[1]: reasons.append(f"out_of_bounds:{key}")
            except (IndexError, TypeError):
                reasons.append(f"invalid_bounds:{key}")
        if version.status == "enabled" and not version.approved_by: reasons.append("manual_approval_required")
        if version.status == "rolled_back" and not version.rollback_of: reasons.append("rollback_target_required")
        if version.status not in {"shadow", "enabled", "rolled_back"}: reasons.append("invalid_status")
        return reasons
