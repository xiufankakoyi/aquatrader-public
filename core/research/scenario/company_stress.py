"""P3.4 公司压力测试引擎。

按 P3 计划 Task P3.4：
- 输出财务、现金流、估值和风险变化，不输出交易动作
- 通过 MacroTransmissionEngine 计算 driver impacts
- 将 driver impacts 翻译为 forecast_before / forecast_after / risk_delta
- 相关性观察路径默认不参与因果传导（``allow_correlation_only=False``）

驱动 → 财务翻译约定：
- 所有 driver 的 ``impact_pct`` 统一为「相对百分比变化」
  （如 -10.0 表示该驱动相对基线下降 10%）
- revenue / valuation_multiple / capex: 直接乘以 (1 + impact_pct/100)
- gross_margin / opex_ratio / fcf_conversion / discount_rate:
  先按相对变化更新比率值，再参与净利润 / 现金流推导
- 这样所有冲击的单位语义一致，避免 pp 与 % 混用导致的放大失真

净利润推导（简化、保守）：
    operating_margin = gross_margin - opex_ratio
    net_profit = revenue × operating_margin
    （税率与利息费用在 P3.4 不展开，由 P1 估值层处理）

企业价值（EV）推导（PE-based 简化）：
    enterprise_value = max(net_profit, 0) × valuation_multiple
"""
from __future__ import annotations

import hashlib
import math
from typing import Iterable

from .contracts import (
    CompanyStressScenario,
    DriverImpact,
    ForecastSnapshot,
    MacroShock,
    NOTICE,
    RISK_LEVEL_EXTREME,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_LOW,
    RISK_LEVEL_MEDIUM,
    RISK_LEVEL_SCORES,
    RiskDelta,
    ScenarioContractError,
    TARGET_DRIVER_CAPEX,
    TARGET_DRIVER_DISCOUNT_RATE,
    TARGET_DRIVER_FCF_CONVERSION,
    TARGET_DRIVER_GROSS_MARGIN,
    TARGET_DRIVER_OPEX_RATIO,
    TARGET_DRIVER_REVENUE,
    TARGET_DRIVER_VALUATION_MULTIPLE,
    TransmissionPath,
    assert_no_trade_action,
    utc_now,
)
from .macro_transmission import (
    MacroTransmissionEngine,
    MacroTransmissionRegistry,
    build_default_engine,
)


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

ERR_MISSING_BASELINE: str = "missing_baseline"
ERR_INVALID_RISK_LEVEL: str = "invalid_risk_level"
ERR_TRADE_ACTION_KEYWORD: str = "trade_action_keyword"


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


# ---------------------------------------------------------------------------
# 风险等级辅助
# ---------------------------------------------------------------------------

#: |impact_pct| 触发风险升级的阈值（按 driver 类别）
_RISK_ESCALATION_THRESHOLD_PCT: float = 10.0

#: |impact_pct| 触发极端风险的阈值
_RISK_EXTREME_THRESHOLD_PCT: float = 25.0

#: 每单位 adverse impact_pct 推高风险分数的系数
_RISK_SCORE_PER_PCT: float = 0.012


def _score_to_level(score: float) -> str:
    """将 [0,1] 风险分数映射到风险等级。"""
    s = max(0.0, min(1.0, score))
    if s >= RISK_LEVEL_SCORES[RISK_LEVEL_EXTREME]:
        return RISK_LEVEL_EXTREME
    if s >= RISK_LEVEL_SCORES[RISK_LEVEL_HIGH]:
        return RISK_LEVEL_HIGH
    if s >= RISK_LEVEL_SCORES[RISK_LEVEL_MEDIUM]:
        return RISK_LEVEL_MEDIUM
    return RISK_LEVEL_LOW


# ---------------------------------------------------------------------------
# 公司压力测试引擎
# ---------------------------------------------------------------------------


class CompanyStressEngine:
    """公司压力测试引擎。

    将宏观冲击通过传导路径翻译为公司财务、现金流、估值和风险变化。

    Usage::

        engine = CompanyStressEngine(build_default_engine())
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="利率上行 50bp 情景",
            scenario_description="测试利率上行对消费行业估值的影响",
            macro_shocks=[shock_rate_hike],
            baseline_forecast=ForecastSnapshot(
                revenue=100000.0, gross_margin=0.70, opex_ratio=0.20,
                net_profit=50000.0, fcf=40000.0,
                discount_rate=0.10, valuation_multiple=30.0,
                enterprise_value=1500000.0,
            ),
        )
    """

    def __init__(self, transmission_engine: MacroTransmissionEngine) -> None:
        """初始化公司压力引擎。

        Args:
            transmission_engine: 宏观传导引擎（含注册表与评估期配置）
        """
        self._engine = transmission_engine

    @property
    def transmission(self) -> MacroTransmissionEngine:
        """返回底层传导引擎。"""
        return self._engine

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def stress(
        self,
        *,
        symbol: str,
        industry: str,
        as_of: str,
        scenario_name: str,
        scenario_description: str,
        macro_shocks: Iterable[MacroShock],
        baseline_forecast: ForecastSnapshot,
        baseline_risk_level: str = RISK_LEVEL_LOW,
        baseline_risk_score: float | None = None,
        model_version: str = "v1",
    ) -> CompanyStressScenario:
        """对公司施加宏观冲击情景并计算影响。

        Args:
            symbol: 公司代码
            industry: 公司行业
            as_of: PIT 时点（YYYY-MM-DD）
            scenario_name: 情景名称
            scenario_description: 情景描述
            macro_shocks: 宏观冲击列表
            baseline_forecast: 情景前基线财务快照
            baseline_risk_level: 情景前风险等级
            baseline_risk_score: 情景前风险分数；不提供则由等级映射
            model_version: 模型版本

        Returns:
            CompanyStressScenario: 含 forecast_before / after / risk_delta

        Raises:
            ScenarioContractError: 输入非法 / 含交易动作词
        """
        # ---------------- 输入校验 ----------------
        if not symbol:
            raise ScenarioContractError("symbol 必填")
        if not industry:
            raise ScenarioContractError("industry 必填")
        if not as_of:
            raise ScenarioContractError("as_of 必填")
        assert_no_trade_action(scenario_name)
        assert_no_trade_action(scenario_description)
        if baseline_risk_level not in RISK_LEVEL_SCORES:
            raise ScenarioContractError(
                f"baseline_risk_level 非法：{baseline_risk_level}"
                f"（合法：{', '.join(RISK_LEVEL_SCORES.keys())}）"
            )
        if not isinstance(baseline_forecast, ForecastSnapshot):
            raise ScenarioContractError("baseline_forecast 必须是 ForecastSnapshot")
        if baseline_forecast.revenue <= 0:
            raise ScenarioContractError(
                "baseline_forecast.revenue 必须为正；缺失基线无法计算压力"
            )

        try:
            raw_shocks = tuple(macro_shocks)
        except TypeError:
            raw_shocks = ()
        shocks_tuple = tuple(item for item in raw_shocks if isinstance(item, MacroShock))
        if not shocks_tuple:
            # 空冲击：返回零影响情景
            return self._build_zero_impact_scenario(
                symbol=symbol,
                industry=industry,
                as_of=as_of,
                scenario_name=scenario_name,
                scenario_description=scenario_description,
                baseline_forecast=baseline_forecast,
                baseline_risk_level=baseline_risk_level,
                baseline_risk_score=baseline_risk_score,
                model_version=model_version,
            )

        # ---------------- 计算 driver impacts ----------------
        driver_impacts, correlation_only_paths, skipped_no_path = (
            self._engine.compute_driver_impacts(shocks_tuple, industry)
        )

        # ---------------- 翻译为 forecast_after ----------------
        forecast_after, revenue_impact_pct, profit_impact_pct, \
            cashflow_impact_pct, valuation_impact_pct = (
                self._translate_to_financials(
                    baseline_forecast=baseline_forecast,
                    driver_impacts=driver_impacts,
                )
            )

        # ---------------- 计算 risk_delta ----------------
        risk_delta = self._compute_risk_delta(
            driver_impacts=driver_impacts,
            baseline_risk_level=baseline_risk_level,
            baseline_risk_score=baseline_risk_score,
        )

        # ---------------- 汇总证据 ----------------
        evidence_refs = tuple(
            sorted({e for di in driver_impacts for e in di.evidence_refs})
        )

        # ---------------- 生成 scenario_id ----------------
        scenario_id = self._derive_scenario_id(
            symbol=symbol,
            as_of=as_of,
            scenario_name=scenario_name,
            shocks=shocks_tuple,
            model_version=model_version,
        )

        return CompanyStressScenario(
            scenario_id=scenario_id,
            symbol=symbol,
            industry=industry,
            as_of=as_of,
            scenario_name=scenario_name,
            scenario_description=scenario_description,
            macro_shocks=shocks_tuple,
            driver_impacts=driver_impacts,
            forecast_before=baseline_forecast,
            forecast_after=forecast_after,
            revenue_impact_pct=revenue_impact_pct,
            profit_impact_pct=profit_impact_pct,
            cashflow_impact_pct=cashflow_impact_pct,
            valuation_impact_pct=valuation_impact_pct,
            risk_delta=risk_delta,
            correlation_only_paths=correlation_only_paths,
            skipped_no_path=skipped_no_path,
            evidence_refs=evidence_refs,
            model_version=model_version,
        )

    # ------------------------------------------------------------------
    # 财务翻译
    # ------------------------------------------------------------------

    def _translate_to_financials(
        self,
        *,
        baseline_forecast: ForecastSnapshot,
        driver_impacts: tuple[DriverImpact, ...],
    ) -> tuple[ForecastSnapshot, float, float, float, float]:
        """将 driver impacts 翻译为 forecast_after 与 4 个 impact_pct。

        约定：所有 driver 的 ``impact_pct`` 均为「相对百分比变化」。
        - revenue / valuation_multiple / capex: 直接乘以 (1 + impact_pct/100)
        - gross_margin / opex_ratio / fcf_conversion / discount_rate:
          按相对变化更新比率值，再参与净利润 / 现金流推导

        Returns:
            tuple: (forecast_after, revenue_impact_pct,
                    profit_impact_pct, cashflow_impact_pct, valuation_impact_pct)
        """
        # 聚合每个 driver 的总影响
        impact_by_driver: dict[str, float] = {}
        for di in driver_impacts:
            impact_by_driver[di.driver] = (
                impact_by_driver.get(di.driver, 0.0) + di.impact_pct
            )

        # 各 driver 的相对百分比变化
        revenue_impact_pct = impact_by_driver.get(TARGET_DRIVER_REVENUE, 0.0)
        gross_margin_pct = impact_by_driver.get(TARGET_DRIVER_GROSS_MARGIN, 0.0)
        opex_ratio_pct = impact_by_driver.get(TARGET_DRIVER_OPEX_RATIO, 0.0)
        fcf_conversion_pct = impact_by_driver.get(TARGET_DRIVER_FCF_CONVERSION, 0.0)
        discount_rate_pct = impact_by_driver.get(TARGET_DRIVER_DISCOUNT_RATE, 0.0)
        valuation_multiple_pct = impact_by_driver.get(
            TARGET_DRIVER_VALUATION_MULTIPLE, 0.0
        )
        capex_pct = impact_by_driver.get(TARGET_DRIVER_CAPEX, 0.0)

        # 计算 forecast_after
        new_revenue = baseline_forecast.revenue * (1.0 + revenue_impact_pct / 100.0)
        new_gross_margin = baseline_forecast.gross_margin * (
            1.0 + gross_margin_pct / 100.0
        )
        new_opex_ratio = baseline_forecast.opex_ratio * (
            1.0 + opex_ratio_pct / 100.0
        )
        new_discount_rate = baseline_forecast.discount_rate * (
            1.0 + discount_rate_pct / 100.0
        )
        new_valuation_multiple = baseline_forecast.valuation_multiple * (
            1.0 + valuation_multiple_pct / 100.0
        )

        # 经营利润率 = 毛利率 - 运营费用率
        old_operating_margin = (
            baseline_forecast.gross_margin - baseline_forecast.opex_ratio
        )
        new_operating_margin = new_gross_margin - new_opex_ratio

        # 净利润：revenue × operating_margin（简化；不展开税率和利息）
        old_net_profit = baseline_forecast.net_profit
        if old_net_profit <= 0:
            # 基线净利润非正时，用基线 operating_margin 重建
            old_net_profit = baseline_forecast.revenue * old_operating_margin
        new_net_profit = new_revenue * new_operating_margin
        profit_impact_pct = (
            (new_net_profit - old_net_profit) / max(abs(old_net_profit), 1e-9) * 100.0
        )

        # 自由现金流：net_profit × fcf_conversion_rate
        # 基线 fcf_conversion_rate = fcf / net_profit（保守下界 0.0）
        old_conv_rate = (
            baseline_forecast.fcf / max(abs(old_net_profit), 1e-9)
            if baseline_forecast.fcf != 0
            else 0.0
        )
        new_conv_rate = max(0.0, old_conv_rate * (1.0 + fcf_conversion_pct / 100.0))
        new_fcf = new_net_profit * new_conv_rate
        old_fcf = baseline_forecast.fcf if baseline_forecast.fcf != 0 else (
            old_net_profit * old_conv_rate
        )
        cashflow_impact_pct = (
            (new_fcf - old_fcf) / max(abs(old_fcf), 1e-9) * 100.0
            if abs(old_fcf) > 1e-9
            else 0.0
        )

        # 企业价值：max(net_profit, 0) × valuation_multiple
        old_ev = baseline_forecast.enterprise_value
        if old_ev <= 0:
            old_ev = max(old_net_profit, 0.0) * baseline_forecast.valuation_multiple
        new_ev = max(new_net_profit, 0.0) * new_valuation_multiple
        valuation_impact_pct = (
            (new_ev - old_ev) / max(abs(old_ev), 1e-9) * 100.0
            if abs(old_ev) > 1e-9
            else valuation_multiple_pct
        )

        forecast_after = ForecastSnapshot(
            revenue=new_revenue,
            gross_margin=new_gross_margin,
            opex_ratio=new_opex_ratio,
            net_profit=new_net_profit,
            fcf=new_fcf,
            discount_rate=new_discount_rate,
            valuation_multiple=new_valuation_multiple,
            enterprise_value=new_ev,
        )

        # capex 影响不在 4 个 impact_pct 中体现，但记录在 driver_impacts
        # 由调用方按需查阅。
        _ = capex_pct  # noqa: F841

        return (
            forecast_after,
            revenue_impact_pct,
            profit_impact_pct,
            cashflow_impact_pct,
            valuation_impact_pct,
        )

    # ------------------------------------------------------------------
    # 风险变化
    # ------------------------------------------------------------------

    def _compute_risk_delta(
        self,
        *,
        driver_impacts: tuple[DriverImpact, ...],
        baseline_risk_level: str,
        baseline_risk_score: float | None,
    ) -> RiskDelta:
        """根据 driver impacts 计算风险变化。

        规则：
        - 不利影响（impact_pct < 0）推高风险分数
        - 有利影响（impact_pct > 0）不降低风险分数（保守）
        - |impact_pct| ≥ 阈值的 driver 计入 escalated_drivers
        - 新增风险因素按 driver 描述生成
        """
        score_before = (
            baseline_risk_score
            if baseline_risk_score is not None
            else RISK_LEVEL_SCORES[baseline_risk_level]
        )
        score_before = max(0.0, min(1.0, _safe_float(score_before)))

        adverse_total = 0.0
        escalated: list[str] = []
        new_factors: list[str] = []
        for di in driver_impacts:
            if di.impact_pct < 0:
                adverse_total += abs(di.impact_pct)
            if abs(di.impact_pct) >= _RISK_ESCALATION_THRESHOLD_PCT:
                escalated.append(di.driver)
                # 生成可读风险因素文本
                direction_text = "下降" if di.impact_pct < 0 else "上升"
                new_factors.append(
                    f"{di.driver} {direction_text} {abs(di.impact_pct):.2f}%"
                    f"（来自 {di.source_variable}）"
                )

        score_after = min(1.0, score_before + adverse_total * _RISK_SCORE_PER_PCT)
        if adverse_total >= _RISK_EXTREME_THRESHOLD_PCT:
            # 极端阈值触发额外升级
            score_after = max(score_after, RISK_LEVEL_SCORES[RISK_LEVEL_EXTREME])

        level_before = baseline_risk_level
        level_after = _score_to_level(score_after)

        return RiskDelta(
            risk_level_before=level_before,
            risk_level_after=level_after,
            risk_score_before=score_before,
            risk_score_after=score_after,
            score_delta=score_after - score_before,
            escalated_drivers=tuple(sorted(set(escalated))),
            new_risk_factors=tuple(new_factors),
        )

    # ------------------------------------------------------------------
    # 零影响情景
    # ------------------------------------------------------------------

    def _build_zero_impact_scenario(
        self,
        *,
        symbol: str,
        industry: str,
        as_of: str,
        scenario_name: str,
        scenario_description: str,
        baseline_forecast: ForecastSnapshot,
        baseline_risk_level: str,
        baseline_risk_score: float | None,
        model_version: str,
    ) -> CompanyStressScenario:
        """空冲击情景：driver_impacts 为空，forecast_after = before。"""
        score_before = (
            baseline_risk_score
            if baseline_risk_score is not None
            else RISK_LEVEL_SCORES[baseline_risk_level]
        )
        score_before = max(0.0, min(1.0, _safe_float(score_before)))
        risk_delta = RiskDelta(
            risk_level_before=baseline_risk_level,
            risk_level_after=baseline_risk_level,
            risk_score_before=score_before,
            risk_score_after=score_before,
            score_delta=0.0,
            escalated_drivers=(),
            new_risk_factors=(),
        )
        scenario_id = self._derive_scenario_id(
            symbol=symbol,
            as_of=as_of,
            scenario_name=scenario_name,
            shocks=(),
            model_version=model_version,
        )
        return CompanyStressScenario(
            scenario_id=scenario_id,
            symbol=symbol,
            industry=industry,
            as_of=as_of,
            scenario_name=scenario_name,
            scenario_description=scenario_description,
            macro_shocks=(),
            driver_impacts=(),
            forecast_before=baseline_forecast,
            forecast_after=baseline_forecast,
            revenue_impact_pct=0.0,
            profit_impact_pct=0.0,
            cashflow_impact_pct=0.0,
            valuation_impact_pct=0.0,
            risk_delta=risk_delta,
            correlation_only_paths=(),
            skipped_no_path=(),
            evidence_refs=(),
            model_version=model_version,
        )

    # ------------------------------------------------------------------
    # scenario_id 派生
    # ------------------------------------------------------------------

    def _derive_scenario_id(
        self,
        *,
        symbol: str,
        as_of: str,
        scenario_name: str,
        shocks: tuple[MacroShock, ...],
        model_version: str,
    ) -> str:
        """确定性 scenario_id。"""
        shock_sig = "|".join(
            f"{s.variable.name}:{s.shock_value}:{s.shock_unit}" for s in shocks
        )
        raw = (
            f"{symbol}|{as_of}|{scenario_name}|{shock_sig}|{model_version}"
        )
        return f"scenario_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_default_stress_engine(
    *,
    horizon_months: int = 12,
    allow_correlation_only: bool = False,
) -> CompanyStressEngine:
    """构造默认公司压力测试引擎（含 5 类标准模板路径）。

    Args:
        horizon_months: 评估期（月）
        allow_correlation_only: 是否允许相关性观察路径参与传导

    Returns:
        CompanyStressEngine
    """
    engine = build_default_engine(
        horizon_months=horizon_months,
        allow_correlation_only=allow_correlation_only,
    )
    return CompanyStressEngine(engine)


__all__ = [
    "ERR_INVALID_RISK_LEVEL",
    "ERR_MISSING_BASELINE",
    "ERR_TRADE_ACTION_KEYWORD",
    "CompanyStressEngine",
    "build_default_stress_engine",
    "MacroTransmissionEngine",
    "MacroTransmissionRegistry",
]
