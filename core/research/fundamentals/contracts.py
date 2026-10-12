"""P1.1 标准化三表契约。

按 P1 计划 §4.1 NormalizedFinancialStatement 实现：
- income_statement / balance_sheet / cash_flow_statement
- reported_values / normalized_values / adjustments
- accounting_policy_refs / restatement_version
- period / published_at / available_at / source_trace

硬门禁：
- 财务数字若无来源、单位、期间或可见时间，必须阻断计算（grounding_status="missing"）
- 旧版本不可覆盖（restatement_version 严格递增，由 repository 强制）
- 任何时点只能看到 available_at <= as_of 的报表版本（PIT 过滤由 repository 强制）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str) and item]


# 三表类型
STATEMENT_TYPES: tuple[str, ...] = ("income", "balance_sheet", "cash_flow")

# 调整账本类型（按 P1 §5 Task P1.1 第 4 点）
ADJUSTMENT_KINDS: tuple[str, ...] = (
    "one_off_gain_loss",        # 一次性损益
    "asset_disposal",            # 资产处置
    "impairment",                # 减值
    "government_grant",          # 政府补助
    "investment_income",         # 投资收益
    "minority_interest",         # 少数股东损益
    "share_based_compensation",  # 股权激励
    "rd_capitalization",         # 研发资本化
)

# 字段可追溯状态（与 P0 contracts.py 对齐）
GROUNDING_STATUSES: tuple[str, ...] = (
    "grounding_ok",        # 完全可追溯
    "grounding_partial",   # 部分可追溯（缺 source_trace_id 等）
    "missing",             # 字段缺失或值不可转换
)


@dataclass
class StatementLine:
    """标准化报表单行数据。

    每个数字必须带 metric_name/value/unit/currency/source_ref，
    缺失任一项则 grounding_status="missing"，调用方必须阻断计算。

    Attributes:
        metric_name: 标准化指标名（如 "revenue"、"total_assets"）
        value: 标准化后的数值；缺失为 None
        unit: 单位（如 "元"、"万元"、"亿元"）
        currency: 币种（如 "CNY"、"USD"）
        source_ref: 来源引用（source_trace_id 或 evidence_id）
        grounding_status: 可追溯状态
        reported_value: 原始披露值（未标准化），便于审计对比
    """

    metric_name: str = ""
    value: float | None = None
    unit: str = ""
    currency: str = ""
    source_ref: str = ""
    grounding_status: str = "missing"
    reported_value: float | None = None

    def __post_init__(self) -> None:
        self.value = _safe_float(self.value)
        self.reported_value = _safe_float(self.reported_value)
        if self.grounding_status not in GROUNDING_STATUSES:
            self.grounding_status = "missing"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "metric_name": self.metric_name,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "source_ref": self.source_ref,
            "grounding_status": self.grounding_status,
            "reported_value": self.reported_value,
        }


@dataclass
class AdjustmentEntry:
    """调整账本条目。

    每项调整必须有来源（source_ref）；无来源只能创建 gap。

    Attributes:
        kind: 调整类型（ADJUSTMENT_KINDS 之一）
        amount: 调整金额
        unit: 单位
        currency: 币种
        source_ref: 来源引用
        note: 备注（说明调整理由）
    """

    kind: str = ""
    amount: float | None = None
    unit: str = ""
    currency: str = ""
    source_ref: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self.amount = _safe_float(self.amount)
        if self.kind not in ADJUSTMENT_KINDS:
            self.kind = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "kind": self.kind,
            "amount": self.amount,
            "unit": self.unit,
            "currency": self.currency,
            "source_ref": self.source_ref,
            "note": self.note,
        }


@dataclass
class NormalizedFinancialStatement:
    """标准化财务三表（P1 §4.1）。

    Attributes:
        symbol: 公司股票代码
        period: 期间结束日 YYYY-MM-DD
        published_at: 公司披露日
        available_at: 数据可观测时点（>= published_at）
        currency: 币种
        unit: 单位
        income_statement: 利润表行列表
        balance_sheet: 资产负债表行列表
        cash_flow_statement: 现金流量表行列表
        adjustments: 调整账本条目
        accounting_policy_refs: 会计政策引用列表
        restatement_version: 重述版本号（1=原始披露，>1=重述）
        source_trace_id: 数据来源追溯 ID
        schema_version: 契约版本号
    """

    symbol: str = ""
    period: str = ""
    published_at: str = ""
    available_at: str = ""
    currency: str = "CNY"
    unit: str = "元"
    income_statement: list[StatementLine] = field(default_factory=list)
    balance_sheet: list[StatementLine] = field(default_factory=list)
    cash_flow_statement: list[StatementLine] = field(default_factory=list)
    adjustments: list[AdjustmentEntry] = field(default_factory=list)
    accounting_policy_refs: list[str] = field(default_factory=list)
    restatement_version: int = 1
    source_trace_id: str = ""
    schema_version: str = "normalized_statement_v1"

    def __post_init__(self) -> None:
        self.income_statement = [item for item in self.income_statement if isinstance(item, StatementLine)]
        self.balance_sheet = [item for item in self.balance_sheet if isinstance(item, StatementLine)]
        self.cash_flow_statement = [item for item in self.cash_flow_statement if isinstance(item, StatementLine)]
        self.adjustments = [item for item in self.adjustments if isinstance(item, AdjustmentEntry)]
        self.accounting_policy_refs = _string_list(self.accounting_policy_refs)
        try:
            self.restatement_version = max(1, int(self.restatement_version))
        except (TypeError, ValueError):
            self.restatement_version = 1

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "period": self.period,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "currency": self.currency,
            "unit": self.unit,
            "income_statement": [item.to_dict() for item in self.income_statement],
            "balance_sheet": [item.to_dict() for item in self.balance_sheet],
            "cash_flow_statement": [item.to_dict() for item in self.cash_flow_statement],
            "adjustments": [item.to_dict() for item in self.adjustments],
            "accounting_policy_refs": list(self.accounting_policy_refs),
            "restatement_version": self.restatement_version,
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
        }

    def get_line(self, statement_type: str, metric_name: str) -> StatementLine | None:
        """按三表类型与指标名查询行。

        Args:
            statement_type: STATEMENT_TYPES 之一
            metric_name: 指标名

        Returns:
            匹配的 StatementLine；未找到返回 None
        """
        if statement_type == "income":
            lines = self.income_statement
        elif statement_type == "balance_sheet":
            lines = self.balance_sheet
        elif statement_type == "cash_flow":
            lines = self.cash_flow_statement
        else:
            return None
        for line in lines:
            if line.metric_name == metric_name:
                return line
        return None


@dataclass
class BalanceCheck:
    """三表勾稽检查结果。

    diff 字段为 None 表示缺数据无法检查；balanced 为 None 时同理。
    字段非 None 但 balanced=False 表示勾稽不平。

    Attributes:
        asset_liability_equity_diff: 资产-负债-权益差额
        asset_liability_equity_balanced: 是否勾稽平衡
        cash_reconciliation_diff: 现金流净增加-(期末-期初)差额
        cash_reconciliation_explained: 现金流勾稽是否通过
        notes: 不平或异常的备注
    """

    asset_liability_equity_diff: float | None = None
    asset_liability_equity_balanced: bool | None = None
    cash_reconciliation_diff: float | None = None
    cash_reconciliation_explained: bool | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.asset_liability_equity_diff = _safe_float(self.asset_liability_equity_diff)
        self.cash_reconciliation_diff = _safe_float(self.cash_reconciliation_diff)
        self.notes = _string_list(self.notes)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "asset_liability_equity_diff": self.asset_liability_equity_diff,
            "asset_liability_equity_balanced": self.asset_liability_equity_balanced,
            "cash_reconciliation_diff": self.cash_reconciliation_diff,
            "cash_reconciliation_explained": self.cash_reconciliation_explained,
            "notes": list(self.notes),
        }


def validate_adjustment_kind(kind: str) -> None:
    """校验调整类型合法性。

    Args:
        kind: 调整类型字符串

    Raises:
        ValueError: 当 kind 不在 ADJUSTMENT_KINDS 中
    """
    if kind not in ADJUSTMENT_KINDS:
        raise ValueError(
            f"非法调整类型: {kind}; 允许值: {ADJUSTMENT_KINDS}"
        )
