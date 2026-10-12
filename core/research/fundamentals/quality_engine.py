"""P1.2 财务质量引擎：基于 NormalizedFinancialStatement 计算指标。

按 P1 计划 Task P1.2 至少计算：
- 收入、利润和扣非利润增长
- 毛利率、营业利润率、净利率
- ROE、ROIC、增量 ROIC
- 再投资率和可持续增长关系
- 经营现金流/净利润、自由现金流
- 应收、存货、合同负债周转
- 净债务、利息覆盖、短债压力
- 杜邦分解

硬门禁：
- 每个指标带公式、输入字段和期间
- 分母为 0/None/负值时 value=None，不返回 0.0 兜底
- 同比只比较同口径期间（previous period 的 NormalizedFinancialStatement）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from .contracts import NormalizedFinancialStatement, StatementLine


# 指标状态：与 P0 grounding_status 对齐
METRIC_OK: str = "ok"
METRIC_MISSING: str = "missing"
METRIC_NOT_COMPUTABLE: str = "not_computable"  # 分母为 0 或负值


@dataclass
class QualityMetric:
    """财务质量指标。

    每个指标必须带公式、输入字段和期间；缺数据时 value=None。

    Attributes:
        name: 指标名（如 "revenue_growth"）
        value: 数值；缺数据/不可计算为 None
        formula: 公式文本（人可读，如 "revenue_t / revenue_t-1 - 1"）
        input_fields: 输入字段名列表（如 ["revenue"]）
        period: 期间 YYYY-MM-DD
        unit: 单位（"ratio" / "percent" / "元" / "天"）
        status: 状态（ok / missing / not_computable）
        note: 备注（如 "分母为 0"）
    """

    name: str = ""
    value: float | None = None
    formula: str = ""
    input_fields: list[str] = field(default_factory=list)
    period: str = ""
    unit: str = ""
    status: str = METRIC_MISSING
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "value": self.value,
            "formula": self.formula,
            "input_fields": list(self.input_fields),
            "period": self.period,
            "unit": self.unit,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class QualityReport:
    """财务质量报告。"""

    status: str = "not_computable"  # ok / partial / not_computable
    metrics: dict[str, QualityMetric] = field(default_factory=dict)
    missing_metrics: list[str] = field(default_factory=list)
    dupont_decomposition: dict[str, QualityMetric] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "status": self.status,
            "metrics": {name: m.to_dict() for name, m in self.metrics.items()},
            "missing_metrics": list(self.missing_metrics),
            "dupont_decomposition": {
                name: m.to_dict() for name, m in self.dupont_decomposition.items()
            },
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


def _safe_div(
    numerator: float | None,
    denominator: float | None,
) -> tuple[float | None, str]:
    """安全除法。

    Args:
        numerator: 分子
        denominator: 分母

    Returns:
        (值, 状态)；分子或分母为 None 时返回 (None, missing)；
        分母为 0 或负值时返回 (None, not_computable)
    """
    numerator = _finite_value(numerator)
    denominator = _finite_value(denominator)
    if numerator is None or denominator is None:
        return None, METRIC_MISSING
    if denominator == 0:
        return None, METRIC_NOT_COMPUTABLE
    if denominator < 0:
        return None, METRIC_NOT_COMPUTABLE
    value = numerator / denominator
    return (value, METRIC_OK) if isfinite(value) else (None, METRIC_NOT_COMPUTABLE)


def _safe_growth(
    current: float | None,
    previous: float | None,
) -> tuple[float | None, str]:
    """同比增长率。

    Args:
        current: 当期值
        previous: 同期值

    Returns:
        (增长率, 状态)；previous 为 None 时 (None, missing)；
        previous <= 0 时 (None, not_computable)
    """
    current = _finite_value(current)
    previous = _finite_value(previous)
    if current is None or previous is None:
        return None, METRIC_MISSING
    if previous <= 0:
        return None, METRIC_NOT_COMPUTABLE
    value = current / previous - 1.0
    return (value, METRIC_OK) if isfinite(value) else (None, METRIC_NOT_COMPUTABLE)


def _safe_turnover_days(
    flow: float | None,
    stock: float | None,
) -> tuple[float | None, str]:
    """周转天数 = 365 * stock / flow。

    flow/stock 为 None → missing；flow <= 0 → not_computable。
    """
    flow = _finite_value(flow)
    stock = _finite_value(stock)
    if flow is None or stock is None:
        return None, METRIC_MISSING
    if flow <= 0:
        return None, METRIC_NOT_COMPUTABLE
    value = 365.0 * stock / flow
    return (value, METRIC_OK) if isfinite(value) else (None, METRIC_NOT_COMPUTABLE)


def _build_metric(
    name: str,
    value: float | None,
    status: str,
    formula: str,
    input_fields: list[str],
    period: str,
    unit: str,
    note: str = "",
) -> QualityMetric:
    """构造 QualityMetric 实例。"""
    return QualityMetric(
        name=name,
        value=value,
        formula=formula,
        input_fields=list(input_fields),
        period=period,
        unit=unit,
        status=status,
        note=note,
    )


def compute_quality_metrics(
    current: NormalizedFinancialStatement,
    previous: NormalizedFinancialStatement | None = None,
) -> QualityReport:
    """计算财务质量指标。

    遵循验收点：
    - 每个指标带公式、输入字段和期间
    - 分母为 0/None/负值时 value=None，status="not_computable"
    - 同比只比较同口径期间（previous 是同公司同期间上一年的 NormalizedFinancialStatement）

    Args:
        current: 当期标准化三表
        previous: 同口径上一期（可选，缺时同比指标 missing）

    Returns:
        QualityReport 实例
    """
    metrics: dict[str, QualityMetric] = {}
    period = current.period
    expected_metrics: list[str] = []

    # ---- 1. 增长指标（依赖 previous）----
    revenue_curr = _line_value(current, "income", "revenue")
    net_profit_curr = _line_value(current, "income", "net_profit")
    operating_profit_curr = _line_value(current, "income", "operating_profit")

    if previous is not None:
        revenue_prev = _line_value(previous, "income", "revenue")
        net_profit_prev = _line_value(previous, "income", "net_profit")
        operating_profit_prev = _line_value(previous, "income", "operating_profit")

        growth, status = _safe_growth(revenue_curr, revenue_prev)
        metrics["revenue_growth"] = _build_metric(
            "revenue_growth", growth, status,
            formula="revenue_t / revenue_t-1 - 1",
            input_fields=["revenue"], period=period, unit="ratio",
            note="previous 期间非正时不可计算",
        )

        growth, status = _safe_growth(net_profit_curr, net_profit_prev)
        metrics["net_profit_growth"] = _build_metric(
            "net_profit_growth", growth, status,
            formula="net_profit_t / net_profit_t-1 - 1",
            input_fields=["net_profit"], period=period, unit="ratio",
        )

        growth, status = _safe_growth(operating_profit_curr, operating_profit_prev)
        metrics["operating_profit_growth"] = _build_metric(
            "operating_profit_growth", growth, status,
            formula="operating_profit_t / operating_profit_t-1 - 1",
            input_fields=["operating_profit"], period=period, unit="ratio",
        )
    else:
        for name in ("revenue_growth", "net_profit_growth", "operating_profit_growth"):
            metrics[name] = _build_metric(
                name, None, METRIC_MISSING,
                formula="<needs previous period>",
                input_fields=[], period=period, unit="ratio",
                note="缺少同口径 previous 期间",
            )

    expected_metrics += ["revenue_growth", "net_profit_growth", "operating_profit_growth"]

    # ---- 2. 利润率指标 ----
    gross_profit = _line_value(current, "income", "gross_profit")
    ratio, status = _safe_div(gross_profit, revenue_curr)
    metrics["gross_margin"] = _build_metric(
        "gross_margin", ratio, status,
        formula="gross_profit / revenue",
        input_fields=["gross_profit", "revenue"], period=period, unit="ratio",
    )
    expected_metrics.append("gross_margin")

    ratio, status = _safe_div(operating_profit_curr, revenue_curr)
    metrics["operating_margin"] = _build_metric(
        "operating_margin", ratio, status,
        formula="operating_profit / revenue",
        input_fields=["operating_profit", "revenue"], period=period, unit="ratio",
    )
    expected_metrics.append("operating_margin")

    ratio, status = _safe_div(net_profit_curr, revenue_curr)
    metrics["net_margin"] = _build_metric(
        "net_margin", ratio, status,
        formula="net_profit / revenue",
        input_fields=["net_profit", "revenue"], period=period, unit="ratio",
    )
    expected_metrics.append("net_margin")

    # ---- 3. ROE / ROIC ----
    total_equity = _line_value(current, "balance_sheet", "total_equity")
    equity_parent = _line_value(current, "balance_sheet", "equity_parent")
    equity_for_roe = equity_parent if equity_parent is not None else total_equity
    ratio, status = _safe_div(net_profit_curr, equity_for_roe)
    metrics["roe"] = _build_metric(
        "roe", ratio, status,
        formula="net_profit / equity_parent (fallback total_equity)",
        input_fields=["net_profit", "equity_parent"], period=period, unit="ratio",
    )
    expected_metrics.append("roe")

    total_assets = _line_value(current, "balance_sheet", "total_assets")
    # ROIC 简化：净利 / (总资产 - 流动负债)，需要 total_current_liabilities
    current_liab = _line_value(current, "balance_sheet", "total_current_liabilities")
    invested_capital: float | None = None
    if total_assets is not None and current_liab is not None:
        invested_capital = _finite_value(total_assets - current_liab)
    ratio, status = _safe_div(net_profit_curr, invested_capital)
    metrics["roic"] = _build_metric(
        "roic", ratio, status,
        formula="net_profit / (total_assets - current_liabilities)",
        input_fields=["net_profit", "total_assets", "total_current_liabilities"],
        period=period, unit="ratio",
        note="简化口径，生产应使用 (有息负债 + 权益)",
    )
    expected_metrics.append("roic")

    # 增量 ROIC：需要 previous 的 invested_capital 与 NOPAT
    if previous is not None:
        prev_total_assets = _line_value(previous, "balance_sheet", "total_assets")
        prev_current_liab = _line_value(previous, "balance_sheet", "total_current_liabilities")
        prev_net_profit = _line_value(previous, "income", "net_profit")
        prev_invested = (
            _finite_value(prev_total_assets - prev_current_liab)
            if prev_total_assets is not None and prev_current_liab is not None
            else None
        )
        delta_nopat = (
            _finite_value(net_profit_curr - prev_net_profit)
            if net_profit_curr is not None and prev_net_profit is not None
            else None
        )
        delta_capital = (
            _finite_value(invested_capital - prev_invested)
            if invested_capital is not None and prev_invested is not None
            else None
        )
        ratio, status = _safe_div(delta_nopat, delta_capital)
        metrics["incremental_roic"] = _build_metric(
            "incremental_roic", ratio, status,
            formula="ΔNOPAT / ΔInvestedCapital",
            input_fields=["net_profit", "total_assets", "total_current_liabilities"],
            period=period, unit="ratio",
        )
    else:
        metrics["incremental_roic"] = _build_metric(
            "incremental_roic", None, METRIC_MISSING,
            formula="ΔNOPAT / ΔInvestedCapital",
            input_fields=[], period=period, unit="ratio",
            note="缺少 previous 期间",
        )
    expected_metrics.append("incremental_roic")

    # ---- 4. 现金流指标 ----
    operating_cf = _line_value(current, "cash_flow", "operating_cash_flow")
    capex = _line_value(current, "cash_flow", "capex")
    ratio, status = _safe_div(operating_cf, net_profit_curr)
    metrics["cash_conversion"] = _build_metric(
        "cash_conversion", ratio, status,
        formula="operating_cash_flow / net_profit",
        input_fields=["operating_cash_flow", "net_profit"], period=period, unit="ratio",
    )
    expected_metrics.append("cash_conversion")

    free_cf: float | None = None
    if operating_cf is not None and capex is not None:
        free_cf = _finite_value(operating_cf - capex)
    metrics["free_cash_flow"] = _build_metric(
        "free_cash_flow", free_cf, METRIC_OK if free_cf is not None else METRIC_MISSING,
        formula="operating_cash_flow - capex",
        input_fields=["operating_cash_flow", "capex"], period=period, unit="元",
    )
    expected_metrics.append("free_cash_flow")

    # ---- 5. 周转天数 ----
    accounts_receivable = _line_value(current, "balance_sheet", "accounts_receivable")
    inventory = _line_value(current, "balance_sheet", "inventory")
    contract_liab = _line_value(current, "balance_sheet", "contract_liabilities")
    operating_cost = _line_value(current, "income", "operating_cost")

    days, status = _safe_turnover_days(revenue_curr, accounts_receivable)
    metrics["accounts_receivable_turnover_days"] = _build_metric(
        "accounts_receivable_turnover_days", days, status,
        formula="365 * accounts_receivable / revenue",
        input_fields=["accounts_receivable", "revenue"], period=period, unit="天",
    )
    expected_metrics.append("accounts_receivable_turnover_days")

    days, status = _safe_turnover_days(operating_cost, inventory)
    metrics["inventory_turnover_days"] = _build_metric(
        "inventory_turnover_days", days, status,
        formula="365 * inventory / operating_cost",
        input_fields=["inventory", "operating_cost"], period=period, unit="天",
    )
    expected_metrics.append("inventory_turnover_days")

    days, status = _safe_turnover_days(revenue_curr, contract_liab)
    metrics["contract_liabilities_turnover_days"] = _build_metric(
        "contract_liabilities_turnover_days", days, status,
        formula="365 * contract_liabilities / revenue",
        input_fields=["contract_liabilities", "revenue"], period=period, unit="天",
    )
    expected_metrics.append("contract_liabilities_turnover_days")

    # ---- 6. 净债务 / 利息覆盖 / 短债压力 ----
    short_term = _line_value(current, "balance_sheet", "short_term_borrowings")
    long_term = _line_value(current, "balance_sheet", "long_term_borrowings")
    cash = _line_value(current, "balance_sheet", "cash_and_equivalents")
    interest_expense = _line_value(current, "income", "financial_expense")

    debt_total: float | None = None
    if short_term is not None and long_term is not None:
        debt_total = _finite_value(short_term + long_term)
    net_debt: float | None = None
    if debt_total is not None and cash is not None:
        net_debt = _finite_value(debt_total - cash)
    metrics["net_debt"] = _build_metric(
        "net_debt", net_debt, METRIC_OK if net_debt is not None else METRIC_MISSING,
        formula="(short_term_borrowings + long_term_borrowings) - cash_and_equivalents",
        input_fields=["short_term_borrowings", "long_term_borrowings", "cash_and_equivalents"],
        period=period, unit="元",
    )
    expected_metrics.append("net_debt")

    ratio, status = _safe_div(operating_profit_curr, interest_expense)
    metrics["interest_coverage"] = _build_metric(
        "interest_coverage", ratio, status,
        formula="operating_profit / financial_expense",
        input_fields=["operating_profit", "financial_expense"], period=period, unit="ratio",
        note="使用 financial_expense 近似利息费用",
    )
    expected_metrics.append("interest_coverage")

    ratio, status = _safe_div(short_term, debt_total)
    metrics["short_term_debt_ratio"] = _build_metric(
        "short_term_debt_ratio", ratio, status,
        formula="short_term_borrowings / (short_term + long_term)",
        input_fields=["short_term_borrowings", "long_term_borrowings"],
        period=period, unit="ratio",
    )
    expected_metrics.append("short_term_debt_ratio")

    # ---- 7. 再投资率 / 可持续增长 ----
    # 再投资率 = 1 - 股息支付率，但无股息数据，改用留存率 = (net_profit - 股息) / net_profit
    # 简化：用 retained = (NI - dividends)/NI；缺股息数据时 missing
    metrics["reinvestment_rate"] = _build_metric(
        "reinvestment_rate", None, METRIC_MISSING,
        formula="(net_profit - dividends) / net_profit",
        input_fields=["net_profit", "dividends"], period=period, unit="ratio",
        note="P1.2 未接入股息数据，留待 P1.6 资本配置",
    )
    expected_metrics.append("reinvestment_rate")

    metrics["sustainable_growth"] = _build_metric(
        "sustainable_growth", None, METRIC_MISSING,
        formula="ROE * reinvestment_rate",
        input_fields=["roe", "reinvestment_rate"], period=period, unit="ratio",
        note="依赖 reinvestment_rate",
    )
    expected_metrics.append("sustainable_growth")

    # ---- 8. 杜邦分解：ROE = 净利率 × 资产周转率 × 权益乘数 ----
    # equity 口径与 direct ROE 保持一致：优先 equity_parent，缺失时回退 total_equity
    net_margin, _ = _safe_div(net_profit_curr, revenue_curr)
    asset_turnover, _ = _safe_div(revenue_curr, total_assets)
    equity_multiplier, _ = _safe_div(total_assets, equity_for_roe)
    dupont_parts: dict[str, QualityMetric] = {
        "net_margin": _build_metric(
            "net_margin", net_margin, METRIC_OK if net_margin is not None else METRIC_MISSING,
            formula="net_profit / revenue",
            input_fields=["net_profit", "revenue"], period=period, unit="ratio",
        ),
        "asset_turnover": _build_metric(
            "asset_turnover", asset_turnover, METRIC_OK if asset_turnover is not None else METRIC_MISSING,
            formula="revenue / total_assets",
            input_fields=["revenue", "total_assets"], period=period, unit="ratio",
        ),
        "equity_multiplier": _build_metric(
            "equity_multiplier", equity_multiplier, METRIC_OK if equity_multiplier is not None else METRIC_MISSING,
            formula="total_assets / equity_parent (fallback total_equity)",
            input_fields=["total_assets", "equity_parent"], period=period, unit="ratio",
        ),
    }
    # 杜邦 ROE 验证：net_margin * asset_turnover * equity_multiplier
    dupont_roe: float | None = None
    if net_margin is not None and asset_turnover is not None and equity_multiplier is not None:
        dupont_roe = _finite_value(net_margin * asset_turnover * equity_multiplier)
    dupont_parts["roe_check"] = _build_metric(
        "roe_check", dupont_roe, METRIC_OK if dupont_roe is not None else METRIC_MISSING,
        formula="net_margin * asset_turnover * equity_multiplier (应等于 ROE)",
        input_fields=["net_profit", "revenue", "total_assets", "total_equity"],
        period=period, unit="ratio",
    )

    # ---- 汇总状态 ----
    missing = [name for name in expected_metrics if name not in metrics]
    if not metrics:
        status = "not_computable"
    elif missing or any(m.status == METRIC_MISSING for m in metrics.values()):
        status = "partial"
    else:
        status = "ok"
    return QualityReport(
        status=status,
        metrics=metrics,
        missing_metrics=missing,
        dupont_decomposition=dupont_parts,
    )
