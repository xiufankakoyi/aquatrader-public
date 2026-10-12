"""P1.1 标准化三表子包。

按 P1 计划 §4.1 实现 NormalizedFinancialStatement 契约：
- income_statement / balance_sheet / cash_flow_statement
- reported_values / normalized_values / adjustments
- accounting_policy_refs / restatement_version
- period / published_at / available_at / source_trace
"""
from .contracts import (
    ADJUSTMENT_KINDS,
    AdjustmentEntry,
    BalanceCheck,
    NormalizedFinancialStatement,
    StatementLine,
    STATEMENT_TYPES,
)

__all__ = [
    "ADJUSTMENT_KINDS",
    "AdjustmentEntry",
    "BalanceCheck",
    "NormalizedFinancialStatement",
    "StatementLine",
    "STATEMENT_TYPES",
]
