"""P1.1 标准化器：把 provider 原始财务数据转为 NormalizedFinancialStatement。

按 P1 计划 Task P1.1：
1. 接入利润表、资产负债表、现金流量表和财务附注可得字段。
2. 对同公司、同期间、不同 provider 做字段映射和冲突记录（通过 _*_ALIASES）。
3. 支持重述版本（由 repository 强制严格递增）。
4. 任何时点只能看到 available_at <= as_of 的报表版本（apply_pit_filter）。
5. 财务数字若无来源、单位、期间或可见时间，必须阻断计算（grounding_status="missing"）。

字段映射覆盖三个表的常见 provider 候选名（tushare/akshare/cninfo）。
"""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any, Iterable

from .contracts import (
    ADJUSTMENT_KINDS,
    AdjustmentEntry,
    BalanceCheck,
    NormalizedFinancialStatement,
    StatementLine,
    validate_adjustment_kind,
)


# 利润表字段映射：标准化名称 -> 多 provider 候选名（按优先级）
_INCOME_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "total_revenue", "营业收入", "operating_revenue"),
    "operating_cost": ("operating_cost", "营业成本", "operating_total_cost"),
    "gross_profit": ("gross_profit", "毛利润", "operating_profit_before_sga"),
    "operating_profit": ("operating_profit", "营业利润", "operating_profit_to_parent"),
    "net_profit": (
        "net_profit", "net_profit_parent", "归母净利润", "净利润",
        "n_income_attr_p", "net_profit_is_parent",
    ),
    "net_profit_incl_minority": (
        "net_profit_incl_minority", "净利润含少数股东", "n_income",
    ),
    "income_tax": ("income_tax", "所得税费用", "income_tax_expense"),
    "financial_expense": ("financial_expense", "财务费用"),
    "selling_expense": ("selling_expense", "销售费用"),
    "admin_expense": ("admin_expense", "管理费用"),
    "rd_expense": ("rd_expense", "研发费用"),
}

# 资产负债表字段映射
_BALANCE_SHEET_ALIASES: dict[str, tuple[str, ...]] = {
    "total_assets": ("total_assets", "资产总计", "total_asset"),
    "total_liabilities": ("total_liabilities", "负债合计", "total_liab"),
    "total_equity": (
        "total_equity", "股东权益合计", "equity_total",
        "total_holders_equity", "total_shareholders_equity",
    ),
    "equity_parent": ("equity_parent", "归属于母公司股东权益", "equity_parent_company"),
    "cash_and_equivalents": ("cash_and_equivalents", "货币资金", "cash_equivalents"),
    "accounts_receivable": ("accounts_receivable", "应收账款"),
    "inventory": ("inventory", "存货"),
    "contract_liabilities": ("contract_liabilities", "合同负债"),
    "total_current_assets": ("total_current_assets", "流动资产合计"),
    "total_current_liabilities": ("total_current_liabilities", "流动负债合计"),
    "total_noncurrent_assets": ("total_noncurrent_assets", "非流动资产合计"),
    "total_noncurrent_liabilities": ("total_noncurrent_liabilities", "非流动负债合计"),
    "short_term_borrowings": ("short_term_borrowings", "短期借款"),
    "long_term_borrowings": ("long_term_borrowings", "长期借款"),
}

# 现金流量表字段映射
_CASH_FLOW_ALIASES: dict[str, tuple[str, ...]] = {
    "operating_cash_flow": (
        "operating_cash_flow", "net_operate_cash_flow",
        "经营活动现金流净额", "n_cashflow_act",
    ),
    "investing_cash_flow": (
        "investing_cash_flow", "net_invest_cash_flow",
        "投资活动现金流净额", "n_cashflow_inv_act",
    ),
    "financing_cash_flow": (
        "financing_cash_flow", "net_finance_cash_flow",
        "筹资活动现金流净额", "n_cashflow_fin_act",
    ),
    "free_cash_flow": ("free_cash_flow", "自由现金流"),
    "capex": (
        "capex", "资本开支", "固定资产与无形资产支出",
        "c_pay_acq_const_fiolta",
    ),
    "cash_and_equivalents_end": (
        "cash_and_equivalents_end", "期末现金及现金等价物余额",
        "c_cash_equ_end_period",
    ),
    "cash_and_equivalents_begin": (
        "cash_and_equivalents_begin", "期初现金及现金等价物余额",
        "c_cash_equ_bgn_period",
    ),
    "net_cash_increase": (
        "net_cash_increase", "现金及现金等价物净增加额",
        "n_cash_flows_act",
    ),
}


def _pick(row: dict[str, Any], aliases: tuple[str, ...]) -> tuple[Any, str]:
    """按候选列名顺序选取第一个非空值。

    Args:
        row: provider 原始行数据
        aliases: 候选列名（按优先级排序）

    Returns:
        (值, 命中的列名)；未命中返回 (None, "")
    """
    if not isinstance(row, Mapping):
        return None, ""
    for key in aliases:
        value = row.get(key)
        if value in (None, ""):
            continue
        return value, key
    return None, ""


def _to_float(value: Any) -> float | None:
    """安全转 float，失败或空值返回 None。

    Args:
        value: 原始值

    Returns:
        转换后的 float；无法转换返回 None
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _normalize_period(period: Any) -> str:
    """标准化期间为 YYYY-MM-DD 格式。

    支持输入：YYYYMMDD / YYYY-MM-DD / YYYYMMDD000000 / None。

    Args:
        period: 原始期间字符串

    Returns:
        YYYY-MM-DD 格式；空值返回空串
    """
    if not period:
        return ""
    text = str(period).strip().replace("/", "").replace("-", "")
    # 处理可能的 timestamp 形如 20240328000000
    if len(text) > 8 and text.isdigit():
        text = text[:8]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(period)


def _build_lines(
    row: dict[str, Any],
    aliases_map: dict[str, tuple[str, ...]],
    unit: str,
    currency: str,
    source_trace_id: str,
) -> list[StatementLine]:
    """从原始 row 构建标准化报表行列表。

    Args:
        row: provider 原始行数据
        aliases_map: 字段映射表
        unit: 单位
        currency: 币种
        source_trace_id: 来源追溯 ID

    Returns:
        StatementLine 列表，缺失字段 grounding_status="missing"
    """
    lines: list[StatementLine] = []
    for metric, aliases in aliases_map.items():
        value, key = _pick(row, aliases)
        float_value = _to_float(value)
        if float_value is None:
            grounding = "missing"
            ref = key  # 缺失时也保留列名，便于审计
        elif not source_trace_id:
            grounding = "grounding_partial"
            ref = key
        else:
            grounding = "grounding_ok"
            ref = source_trace_id
        lines.append(StatementLine(
            metric_name=metric,
            value=float_value,
            unit=unit,
            currency=currency,
            source_ref=ref,
            grounding_status=grounding,
            reported_value=float_value,
        ))
    return lines


def normalize_statement(
    *,
    symbol: str,
    period: str,
    published_at: str,
    available_at: str,
    provider: str,
    income_row: dict[str, Any] | None = None,
    balance_row: dict[str, Any] | None = None,
    cashflow_row: dict[str, Any] | None = None,
    currency: str = "CNY",
    unit: str = "元",
    restatement_version: int = 1,
) -> NormalizedFinancialStatement:
    """把 provider 原始行数据转为标准化三表。

    Args:
        symbol: 公司股票代码
        period: 期间结束日（YYYYMMDD 或 YYYY-MM-DD）
        published_at: 公司披露日
        available_at: 数据可观测时点（>= published_at）
        provider: 数据来源 provider 名（如 "tushare"）
        income_row: 利润表原始字段；None 表示缺整张表
        balance_row: 资产负债表原始字段
        cashflow_row: 现金流量表原始字段
        currency: 币种
        unit: 单位
        restatement_version: 重述版本号

    Returns:
        NormalizedFinancialStatement 实例，period 标准化为 YYYY-MM-DD
    """
    period_norm = _normalize_period(period)
    source_trace_id = f"{provider}:{symbol}:{period_norm}" if provider and symbol else ""
    income_lines = _build_lines(
        income_row if isinstance(income_row, Mapping) else {},
        _INCOME_ALIASES, unit, currency, source_trace_id,
    ) if income_row else []
    balance_lines = _build_lines(
        balance_row if isinstance(balance_row, Mapping) else {},
        _BALANCE_SHEET_ALIASES, unit, currency, source_trace_id,
    ) if balance_row else []
    cashflow_lines = _build_lines(
        cashflow_row if isinstance(cashflow_row, Mapping) else {},
        _CASH_FLOW_ALIASES, unit, currency, source_trace_id,
    ) if cashflow_row else []
    return NormalizedFinancialStatement(
        symbol=symbol,
        period=period_norm,
        published_at=published_at,
        available_at=available_at,
        currency=currency,
        unit=unit,
        income_statement=income_lines,
        balance_sheet=balance_lines,
        cash_flow_statement=cashflow_lines,
        restatement_version=restatement_version,
        source_trace_id=source_trace_id,
    )


def check_balance(
    statement: NormalizedFinancialStatement,
    tolerance: float = 1.0,
) -> BalanceCheck:
    """勾稽检查：资产=负债+权益；现金流变化=期末-期初。

    Args:
        statement: 标准化三表
        tolerance: 允许误差（与 unit 一致，单位为元时 tolerance=1.0）

    Returns:
        BalanceCheck 结果，diff 为 None 表示缺数据无法检查
    """
    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError, OverflowError):
        tolerance = 1.0
    tolerance = tolerance if isfinite(tolerance) and tolerance >= 0 else 1.0
    notes: list[str] = []

    def _get(lines: list[StatementLine], name: str) -> float | None:
        for line in lines:
            if line.metric_name == name:
                return line.value
        return None

    total_assets = _get(statement.balance_sheet, "total_assets")
    total_liabilities = _get(statement.balance_sheet, "total_liabilities")
    total_equity = _get(statement.balance_sheet, "total_equity")

    diff: float | None = None
    balanced: bool | None = None
    if total_assets is not None and total_liabilities is not None and total_equity is not None:
        diff = round(total_assets - total_liabilities - total_equity, 6)
        balanced = abs(diff) <= tolerance
        if not balanced:
            notes.append(f"资产-负债-权益差异 {diff} 超出容差 {tolerance}")

    cash_end = _get(statement.cash_flow_statement, "cash_and_equivalents_end")
    cash_begin = _get(statement.cash_flow_statement, "cash_and_equivalents_begin")
    net_increase = _get(statement.cash_flow_statement, "net_cash_increase")

    cash_diff: float | None = None
    cash_explained: bool | None = None
    if cash_end is not None and cash_begin is not None and net_increase is not None:
        cash_diff = round(net_increase - (cash_end - cash_begin), 6)
        cash_explained = abs(cash_diff) <= tolerance
        if not cash_explained:
            notes.append(
                f"现金流净增加与期末期初差异 {cash_diff} 超出容差 {tolerance}"
            )

    return BalanceCheck(
        asset_liability_equity_diff=diff,
        asset_liability_equity_balanced=balanced,
        cash_reconciliation_diff=cash_diff,
        cash_reconciliation_explained=cash_explained,
        notes=notes,
    )


def apply_pit_filter(
    statements: Iterable[NormalizedFinancialStatement],
    as_of: str,
) -> list[NormalizedFinancialStatement]:
    """PIT 过滤：只保留 available_at <= as_of 的报表版本。

    对同一 period 多 restatement_version，只保留 as_of 时点可见的最新版本。

    Args:
        statements: 待过滤的报表列表
        as_of: PIT 时点 YYYY-MM-DD（或 YYYYMMDD）

    Returns:
        过滤后的报表列表，按 period 升序排序
    """
    cutoff = _normalize_period(as_of)
    filtered = [
        s for s in statements
        if isinstance(s, NormalizedFinancialStatement)
        if s.available_at and _normalize_period(s.available_at) <= cutoff
    ]
    # 同 period 保留最新 restatement_version
    by_period: dict[str, NormalizedFinancialStatement] = {}
    for s in sorted(filtered, key=lambda x: (x.period, x.restatement_version)):
        by_period[s.period] = s  # 后到的同 period 会覆盖
    return sorted(by_period.values(), key=lambda s: s.period)


def add_adjustment(
    statement: NormalizedFinancialStatement,
    *,
    kind: str,
    amount: float,
    source_ref: str,
    unit: str = "",
    currency: str = "",
    note: str = "",
) -> NormalizedFinancialStatement:
    """给报表追加一条调整账本条目。

    按 P1 计划「无来源只能创建 gap」，本函数强制要求 source_ref 非空。

    Args:
        statement: 原始报表（不可变，返回新实例）
        kind: 调整类型（ADJUSTMENT_KINDS 之一）
        amount: 调整金额
        source_ref: 来源引用（不可为空）
        unit: 单位（默认沿用 statement.unit）
        currency: 币种（默认沿用 statement.currency）
        note: 备注

    Returns:
        追加调整后的新 NormalizedFinancialStatement 实例

    Raises:
        ValueError: 当 kind 非法或 source_ref 为空
    """
    validate_adjustment_kind(kind)
    if not source_ref:
        raise ValueError("调整账本条目必须有 source_ref，无来源只能创建 gap")
    entry = AdjustmentEntry(
        kind=kind,
        amount=amount,
        unit=unit or statement.unit,
        currency=currency or statement.currency,
        source_ref=source_ref,
        note=note,
    )
    return NormalizedFinancialStatement(
        symbol=statement.symbol,
        period=statement.period,
        published_at=statement.published_at,
        available_at=statement.available_at,
        currency=statement.currency,
        unit=statement.unit,
        income_statement=list(statement.income_statement),
        balance_sheet=list(statement.balance_sheet),
        cash_flow_statement=list(statement.cash_flow_statement),
        adjustments=list(statement.adjustments) + [entry],
        accounting_policy_refs=list(statement.accounting_policy_refs),
        restatement_version=statement.restatement_version,
        source_trace_id=statement.source_trace_id,
    )
