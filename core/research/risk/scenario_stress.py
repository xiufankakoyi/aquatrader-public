"""P2.8 情景压力测试引擎。

按 P2 计划 §281-299 Task P2.8：
- 模拟某情景下公司收入、利润、现金流和估值变化
- 3 个标准情景（bear / base / bull）+ 自定义情景
- 边界：只输出压力结果，不输出交易动作

设计要点：
- 输入：基线财务数据 + 情景假设（shock 参数）
- 输出：4 个 impact_pct（revenue / profit / cashflow / valuation）
- 计算方式：线性敏感性（base × (1 + shock) × elasticity）
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .contracts import (
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    SCENARIO_BASE_CASE,
    SCENARIO_BEAR_CASE,
    SCENARIO_BULL_CASE,
    SCENARIO_CUSTOM,
    SCENARIO_TYPES,
    ScenarioStressTest,
    utc_now,
)


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

ERR_INVALID_SCENARIO_TYPE: str = "invalid_scenario_type"
ERR_TRADE_ACTION_KEYWORD: str = "trade_action_keyword"
ERR_MISSING_BASELINE: str = "missing_baseline"
ERR_INVALID_SHOCK: str = "invalid_shock"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


# ---------------------------------------------------------------------------
# 标准情景模板
# ---------------------------------------------------------------------------


# 3 个标准情景的默认 shock 参数
# shock 参数说明：
#   revenue_shock_pct: 收入冲击百分比（如 -10 表示收入下降 10%）
#   margin_shock_pct: 利润率冲击百分比（如 -200bp 表示利润率下降 2 个百分点）
#   fcf_conversion_drop_pct: 现金流转化率下降百分比
#   multiple_compress_pct: 估值倍数压缩百分比

STANDARD_SCENARIO_TEMPLATES: dict[str, dict[str, Any]] = {
    SCENARIO_BEAR_CASE: {
        "scenario_name": "标准悲观情景",
        "scenario_description": (
            "假设行业需求下降 10%、产品价格下降 5%、原料成本上升 5%、"
            "估值倍数压缩 20%"
        ),
        "assumptions": {
            "revenue_shock_pct": -10.0,
            "margin_shock_pct": -2.0,  # 利润率下降 2 个百分点
            "fcf_conversion_drop_pct": -10.0,
            "multiple_compress_pct": -20.0,
        },
    },
    SCENARIO_BASE_CASE: {
        "scenario_name": "标准基准情景",
        "scenario_description": "假设行业需求、价格、成本和估值倍数保持当前水平",
        "assumptions": {
            "revenue_shock_pct": 0.0,
            "margin_shock_pct": 0.0,
            "fcf_conversion_drop_pct": 0.0,
            "multiple_compress_pct": 0.0,
        },
    },
    SCENARIO_BULL_CASE: {
        "scenario_name": "标准乐观情景",
        "scenario_description": (
            "假设行业需求上升 10%、产品价格上升 5%、原料成本下降 5%、"
            "估值倍数扩张 15%"
        ),
        "assumptions": {
            "revenue_shock_pct": 10.0,
            "margin_shock_pct": 2.0,
            "fcf_conversion_drop_pct": 5.0,
            "multiple_compress_pct": 15.0,
        },
    },
}


# ---------------------------------------------------------------------------
# 情景压力引擎
# ---------------------------------------------------------------------------


class ScenarioStressEngine:
    """情景压力测试引擎。

    计算某情景下公司收入、利润、现金流和估值的变化。
    """

    def compute(
        self,
        *,
        company_symbol: str,
        as_of: str,
        scenario_type: str = SCENARIO_BASE_CASE,
        scenario_name: str = "",
        scenario_description: str = "",
        assumptions: dict[str, Any] | None = None,
        baseline: dict[str, float] | None = None,
        elasticities: dict[str, float] | None = None,
        evidence_refs: list[str] | None = None,
        source_refs: list[str] | None = None,
        model_version: str = "v1",
    ) -> ScenarioStressTest:
        """计算情景压力测试。

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            scenario_type: 情景类型
            scenario_name: 情景名称（覆盖模板默认值）
            scenario_description: 情景描述（覆盖模板默认值）
            assumptions: 假设参数（覆盖模板默认值）
            baseline: 基线财务数据（revenue / profit / fcf / multiple）
            elasticities: 敏感性弹性系数
            evidence_refs: 证据 ID 列表
            source_refs: 来源引用
            model_version: 模型版本

        Returns:
            ScenarioStressTest

        Raises:
            ValueError: 非法情景类型 / 缺基线 / 含交易动作词 / 非法 shock
        """
        if not company_symbol or not as_of:
            raise ValueError("company_symbol / as_of 必填")
        if scenario_type not in SCENARIO_TYPES:
            raise ValueError(
                f"非法情景类型：{scenario_type}（合法：{', '.join(SCENARIO_TYPES)}）"
            )

        # 应用模板默认值（仅对标准情景）
        template = STANDARD_SCENARIO_TEMPLATES.get(scenario_type, {})
        final_name = scenario_name or template.get("scenario_name", "")
        final_description = scenario_description or template.get("scenario_description", "")
        final_assumptions = {
            **template.get("assumptions", {}),
            **(dict(assumptions) if isinstance(assumptions, Mapping) else {}),
        }

        # 校验交易动作词
        for field_name in ("final_name", "final_description"):
            value = locals()[field_name]
            if value and any(kw in value for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS):
                raise ValueError(
                    f"情景名称/描述含交易动作关键词（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
                )

        # 校验 baseline（基准情景可不提供）
        if scenario_type != SCENARIO_BASE_CASE:
            if not baseline or "revenue" not in baseline:
                raise ValueError(
                    "非基准情景必须提供 baseline.revenue"
                )

        # 校验 shock 参数（数值型）
        for key, value in final_assumptions.items():
            if key.endswith("_pct") or key.endswith("_bp"):
                try:
                    float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"非法 shock 参数：{key}={value}")

        # 默认弹性系数（敏感性 = 1.0 表示线性传导）
        default_elasticities = {
            "revenue_shock_pct": 1.0,
            "margin_shock_pct": 1.0,
            "fcf_conversion_drop_pct": 1.0,
            "multiple_compress_pct": 1.0,
        }
        final_elasticities = {
            **default_elasticities,
            **(dict(elasticities) if isinstance(elasticities, Mapping) else {}),
        }

        # 计算 4 个 impact_pct
        revenue_impact = self._compute_revenue_impact(
            final_assumptions, final_elasticities
        )
        profit_impact = self._compute_profit_impact(
            final_assumptions, final_elasticities, baseline
        )
        cashflow_impact = self._compute_cashflow_impact(
            final_assumptions, final_elasticities, baseline
        )
        valuation_impact = self._compute_valuation_impact(
            final_assumptions, final_elasticities
        )

        return ScenarioStressTest(
            company_symbol=company_symbol,
            as_of=as_of,
            scenario_type=scenario_type,
            scenario_name=final_name,
            scenario_description=final_description,
            assumptions=final_assumptions,
            revenue_impact_pct=revenue_impact,
            profit_impact_pct=profit_impact,
            cashflow_impact_pct=cashflow_impact,
            valuation_impact_pct=valuation_impact,
            evidence_refs=_string_list(evidence_refs),
            source_refs=_string_list(source_refs),
            model_version=model_version,
        )

    # ------------------------------------------------------------------
    # 内部计算
    # ------------------------------------------------------------------

    def _compute_revenue_impact(
        self,
        assumptions: dict[str, Any],
        elasticities: dict[str, float],
    ) -> float:
        """计算收入影响百分比。

        收入 = revenue_shock_pct × elasticity
        """
        shock = _safe_float(assumptions.get("revenue_shock_pct", 0.0))
        elasticity = _safe_float(elasticities.get("revenue_shock_pct", 1.0), 1.0)
        return shock * elasticity

    def _compute_profit_impact(
        self,
        assumptions: dict[str, Any],
        elasticities: dict[str, float],
        baseline: dict[str, float] | None,
    ) -> float:
        """计算利润影响百分比。

        利润影响 = 收入影响 × 经营杠杆 + 利润率冲击影响

        单位约定：
        - revenue_shock_pct / margin_shock_pct：百分点数值（如 -3.0 表示 -3pp）
        - baseline_margin：小数（如 0.10 表示 10%）
        - 返回值：百分比数值（如 -30.0 表示 -30%）

        利润率冲击公式推导（margin 从 baseline 下降 margin_shock 个百分点）：
            new_margin = baseline_margin + margin_shock_pct / 100
            利润变化 = (new_margin - baseline_margin) / baseline_margin
                     = (margin_shock_pct / 100) / baseline_margin
            以百分比表达 = margin_shock_pct / baseline_margin
        例：margin -3pp / baseline 0.10 → -30% 利润下降
        """
        revenue_shock = _safe_float(assumptions.get("revenue_shock_pct", 0.0))
        margin_shock = _safe_float(assumptions.get("margin_shock_pct", 0.0))  # 百分点

        # 经营杠杆：默认 2.0（收入下降 10% → 利润下降 20%）
        operating_leverage = _safe_float(elasticities.get("operating_leverage", 2.0), 2.0)

        # 收入影响传导到利润
        revenue_impact_on_profit = revenue_shock * operating_leverage

        # 利润率冲击转化为利润影响百分比
        # 假设基线利润率为 10%，利润率下降 2 个百分点 → 利润下降 20%
        baseline_margin = 0.10  # 默认 10%
        if baseline and "margin" in baseline and baseline["margin"] > 0:
            baseline_margin = _safe_float(baseline["margin"])

        # 单位推导见 docstring：margin_shock_pct / baseline_margin 已是百分比
        margin_impact_pct = (
            (margin_shock / baseline_margin) if baseline_margin > 0 else 0.0
        )

        return revenue_impact_on_profit + margin_impact_pct

    def _compute_cashflow_impact(
        self,
        assumptions: dict[str, Any],
        elasticities: dict[str, float],
        baseline: dict[str, float] | None,
    ) -> float:
        """计算现金流影响百分比。

        现金流影响 = 利润影响 + 现金流转化率下降
        """
        profit_impact = self._compute_profit_impact(assumptions, elasticities, baseline)
        fcf_drop = _safe_float(assumptions.get("fcf_conversion_drop_pct", 0.0))
        return profit_impact + fcf_drop

    def _compute_valuation_impact(
        self,
        assumptions: dict[str, Any],
        elasticities: dict[str, float],
    ) -> float:
        """计算估值影响百分比。

        估值影响 = 估值倍数压缩百分比
        """
        multiple_compress = _safe_float(assumptions.get("multiple_compress_pct", 0.0))
        return multiple_compress


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_scenario_stress_engine() -> ScenarioStressEngine:
    """构造默认 ScenarioStressEngine。"""
    return ScenarioStressEngine()


__all__ = [
    "ERR_INVALID_SCENARIO_TYPE",
    "ERR_INVALID_SHOCK",
    "ERR_MISSING_BASELINE",
    "ERR_TRADE_ACTION_KEYWORD",
    "STANDARD_SCENARIO_TEMPLATES",
    "ScenarioStressEngine",
    "build_scenario_stress_engine",
]
