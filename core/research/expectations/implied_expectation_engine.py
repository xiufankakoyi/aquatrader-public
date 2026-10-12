"""P2.5 市场隐含预期引擎。

按 P2 计划 §4 Task P2.5：
- 当前市值隐含的增长率、利润率、持续年限或资本回报
- Reverse DCF 与一致预期使用独立标签（验收点 2）

输出对象：
- ImpliedMetricKind：隐含指标类型枚举
- ImpliedMetric：单个隐含指标
- ImpliedExpectationSummary：市场隐含预期汇总
- ImpliedExpectationEngine：主引擎

硬门禁（P2.5 验收 + AGENTS.md）：
- Reverse DCF 与一致预期使用独立标签：所有输出 expectation_source="implied"，
  不得写入 consensus_estimates / company_guidance 表
- 输入数据不完整时输出 None，不伪造
- 不输出交易动作建议
- 字段缺失降级处理
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.research.earnings.interval import Interval


# ---------------------------------------------------------------------------
# 隐含指标类型（P2.5 §198）
# ---------------------------------------------------------------------------

IMPLIED_METRIC_GROWTH: str = "implied_growth"
IMPLIED_METRIC_MARGIN: str = "implied_margin"
IMPLIED_METRIC_DURATION: str = "implied_duration"
IMPLIED_METRIC_RETURN_ON_CAPITAL: str = "implied_return_on_capital"

IMPLIED_METRIC_KINDS: tuple[str, ...] = (
    IMPLIED_METRIC_GROWTH,
    IMPLIED_METRIC_MARGIN,
    IMPLIED_METRIC_DURATION,
    IMPLIED_METRIC_RETURN_ON_CAPITAL,
)

# 隐含预期独立标签（验收点 2：Reverse DCF 与一致预期使用独立标签）
IMPLIED_EXPECTATION_SOURCE: str = "implied"


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _utc_now() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 隐含指标
# ---------------------------------------------------------------------------


@dataclass
class ImpliedMetric:
    """单个市场隐含指标。

    所有隐含指标必须带 `expectation_source="implied"` 标签，
    不得冒充 consensus 或 company_guidance（验收点 2）。

    Attributes:
        metric_kind: 指标类型（IMPLIED_METRIC_KINDS 之一）
        value: 隐含值
        unit: 单位（如 "ratio" / "percent" / "years"）
        as_of: PIT 时点
        company_symbol: 公司代码
        reverse_model: 反推模型名（如 "reverse_dcf" / "implied_roic"）
        model_version: 模型版本
        formula_trace: 公式轨迹
        source_refs: 来源引用列表
        expectation_source: 固定为 "implied"（验收点 2）
        assumptions_snapshot: 输入假设快照
        confidence: 置信度区间
        note: 备注
    """

    metric_kind: str = ""
    value: float = 0.0
    unit: str = "ratio"
    as_of: str = ""
    company_symbol: str = ""
    reverse_model: str = ""
    model_version: str = "v1"
    formula_trace: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    expectation_source: str = IMPLIED_EXPECTATION_SOURCE
    assumptions_snapshot: dict[str, Any] = field(default_factory=dict)
    confidence: Interval = field(default_factory=lambda: Interval(0.3, 0.5))
    note: str = ""

    def __post_init__(self) -> None:
        self.value = _finite_float(self.value) or 0.0
        self.formula_trace = _string_list(self.formula_trace)
        self.source_refs = _string_list(self.source_refs)
        self.assumptions_snapshot = _mapping(self.assumptions_snapshot)
        if not isinstance(self.confidence, Interval):
            self.confidence = Interval(0.3, 0.5)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "metric_kind": self.metric_kind,
            "value": self.value,
            "unit": self.unit,
            "as_of": self.as_of,
            "company_symbol": self.company_symbol,
            "reverse_model": self.reverse_model,
            "model_version": self.model_version,
            "formula_trace": list(self.formula_trace),
            "source_refs": list(self.source_refs),
            "expectation_source": self.expectation_source,
            "assumptions_snapshot": dict(self.assumptions_snapshot),
            "confidence": self.confidence.to_dict(),
            "note": self.note,
        }

    def is_implied_tagged(self) -> bool:
        """校验收点 2：是否带 implied 独立标签。"""
        return self.expectation_source == IMPLIED_EXPECTATION_SOURCE


@dataclass
class ImpliedExpectationSummary:
    """市场隐含预期汇总。

    验收点 2：所有指标必须带 expectation_source="implied" 标签。

    Attributes:
        summary_id: 汇总 ID
        company_symbol: 公司代码
        as_of: PIT 时点
        market_cap: 当前市值（输入）
        current_price: 当前股价（输入）
        metrics: 隐含指标列表
        all_implied_tagged: 是否全部带 implied 标签
        status: 状态（ok / not_computable / no_data / partial）
        notes: 备注
        created_at: 创建时间
    """

    summary_id: str = ""
    company_symbol: str = ""
    as_of: str = ""
    market_cap: float = 0.0
    current_price: float = 0.0
    metrics: list[ImpliedMetric] = field(default_factory=list)
    all_implied_tagged: bool = True
    status: str = "ok"
    notes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        self.market_cap = _finite_float(self.market_cap) or 0.0
        self.current_price = _finite_float(self.current_price) or 0.0
        self.metrics = [item for item in self.metrics if isinstance(item, ImpliedMetric)]
        self.notes = _string_list(self.notes)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "summary_id": self.summary_id,
            "company_symbol": self.company_symbol,
            "as_of": self.as_of,
            "market_cap": self.market_cap,
            "current_price": self.current_price,
            "metrics": [m.to_dict() for m in self.metrics],
            "all_implied_tagged": self.all_implied_tagged,
            "status": self.status,
            "notes": list(self.notes),
            "created_at": self.created_at,
        }

    def get_metric(self, kind: str) -> ImpliedMetric | None:
        """获取指定类型的隐含指标。

        Args:
            kind: IMPLIED_METRIC_KINDS 之一

        Returns:
            ImpliedMetric 或 None
        """
        for m in self.metrics:
            if m.metric_kind == kind:
                return m
        return None


# ---------------------------------------------------------------------------
# 市场隐含预期引擎
# ---------------------------------------------------------------------------


class ImpliedExpectationEngine:
    """市场隐含预期引擎（P2.5）。

    从当前市值、收入、利润等市场数据反推隐含假设。
    所有输出必须带 `expectation_source="implied"` 标签，
    不得冒充 consensus 或 company_guidance（验收点 2）。

    反推类型：
    1. 隐含增长率（IMPLIED_METRIC_GROWTH）：给定市值与基础 FCFF，反推 g
    2. 隐含利润率（IMPLIED_METRIC_MARGIN）：给定市值与收入，反推维持市值所需的利润率
    3. 隐含持续年限（IMPLIED_METRIC_DURATION）：给定市值与超额回报，反推竞争优势持续年限
    4. 隐含资本回报（IMPLIED_METRIC_RETURN_ON_CAPITAL）：给定市值与投入资本，反推 ROIC
    """

    def __init__(self, *, model_version: str = "v1") -> None:
        """初始化引擎。

        Args:
            model_version: 模型版本
        """
        self.model_version = str(model_version or "v1")

    # ------------------------------------------------------------------
    # 隐含增长率（Reverse DCF 适配）
    # ------------------------------------------------------------------

    def compute_implied_growth(
        self,
        *,
        company_symbol: str,
        as_of: str,
        market_cap: float,
        base_fcff: float,
        wacc: float,
        terminal_growth_floor: float = -0.05,
        source_refs: list[str] | None = None,
        assumptions_snapshot: dict[str, Any] | None = None,
    ) -> ImpliedMetric | None:
        """反推市场隐含增长率。

        基于简化 Gordon 模型：market_cap = base_fcff * (1 + g) / (wacc - g)
        反推 g = (market_cap * wacc - base_fcff) / (market_cap + base_fcff)

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            market_cap: 当前市值
            base_fcff: 基础 FCFF
            wacc: 加权平均资本成本
            terminal_growth_floor: 增长率下限（默认 -5%）
            source_refs: 来源引用
            assumptions_snapshot: 假设快照

        Returns:
            ImpliedMetric；输入不完整时返回 None
        """
        market_cap = _finite_float(market_cap)
        base_fcff = _finite_float(base_fcff)
        wacc = _finite_float(wacc)
        terminal_growth_floor = _finite_float(terminal_growth_floor)
        if market_cap is None or base_fcff is None or wacc is None or terminal_growth_floor is None:
            return None
        missing: list[str] = []
        if not company_symbol:
            missing.append("company_symbol")
        if not as_of:
            missing.append("as_of")
        if market_cap <= 0:
            missing.append("market_cap")
        if base_fcff <= 0:
            missing.append("base_fcff")
        if wacc <= 0 or wacc >= 1:
            missing.append("wacc")
        if missing:
            return None

        # 反推 g
        # market_cap = base_fcff * (1 + g) / (wacc - g)
        # => market_cap * (wacc - g) = base_fcff * (1 + g)
        # => market_cap * wacc - market_cap * g = base_fcff + base_fcff * g
        # => g * (market_cap + base_fcff) = market_cap * wacc - base_fcff
        # => g = (market_cap * wacc - base_fcff) / (market_cap + base_fcff)
        g = (market_cap * wacc - base_fcff) / (market_cap + base_fcff)

        # 下限保护
        if g < terminal_growth_floor:
            g = terminal_growth_floor
            note = f"反推 g < floor {terminal_growth_floor}，已夹紧"
        else:
            note = ""

        # 上限保护：g < wacc - 0.001
        if g >= wacc:
            g = wacc - 0.001
            note = (note + "; " if note else "") + f"反推 g >= wacc {wacc}，已夹紧"

        formula_trace: list[str] = [
            f"模型: market_cap = base_fcff * (1 + g) / (wacc - g)",
            f"输入: market_cap={market_cap:.4f}, base_fcff={base_fcff:.4f}, wacc={wacc:.4f}",
            f"反推: g = (market_cap * wacc - base_fcff) / (market_cap + base_fcff)",
            f"结果: implied_growth = {g:.6f}",
        ]

        return ImpliedMetric(
            metric_kind=IMPLIED_METRIC_GROWTH,
            value=g,
            unit="ratio",
            as_of=as_of,
            company_symbol=company_symbol,
            reverse_model="reverse_dcf_gordon",
            model_version=self.model_version,
            formula_trace=formula_trace,
            source_refs=_string_list(source_refs),
            expectation_source=IMPLIED_EXPECTATION_SOURCE,
            assumptions_snapshot=_mapping(assumptions_snapshot) or {
                "market_cap": market_cap,
                "base_fcff": base_fcff,
                "wacc": wacc,
            },
            confidence=Interval(0.3, 0.5),
            note=note,
        )

    # ------------------------------------------------------------------
    # 隐含利润率
    # ------------------------------------------------------------------

    def compute_implied_margin(
        self,
        *,
        company_symbol: str,
        as_of: str,
        market_cap: float,
        current_revenue: float,
        required_return: float,
        source_refs: list[str] | None = None,
        assumptions_snapshot: dict[str, Any] | None = None,
    ) -> ImpliedMetric | None:
        """反推市场隐含利润率。

        基于简化模型：market_cap = revenue * margin / required_return
        反推 margin = market_cap * required_return / revenue

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            market_cap: 当前市值
            current_revenue: 当前收入
            required_return: 要求回报率
            source_refs: 来源引用
            assumptions_snapshot: 假设快照

        Returns:
            ImpliedMetric；输入不完整时返回 None
        """
        market_cap = _finite_float(market_cap)
        current_revenue = _finite_float(current_revenue)
        required_return = _finite_float(required_return)
        if market_cap is None or current_revenue is None or required_return is None:
            return None
        missing: list[str] = []
        if not company_symbol:
            missing.append("company_symbol")
        if not as_of:
            missing.append("as_of")
        if market_cap <= 0:
            missing.append("market_cap")
        if current_revenue <= 0:
            missing.append("current_revenue")
        if required_return <= 0 or required_return >= 1:
            missing.append("required_return")
        if missing:
            return None

        margin = (market_cap * required_return) / current_revenue

        formula_trace: list[str] = [
            f"模型: market_cap = revenue * margin / required_return",
            f"输入: market_cap={market_cap:.4f}, revenue={current_revenue:.4f}, "
            f"required_return={required_return:.4f}",
            f"反推: margin = market_cap * required_return / revenue",
            f"结果: implied_margin = {margin:.6f}",
        ]

        return ImpliedMetric(
            metric_kind=IMPLIED_METRIC_MARGIN,
            value=margin,
            unit="ratio",
            as_of=as_of,
            company_symbol=company_symbol,
            reverse_model="implied_margin_perpetuity",
            model_version=self.model_version,
            formula_trace=formula_trace,
            source_refs=_string_list(source_refs),
            expectation_source=IMPLIED_EXPECTATION_SOURCE,
            assumptions_snapshot=_mapping(assumptions_snapshot) or {
                "market_cap": market_cap,
                "current_revenue": current_revenue,
                "required_return": required_return,
            },
            confidence=Interval(0.3, 0.5),
        )

    # ------------------------------------------------------------------
    # 隐含持续年限
    # ------------------------------------------------------------------

    def compute_implied_duration(
        self,
        *,
        company_symbol: str,
        as_of: str,
        market_cap: float,
        invested_capital: float,
        excess_return: float,
        wacc: float,
        source_refs: list[str] | None = None,
        assumptions_snapshot: dict[str, Any] | None = None,
    ) -> ImpliedMetric | None:
        """反推市场隐含竞争优势持续年限。

        基于剩余收益模型：market_cap = invested_capital + sum(EVA_t / (1+wacc)^t)
        简化为 annuity：market_cap = invested_capital + EVA * annuity_factor(T, wacc)
        其中 EVA = invested_capital * excess_return
        反推 T = ln(1 - (market_cap - invested_capital) * wacc / EVA) / ln(1 + wacc)

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            market_cap: 当前市值
            invested_capital: 投入资本
            excess_return: 超额回报率（ROIC - WACC）
            wacc: 加权平均资本成本
            source_refs: 来源引用
            assumptions_snapshot: 假设快照

        Returns:
            ImpliedMetric；输入不完整或反推无解时返回 None
        """
        import math

        market_cap = _finite_float(market_cap)
        invested_capital = _finite_float(invested_capital)
        excess_return = _finite_float(excess_return)
        wacc = _finite_float(wacc)
        if market_cap is None or invested_capital is None or excess_return is None or wacc is None:
            return None
        missing: list[str] = []
        if not company_symbol:
            missing.append("company_symbol")
        if not as_of:
            missing.append("as_of")
        if market_cap <= 0:
            missing.append("market_cap")
        if invested_capital <= 0:
            missing.append("invested_capital")
        if wacc <= 0 or wacc >= 1:
            missing.append("wacc")
        if missing:
            return None

        eva = invested_capital * excess_return
        if eva <= 0:
            # 无超额回报，无法反推持续年限
            return None

        excess_value = market_cap - invested_capital
        if excess_value <= 0:
            # 市值低于投入资本，无隐含持续年限
            return None

        # annuity_factor = (1 - (1+wacc)^-T) / wacc = excess_value / eva
        # (1+wacc)^-T = 1 - wacc * excess_value / eva
        ratio = 1 - wacc * excess_value / eva
        if ratio <= 0 or ratio >= 1:
            # 反推无解
            return None

        # T = -ln(ratio) / ln(1 + wacc)
        try:
            t = -math.log(ratio) / math.log(1 + wacc)
        except (ValueError, ZeroDivisionError):
            return None

        formula_trace: list[str] = [
            f"模型: market_cap = invested_capital + EVA * annuity_factor(T, wacc)",
            f"EVA = invested_capital * excess_return = {invested_capital:.4f} * {excess_return:.4f} = {eva:.4f}",
            f"excess_value = market_cap - invested_capital = {excess_value:.4f}",
            f"annuity_factor = excess_value / EVA = {excess_value / eva:.4f}",
            f"反推: T = -ln(1 - wacc*excess_value/EVA) / ln(1 + wacc)",
            f"结果: implied_duration = {t:.4f} 年",
        ]

        return ImpliedMetric(
            metric_kind=IMPLIED_METRIC_DURATION,
            value=t,
            unit="years",
            as_of=as_of,
            company_symbol=company_symbol,
            reverse_model="residual_income_annuity",
            model_version=self.model_version,
            formula_trace=formula_trace,
            source_refs=_string_list(source_refs),
            expectation_source=IMPLIED_EXPECTATION_SOURCE,
            assumptions_snapshot=_mapping(assumptions_snapshot) or {
                "market_cap": market_cap,
                "invested_capital": invested_capital,
                "excess_return": excess_return,
                "wacc": wacc,
            },
            confidence=Interval(0.3, 0.5),
        )

    # ------------------------------------------------------------------
    # 隐含资本回报（ROIC）
    # ------------------------------------------------------------------

    def compute_implied_roic(
        self,
        *,
        company_symbol: str,
        as_of: str,
        market_cap: float,
        invested_capital: float,
        wacc: float,
        duration_years: float = 10.0,
        terminal_growth: float = 0.02,
        source_refs: list[str] | None = None,
        assumptions_snapshot: dict[str, Any] | None = None,
    ) -> ImpliedMetric | None:
        """反推市场隐含 ROIC。

        基于 EVA 模型：market_cap = invested_capital + PV(EVA over T) + PV(TV)
        其中 EVA = invested_capital * (ROIC - WACC)
        PV(EVA over T) = EVA * annuity_factor(T, wacc)
        PV(TV) = EVA * (1 + g) / (wacc - g) / (1 + wacc)^T

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            market_cap: 当前市值
            invested_capital: 投入资本
            wacc: 加权平均资本成本
            duration_years: 竞争优势持续年限（默认 10 年）
            terminal_growth: 终值增长率（默认 2%）
            source_refs: 来源引用
            assumptions_snapshot: 假设快照

        Returns:
            ImpliedMetric；输入不完整或反推无解时返回 None
        """
        market_cap = _finite_float(market_cap)
        invested_capital = _finite_float(invested_capital)
        wacc = _finite_float(wacc)
        duration_years = _finite_float(duration_years)
        terminal_growth = _finite_float(terminal_growth)
        if (
            market_cap is None
            or invested_capital is None
            or wacc is None
            or duration_years is None
            or terminal_growth is None
        ):
            return None
        missing: list[str] = []
        if not company_symbol:
            missing.append("company_symbol")
        if not as_of:
            missing.append("as_of")
        if market_cap <= 0:
            missing.append("market_cap")
        if invested_capital <= 0:
            missing.append("invested_capital")
        if wacc <= 0 or wacc >= 1:
            missing.append("wacc")
        if duration_years <= 0:
            missing.append("duration_years")
        if missing:
            return None

        # 反推 ROIC
        # excess_value = market_cap - invested_capital
        # excess_value = invested_capital * (ROIC - WACC) * (
        #     annuity_factor(T, wacc) + (1 + g) / (wacc - g) / (1 + wacc)^T
        # )
        # 设 factor = annuity_factor(T, wacc) + (1+g)/(wacc-g)/(1+wacc)^T
        # ROIC = WACC + excess_value / (invested_capital * factor)
        if wacc <= terminal_growth:
            return None

        excess_value = market_cap - invested_capital
        if excess_value <= 0:
            # 市值低于投入资本，隐含 ROIC <= WACC
            implied_spread = excess_value / invested_capital
            implied_roic = wacc + implied_spread
        else:
            import math
            annuity_factor = (1 - (1 + wacc) ** (-duration_years)) / wacc
            pv_tv_factor = (1 + terminal_growth) / (wacc - terminal_growth) / ((1 + wacc) ** duration_years)
            factor = annuity_factor + pv_tv_factor
            if factor <= 0:
                return None
            implied_spread = excess_value / (invested_capital * factor)
            implied_roic = wacc + implied_spread

        formula_trace: list[str] = [
            f"模型: market_cap = invested_capital + EVA * factor",
            f"EVA = invested_capital * (ROIC - WACC)",
            f"factor = annuity_factor(T={duration_years}, wacc={wacc}) + PV_TV_factor",
            f"excess_value = market_cap - invested_capital = {excess_value:.4f}",
            f"implied_spread = excess_value / (invested_capital * factor) = {implied_spread:.6f}",
            f"结果: implied_roic = WACC + spread = {implied_roic:.6f}",
        ]

        return ImpliedMetric(
            metric_kind=IMPLIED_METRIC_RETURN_ON_CAPITAL,
            value=implied_roic,
            unit="ratio",
            as_of=as_of,
            company_symbol=company_symbol,
            reverse_model="eva_roic_reverse",
            model_version=self.model_version,
            formula_trace=formula_trace,
            source_refs=_string_list(source_refs),
            expectation_source=IMPLIED_EXPECTATION_SOURCE,
            assumptions_snapshot=_mapping(assumptions_snapshot) or {
                "market_cap": market_cap,
                "invested_capital": invested_capital,
                "wacc": wacc,
                "duration_years": duration_years,
                "terminal_growth": terminal_growth,
            },
            confidence=Interval(0.3, 0.5),
        )

    # ------------------------------------------------------------------
    # 汇总反推
    # ------------------------------------------------------------------

    def summarize(
        self,
        *,
        company_symbol: str,
        as_of: str,
        market_cap: float,
        current_price: float,
        metrics: list[ImpliedMetric | None],
    ) -> ImpliedExpectationSummary:
        """汇总市场隐含预期。

        所有指标必须带 expectation_source="implied" 标签（验收点 2）。

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            market_cap: 当前市值
            current_price: 当前股价
            metrics: 隐含指标列表（None 元素自动过滤）

        Returns:
            ImpliedExpectationSummary
        """
        valid_metrics = [m for m in metrics if isinstance(m, ImpliedMetric)] if isinstance(metrics, (list, tuple)) else []
        all_tagged = all(m.is_implied_tagged() for m in valid_metrics)

        status = "ok"
        notes: list[str] = []
        if not valid_metrics:
            status = "no_data"
            notes.append("no_implied_metrics_computed")
        elif not all_tagged:
            status = "partial"
            notes.append("not_all_metrics_implied_tagged")
        elif len(valid_metrics) < len(IMPLIED_METRIC_KINDS):
            status = "partial"
            notes.append(f"only_{len(valid_metrics)}_of_{len(IMPLIED_METRIC_KINDS)}_metrics_computed")

        return ImpliedExpectationSummary(
            summary_id=f"impl_sum_{company_symbol}_{as_of}",
            company_symbol=company_symbol,
            as_of=as_of,
            market_cap=market_cap,
            current_price=current_price,
            metrics=valid_metrics,
            all_implied_tagged=all_tagged,
            status=status,
            notes=notes,
        )


def build_implied_expectation_engine(*, model_version: str = "v1") -> ImpliedExpectationEngine:
    """工厂函数：构建 ImpliedExpectationEngine。"""
    return ImpliedExpectationEngine(model_version=model_version)
