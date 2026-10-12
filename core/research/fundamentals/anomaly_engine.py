"""P1.2 异常检测引擎：基于多期 NormalizedFinancialStatement 检测异常信号。

按 P1 计划 Task P1.2 至少检测：
- 应计利润异常（accrual_ratio 异常高）
- 利润现金背离（cash_conversion 远低于 1）
- 异常毛利率（与同行业或历史均值偏离大）
- 异常营运资本（应收/存货激增而收入未增）

硬门禁：
- 异常是审查提示，不自动等同财务造假
- 每个异常有 observation（事实）和 conclusion_boundary（结论边界）
- 严重度分级：info / warning / severe
- 缺数据时不触发异常（不产生假阳性）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from .contracts import NormalizedFinancialStatement
from .quality_engine import (
    METRIC_OK,
    METRIC_MISSING,
    QualityMetric,
    compute_quality_metrics,
)


# 严重度
SEVERITY_INFO: str = "info"
SEVERITY_WARNING: str = "warning"
SEVERITY_SEVERE: str = "severe"

# 阈值（基于历史经验，非硬规则）
ACCRUAL_RATIO_WARNING: float = 0.10  # 应计利润 / 总资产 > 10%
ACCRUAL_RATIO_SEVERE: float = 0.20
CASH_CONVERSION_WARNING: float = 0.50  # 经营现金流 / 净利润 < 0.5
CASH_CONVERSION_SEVERE: float = 0.0  # 经营现金流为负
GROSS_MARGIN_DROP_WARNING: float = 0.05  # 毛利率同比下滑 5pct
GROSS_MARGIN_DROP_SEVERE: float = 0.15  # 毛利率同比下滑 15pct
RECEIVABLE_GROWTH_VS_REVENUE_WARNING: float = 1.5  # 应收增长 > 收入增长 1.5 倍
INVENTORY_GROWTH_VS_REVENUE_WARNING: float = 1.5

CONCLUSION_BOUNDARY: str = (
    "异常是审查提示，不等于财务造假；需结合行业周期、会计政策与"
    "原始凭证进一步判断。本引擎只负责标记观察点，不下结论。"
)


@dataclass
class AnomalySignal:
    """异常信号。

    每个信号必须有 observation（观察到的事实），
    不能包含"造假""欺诈"等结论词。

    Attributes:
        name: 异常名（如 "abnormal_accrual"）
        severity: 严重度（info / warning / severe）
        observation: 观察到的事实（不带结论）
        evidence_fields: 触发字段
        period: 期间
        threshold: 触发阈值（用于审计）
        actual_value: 实际值
    """

    name: str = ""
    severity: str = SEVERITY_INFO
    observation: str = ""
    evidence_fields: list[str] = field(default_factory=list)
    period: str = ""
    threshold: float | None = None
    actual_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "severity": self.severity,
            "observation": self.observation,
            "evidence_fields": list(self.evidence_fields),
            "period": self.period,
            "threshold": self.threshold,
            "actual_value": self.actual_value,
        }


@dataclass
class AnomalyReport:
    """异常报告。

    Attributes:
        signals: 异常信号列表
        conclusion_boundary: 结论边界声明
    """

    signals: list[AnomalySignal] = field(default_factory=list)
    conclusion_boundary: str = CONCLUSION_BOUNDARY

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "signals": [s.to_dict() for s in self.signals],
            "conclusion_boundary": self.conclusion_boundary,
        }


def _line_value(
    statement: NormalizedFinancialStatement,
    statement_type: str,
    metric: str,
) -> float | None:
    """从三表中取指标值。"""
    line = statement.get_line(statement_type, metric)
    if line is None or line.value is None:
        return None
    try:
        value = float(line.value)
    except (OverflowError, TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _finite_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def detect_anomalies(
    current: NormalizedFinancialStatement,
    previous: NormalizedFinancialStatement | None = None,
    industry_median_gross_margin: float | None = None,
) -> AnomalyReport:
    """检测异常信号。

    遵循验收点：
    - 异常是审查提示，不自动等同财务造假
    - 缺数据时不触发异常（避免假阳性）

    Args:
        current: 当期标准化三表
        previous: 同口径上一期（可选）
        industry_median_gross_margin: 行业毛利率中位数（可选）

    Returns:
        AnomalyReport 实例，含异常信号列表与结论边界声明
    """
    signals: list[AnomalySignal] = []
    period = current.period

    quality = compute_quality_metrics(current, previous)

    # ---- 1. 应计利润异常 ----
    # accrual_ratio = (net_profit - operating_cash_flow) / total_assets
    net_profit = _line_value(current, "income", "net_profit")
    operating_cf = _line_value(current, "cash_flow", "operating_cash_flow")
    total_assets = _line_value(current, "balance_sheet", "total_assets")

    if net_profit is not None and operating_cf is not None and total_assets is not None:
        accrual = net_profit - operating_cf
        if total_assets > 0:
            accrual_ratio = accrual / total_assets
            severity = SEVERITY_INFO
            if accrual_ratio >= ACCRUAL_RATIO_SEVERE:
                severity = SEVERITY_SEVERE
            elif accrual_ratio >= ACCRUAL_RATIO_WARNING:
                severity = SEVERITY_WARNING
            if severity != SEVERITY_INFO:
                signals.append(AnomalySignal(
                    name="abnormal_accrual",
                    severity=severity,
                    observation=(
                        f"应计利润占总资产 {accrual_ratio:.2%}，"
                        f"超过阈值 {ACCRUAL_RATIO_WARNING:.0%} (warning) / "
                        f"{ACCRUAL_RATIO_SEVERE:.0%} (severe)"
                    ),
                    evidence_fields=["net_profit", "operating_cash_flow", "total_assets"],
                    period=period,
                    threshold=ACCRUAL_RATIO_WARNING,
                    actual_value=accrual_ratio,
                ))

    # ---- 2. 利润现金背离 ----
    cash_conversion = quality.metrics.get("cash_conversion")
    cc_value = _finite_value(cash_conversion.value) if cash_conversion is not None else None
    if cc_value is not None:
        if net_profit is not None and net_profit > 0:
            if cc_value < CASH_CONVERSION_SEVERE:
                signals.append(AnomalySignal(
                    name="cash_profit_divergence",
                    severity=SEVERITY_SEVERE,
                    observation=(
                        f"经营现金流/净利润 = {cc_value:.2f}，"
                        f"净利润为正但现金流为负"
                    ),
                    evidence_fields=["operating_cash_flow", "net_profit"],
                    period=period,
                    threshold=CASH_CONVERSION_SEVERE,
                    actual_value=cc_value,
                ))
            elif cc_value < CASH_CONVERSION_WARNING:
                signals.append(AnomalySignal(
                    name="cash_profit_divergence",
                    severity=SEVERITY_WARNING,
                    observation=(
                        f"经营现金流/净利润 = {cc_value:.2f}，"
                        f"低于阈值 {CASH_CONVERSION_WARNING}"
                    ),
                    evidence_fields=["operating_cash_flow", "net_profit"],
                    period=period,
                    threshold=CASH_CONVERSION_WARNING,
                    actual_value=cc_value,
                ))

    # ---- 3. 毛利率异常 ----
    # 3a. 同比下滑
    if previous is not None:
        gm_curr = quality.metrics.get("gross_margin")
        gm_prev_metric = compute_quality_metrics(previous).metrics.get("gross_margin")
        gm_curr_value = _finite_value(gm_curr.value) if gm_curr is not None else None
        gm_prev_value = _finite_value(gm_prev_metric.value) if gm_prev_metric is not None else None
        if gm_curr_value is not None and gm_prev_value is not None:
            drop = gm_prev_value - gm_curr_value
            if drop >= GROSS_MARGIN_DROP_SEVERE:
                signals.append(AnomalySignal(
                    name="gross_margin_drop_yoy",
                    severity=SEVERITY_SEVERE,
                    observation=(
                        f"毛利率同比下滑 {drop:.2%}，"
                        f"超过 severe 阈值 {GROSS_MARGIN_DROP_SEVERE:.0%}"
                    ),
                    evidence_fields=["gross_profit", "revenue"],
                    period=period,
                    threshold=GROSS_MARGIN_DROP_SEVERE,
                    actual_value=drop,
                ))
            elif drop >= GROSS_MARGIN_DROP_WARNING:
                signals.append(AnomalySignal(
                    name="gross_margin_drop_yoy",
                    severity=SEVERITY_WARNING,
                    observation=(
                        f"毛利率同比下滑 {drop:.2%}，"
                        f"超过 warning 阈值 {GROSS_MARGIN_DROP_WARNING:.0%}"
                    ),
                    evidence_fields=["gross_profit", "revenue"],
                    period=period,
                    threshold=GROSS_MARGIN_DROP_WARNING,
                    actual_value=drop,
                ))
    # 3b. 行业中位数偏离
    if industry_median_gross_margin is not None:
        industry_margin = _finite_value(industry_median_gross_margin)
        gm_curr = quality.metrics.get("gross_margin")
        gm_curr_value = _finite_value(gm_curr.value) if gm_curr is not None else None
        if industry_margin is not None and gm_curr_value is not None:
            if gm_curr is not None:
                gm_curr.value = gm_curr_value
            industry_median_gross_margin = industry_margin
            deviation = gm_curr_value - industry_margin
            if deviation <= -GROSS_MARGIN_DROP_SEVERE:
                signals.append(AnomalySignal(
                    name="gross_margin_industry_divergence",
                    severity=SEVERITY_SEVERE,
                    observation=(
                        f"毛利率 {gm_curr.value:.2%} 低于行业中位数 "
                        f"{industry_median_gross_margin:.2%}，"
                        f"偏离 {deviation:.2%}"
                    ),
                    evidence_fields=["gross_profit", "revenue"],
                    period=period,
                    threshold=-GROSS_MARGIN_DROP_SEVERE,
                    actual_value=deviation,
                ))

    # ---- 4. 异常营运资本 ----
    # 应收/存货增长 vs 收入增长
    if previous is not None:
        revenue_curr = _line_value(current, "income", "revenue")
        revenue_prev = _line_value(previous, "income", "revenue")
        ar_curr = _line_value(current, "balance_sheet", "accounts_receivable")
        ar_prev = _line_value(previous, "balance_sheet", "accounts_receivable")
        inv_curr = _line_value(current, "balance_sheet", "inventory")
        inv_prev = _line_value(previous, "balance_sheet", "inventory")

        if (
            revenue_curr is not None and revenue_prev is not None
            and revenue_prev > 0 and ar_curr is not None and ar_prev is not None
            and ar_prev > 0
        ):
            rev_growth = revenue_curr / revenue_prev - 1.0
            ar_growth = ar_curr / ar_prev - 1.0
            if rev_growth >= 0 and ar_growth > rev_growth * RECEIVABLE_GROWTH_VS_REVENUE_WARNING:
                signals.append(AnomalySignal(
                    name="abnormal_receivable_growth",
                    severity=SEVERITY_WARNING,
                    observation=(
                        f"应收账款增长 {ar_growth:.2%} 显著高于"
                        f"收入增长 {rev_growth:.2%}（倍数 {ar_growth / max(rev_growth, 0.001):.1f}x）"
                    ),
                    evidence_fields=["accounts_receivable", "revenue"],
                    period=period,
                    threshold=RECEIVABLE_GROWTH_VS_REVENUE_WARNING,
                    actual_value=ar_growth,
                ))

        if (
            revenue_curr is not None and revenue_prev is not None
            and revenue_prev > 0 and inv_curr is not None and inv_prev is not None
            and inv_prev > 0
        ):
            rev_growth = revenue_curr / revenue_prev - 1.0
            inv_growth = inv_curr / inv_prev - 1.0
            if rev_growth >= 0 and inv_growth > rev_growth * INVENTORY_GROWTH_VS_REVENUE_WARNING:
                signals.append(AnomalySignal(
                    name="abnormal_inventory_growth",
                    severity=SEVERITY_WARNING,
                    observation=(
                        f"存货增长 {inv_growth:.2%} 显著高于"
                        f"收入增长 {rev_growth:.2%}（倍数 {inv_growth / max(rev_growth, 0.001):.1f}x）"
                    ),
                    evidence_fields=["inventory", "revenue"],
                    period=period,
                    threshold=INVENTORY_GROWTH_VS_REVENUE_WARNING,
                    actual_value=inv_growth,
                ))

    return AnomalyReport(signals=signals, conclusion_boundary=CONCLUSION_BOUNDARY)
