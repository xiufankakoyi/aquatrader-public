"""P2.8 研究风险引擎。

按 P2 计划 §281-299 Task P2.8：
- 6 个风险维度评估
- 综合风险等级聚合
- 集中度 HHI 计算
- 因子暴露聚合
- 相关性备注

边界：只输出风险暴露，不输出仓位或交易动作。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .contracts import (
    DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE,
    DIMENSION_FACTOR_CORRELATION,
    DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH,
    DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY,
    DIMENSION_MACRO_FX_RATE_COMMODITY,
    DIMENSION_SCENARIO_STRESS,
    DRIVER_DIRECTION_DOWNSIDE,
    DRIVER_DIRECTION_NEUTRAL,
    DRIVER_DIRECTION_UPSIDE,
    DRIVER_DIRECTIONS,
    DRIVER_MAGNITUDES,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    NOTICE,
    PortfolioSnapshot,
    RISK_DIMENSIONS,
    RISK_LEVEL_EXTREME,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_LOW,
    RISK_LEVEL_MEDIUM,
    RISK_LEVEL_SCORES,
    RISK_LEVELS,
    ResearchRiskContext,
    RiskDimension,
    RiskDriver,
    utc_now,
)


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

ERR_MISSING_DIMENSION: str = "missing_dimension"
ERR_INVALID_LEVEL: str = "invalid_level"
ERR_INVALID_DIRECTION: str = "invalid_direction"
ERR_INVALID_MAGNITUDE: str = "invalid_magnitude"
ERR_TRADE_ACTION_KEYWORD: str = "trade_action_keyword"
ERR_INVALID_ASSUMPTIONS: str = "invalid_assumptions"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _string_list(value: Any) -> list[str]:
    return [item.strip() for item in _as_list(value) if isinstance(item, str) and item.strip()]


def _float_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _safe_float(item) for key, item in value.items() if str(key).strip()}


# ---------------------------------------------------------------------------
# 综合等级聚合
# ---------------------------------------------------------------------------


def aggregate_overall_risk_level(dimensions: list[RiskDimension]) -> tuple[str, float]:
    """根据 6 个维度的 level / score 聚合综合风险等级。

    聚合规则：
    - 取所有维度的最大 score 作为 overall_risk_score
    - 若任一维度为 extreme → overall = extreme
    - 否则按 max score 映射到 level

    Args:
        dimensions: 风险维度列表

    Returns:
        (overall_level, overall_score)
    """
    dimensions = [item for item in _as_list(dimensions) if isinstance(item, RiskDimension)]
    if not dimensions:
        return RISK_LEVEL_LOW, 0.0

    # 任一维度为 extreme → overall = extreme
    for d in dimensions:
        if d.level == RISK_LEVEL_EXTREME:
            return RISK_LEVEL_EXTREME, max(dim.score for dim in dimensions)

    # 否则按 max score 映射
    max_score = max(d.score for d in dimensions)
    if max_score >= RISK_LEVEL_SCORES[RISK_LEVEL_HIGH]:
        return RISK_LEVEL_HIGH, max_score
    if max_score >= RISK_LEVEL_SCORES[RISK_LEVEL_MEDIUM]:
        return RISK_LEVEL_MEDIUM, max_score
    return RISK_LEVEL_LOW, max_score


# ---------------------------------------------------------------------------
# 集中度 HHI
# ---------------------------------------------------------------------------


def compute_concentration_hhi(weights: list[float]) -> float:
    """计算集中度 HHI（Herfindahl-Hirschman Index）。

    HHI = Σ(weight_i)²，取值范围 [0, 1]：
    - 1 = 单一持仓（最高集中度）
    - 1/N = N 个等权持仓（最低集中度）
    - > 0.25 通常视为高集中度

    Args:
        weights: 权重列表（0-1，应为 1.0 总和）

    Returns:
        HHI 指数
    """
    parsed_weights = [_safe_float(weight) for weight in _as_list(weights)]
    if not parsed_weights:
        return 0.0
    return min(1.0, sum(w * w for w in parsed_weights if w > 0))


def compute_industry_exposure(holdings_weights: dict[str, float], holdings_industries: dict[str, str]) -> dict[str, float]:
    """计算行业暴露字典。

    Args:
        holdings_weights: {stock_code: weight}
        holdings_industries: {stock_code: industry}

    Returns:
        {industry: aggregated_weight}
    """
    exposure: dict[str, float] = {}
    if not isinstance(holdings_weights, Mapping):
        return exposure
    industries = holdings_industries if isinstance(holdings_industries, Mapping) else {}
    for code, weight in holdings_weights.items():
        industry = str(industries.get(code, "unknown") or "unknown")
        exposure[industry] = exposure.get(industry, 0.0) + _safe_float(weight)
    return exposure


# ---------------------------------------------------------------------------
# 研究风险引擎
# ---------------------------------------------------------------------------


class ResearchRiskEngine:
    """研究风险引擎。

    构建 ResearchRiskContext，聚合 6 个风险维度。
    """

    def build_context(
        self,
        *,
        company_symbol: str,
        as_of: str,
        dimensions: list[RiskDimension],
        thesis_id: str = "",
        factor_exposures: dict[str, float] | None = None,
        correlation_notes: list[str] | None = None,
        portfolio_snapshot: PortfolioSnapshot | None = None,
        data_version: str = "",
        model_version: str = "v1",
    ) -> ResearchRiskContext:
        """构建研究风险上下文。

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            dimensions: 6 个风险维度
            thesis_id: 关联观点 ID（可选）
            factor_exposures: 因子暴露字典
            correlation_notes: 相关性备注
            portfolio_snapshot: 组合快照（用于 HHI 计算）
            data_version: 数据版本
            model_version: 模型版本

        Returns:
            ResearchRiskContext

        Raises:
            ValueError: 维度缺失 / 非法 level / 含交易动作词
        """
        if not company_symbol or not as_of:
            raise ValueError("company_symbol / as_of 必填")

        # 校验 6 个维度齐全
        dimensions = [item for item in _as_list(dimensions) if isinstance(item, RiskDimension)]
        provided_dims = {d.dimension for d in dimensions}
        missing = set(RISK_DIMENSIONS) - provided_dims
        if missing:
            raise ValueError(f"缺少风险维度：{', '.join(sorted(missing))}")

        # 校验每个维度
        for d in dimensions:
            if d.level not in RISK_LEVELS:
                raise ValueError(f"非法风险等级：{d.level}（维度 {d.dimension}）")
            if d.contains_trade_action():
                raise ValueError(
                    f"维度 {d.dimension} 含交易动作关键词（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
                )
            for driver in d.drivers:
                if driver.direction not in DRIVER_DIRECTIONS:
                    raise ValueError(
                        f"非法驱动方向：{driver.direction}（维度 {d.dimension} / 驱动 {driver.driver_id}）"
                    )
                if driver.magnitude not in DRIVER_MAGNITUDES:
                    raise ValueError(
                        f"非法驱动幅度：{driver.magnitude}（维度 {d.dimension} / 驱动 {driver.driver_id}）"
                    )

        # 聚合综合风险等级
        overall_level, overall_score = aggregate_overall_risk_level(dimensions)

        # 计算集中度 HHI（来自组合快照）
        concentration_hhi = 0.0
        if portfolio_snapshot:
            concentration_hhi = portfolio_snapshot.concentration_hhi
            if concentration_hhi == 0.0 and portfolio_snapshot.holdings:
                # 兜底：从 holdings 重新计算
                concentration_hhi = compute_concentration_hhi(
                    [h.weight for h in portfolio_snapshot.holdings]
                )

        return ResearchRiskContext(
            company_symbol=company_symbol,
            as_of=as_of,
            thesis_id=thesis_id,
            dimensions=dimensions,
            overall_risk_level=overall_level,
            overall_risk_score=overall_score,
            concentration_hhi=concentration_hhi,
            factor_exposures=_float_mapping(factor_exposures),
            correlation_notes=_string_list(correlation_notes),
            data_version=data_version,
            model_version=model_version,
        )

    def build_dimension(
        self,
        *,
        dimension: str,
        level: str,
        drivers: list[RiskDriver] | None = None,
        evidence_refs: list[str] | None = None,
        mitigants: list[str] | None = None,
        source_refs: list[str] | None = None,
        as_of: str = "",
        note: str = "",
    ) -> RiskDimension:
        """构建单一风险维度。

        Args:
            dimension: 维度 key
            level: 风险等级
            drivers: 风险驱动列表
            evidence_refs: 证据 ID 列表
            mitigants: 风险缓释因素
            source_refs: 来源引用
            as_of: PIT 时点
            note: 备注

        Returns:
            RiskDimension
        """
        if dimension not in RISK_DIMENSIONS:
            raise ValueError(f"非法风险维度：{dimension}")
        if level not in RISK_LEVELS:
            raise ValueError(f"非法风险等级：{level}")

        return RiskDimension(
            dimension=dimension,
            level=level,
            score=RISK_LEVEL_SCORES[level],
            drivers=[item for item in _as_list(drivers) if isinstance(item, RiskDriver)],
            evidence_refs=_string_list(evidence_refs),
            mitigants=_string_list(mitigants),
            source_refs=_string_list(source_refs),
            as_of=as_of,
            note=note,
        )

    def build_driver(
        self,
        *,
        driver_id: str,
        name: str,
        description: str = "",
        direction: str = DRIVER_DIRECTION_NEUTRAL,
        magnitude: str = "medium",
        evidence_refs: list[str] | None = None,
        source_refs: list[str] | None = None,
    ) -> RiskDriver:
        """构建风险驱动。

        Args:
            driver_id: 驱动 ID
            name: 驱动名称
            description: 描述
            direction: 方向
            magnitude: 幅度
            evidence_refs: 证据
            source_refs: 来源

        Returns:
            RiskDriver
        """
        if direction not in DRIVER_DIRECTIONS:
            raise ValueError(f"非法驱动方向：{direction}")
        if magnitude not in DRIVER_MAGNITUDES:
            raise ValueError(f"非法驱动幅度：{magnitude}")

        driver = RiskDriver(
            driver_id=driver_id,
            name=name,
            description=description,
            direction=direction,
            magnitude=magnitude,
            evidence_refs=_string_list(evidence_refs),
            source_refs=_string_list(source_refs),
        )
        if driver.contains_trade_action():
            raise ValueError(
                f"驱动 {driver_id} 含交易动作关键词（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
            )
        return driver


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_research_risk_engine() -> ResearchRiskEngine:
    """构造默认 ResearchRiskEngine。"""
    return ResearchRiskEngine()


__all__ = [
    "ERR_INVALID_ASSUMPTIONS",
    "ERR_INVALID_DIRECTION",
    "ERR_INVALID_LEVEL",
    "ERR_INVALID_MAGNITUDE",
    "ERR_MISSING_DIMENSION",
    "ERR_TRADE_ACTION_KEYWORD",
    "ResearchRiskEngine",
    "aggregate_overall_risk_level",
    "build_research_risk_engine",
    "compute_concentration_hhi",
    "compute_industry_exposure",
]
