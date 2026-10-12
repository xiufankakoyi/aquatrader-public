"""P1.8 多方法估值与敏感性引擎。

包含以下子引擎：
- ValuationAssumptionValidator: 假设校验，缺 source_ref 返回 NOT_APPLICABLE
- DcfEngine: FCFF / FCFE DCF，输出 formula_trace 与 terminal_value_pct
- ReverseDcfEngine: 反推市场隐含增长率
- MultiplesEngine: 历史 PE/PB/PS + Forward PE/PEG/EV-EBITDA/FCF Yield
- ComparableCompanyEngine: 同业可比公司估值
- DdmEngine: 股利折现模型
- SensitivityAnalyzer: WACC × g 敏感性矩阵
- ValuationEngine: 主引擎，调度子引擎并生成 ValuationReport

设计要点：
- 所有定量输出使用 Interval 区间，避免伪精确
- 假设缺 source_ref 时返回 NOT_APPLICABLE，不使用默认值
- 方法不适用（如金融业用 EV/EBITDA）返回 NOT_APPLICABLE
- overall_range 取 min(low)~max(high)，不平均
- 不输出买入/卖出/仓位/止盈/止损等投资建议
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from core.research.earnings.interval import Interval

from .contracts import (
    Computability,
    FINANCIAL_INDUSTRY_KEYWORDS,
    METHODS_NOT_FOR_FINANCIAL,
    SensitivityPoint,
    ValuationAssumption,
    ValuationCase,
    ValuationGap,
    ValuationInput,
    ValuationMethod,
    ValuationReport,
)


# ---------------------------------------------------------------------------
# 假设校验器
# ---------------------------------------------------------------------------

class ValuationAssumptionValidator:
    """假设校验器：缺 source_ref 视为不可用。

    每个关键假设（wacc/risk_free_rate/beta/terminal_growth 等）必须显式
    提供 source_ref，否则引擎返回 NOT_APPLICABLE，禁止使用默认值兜底。
    """

    def validate(
        self,
        assumptions: list[ValuationAssumption],
        required_names: list[str],
    ) -> tuple[dict[str, ValuationAssumption], list[ValuationGap]]:
        """校验假设列表是否覆盖所需名称且每个都带 source_ref。

        Args:
            assumptions: 假设列表
            required_names: 必需假设名列表，如 ["wacc", "terminal_growth"]

        Returns:
            (assumption_map, gaps) 二元组：
              - assumption_map: name -> ValuationAssumption（仅含通过校验的）
              - gaps: 缺失或无来源的 ValuationGap 列表
        """
        assumption_map: dict[str, ValuationAssumption] = {}
        gaps: list[ValuationGap] = []
        safe_assumptions = assumptions if isinstance(assumptions, (list, tuple)) else []
        safe_required_names = [name for name in required_names if isinstance(name, str)] if isinstance(required_names, (list, tuple)) else []
        for a in safe_assumptions:
            if not isinstance(a, ValuationAssumption):
                continue
            name = getattr(a, "name", "")
            if name not in safe_required_names:
                continue
            try:
                valid_value = math.isfinite(float(a.value))
            except (TypeError, ValueError):
                valid_value = False
            if not valid_value:
                gaps.append(ValuationGap(
                    blocker_kind=f"invalid_{name}",
                    reason=f"鍋假设 {name} 必须为有限数",
                ))
                continue
            assumption_map[name] = a
        for name in safe_required_names:
            a = assumption_map.get(name)
            if a is None:
                gaps.append(ValuationGap(
                    blocker_kind=f"missing_{name}",
                    reason=f"缺少关键假设: {name}",
                ))
            elif not a.has_source():
                gaps.append(ValuationGap(
                    blocker_kind=f"missing_source_ref_{name}",
                    reason=f"假设 {name} 缺 source_ref，禁止使用默认值",
                ))
                # 缺 source_ref 时从 map 中剔除，确保引擎不会使用
                assumption_map.pop(name, None)
        return assumption_map, gaps


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _finite_series(values: Any) -> list[float]:
    if values is None or isinstance(values, (str, bytes)):
        return []
    try:
        iterator = iter(values)
    except TypeError:
        return []
    result: list[float] = []
    for value in iterator:
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(normalized):
            result.append(normalized)
    return result


def _has_invalid_values(values: Any, normalized: list[float]) -> bool:
    try:
        original = len(values)
    except TypeError:
        return True
    return original != len(normalized)


def _is_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False

def _percentile(sorted_vals: list[float], pct: float) -> float:
    """线性插值分位数。

    Args:
        sorted_vals: 已排序的非空数值列表
        pct: 百分位（0-100）

    Returns:
        分位数值
    """
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lower_idx = int(rank)
    upper_idx = min(lower_idx + 1, len(sorted_vals) - 1)
    frac = rank - lower_idx
    return sorted_vals[lower_idx] + frac * (sorted_vals[upper_idx] - sorted_vals[lower_idx])


def _net_debt(inputs: ValuationInput) -> float:
    """计算净债务 = total_debt - cash。

    Args:
        inputs: 估值输入

    Returns:
        净债务（可为负，表示现金大于债务）
    """
    return inputs.total_debt - inputs.cash_and_equivalents


def _enterprise_value(inputs: ValuationInput) -> float:
    """计算企业价值 = 市值 + 净债务 + 少数股东 - 联营投资。

    Args:
        inputs: 估值输入

    Returns:
        企业价值
    """
    return (inputs.market_cap
            + _net_debt(inputs)
            + inputs.minority_interest
            - inputs.associates_investments)


def _diluted_shares(inputs: ValuationInput) -> float:
    """获取稀释股本，缺省回退到 shares_outstanding。"""
    return inputs.diluted_shares if inputs.diluted_shares > 0 else inputs.shares_outstanding


def _build_case(
    *,
    inputs: ValuationInput,
    method: ValuationMethod,
    output_interval: Interval,
    output_per_share: Interval | None = None,
    formula_trace: list[str] | None = None,
    sensitivity: list[SensitivityPoint] | None = None,
    source_refs: list[str] | None = None,
    assumptions: list[ValuationAssumption] | None = None,
    computability: str = Computability.COMPUTABLE.value,
    limitations: list[str] | None = None,
    method_specific_metrics: dict[str, Any] | None = None,
) -> ValuationCase:
    """构造 ValuationCase 的统一工厂方法。"""
    return ValuationCase(
        company_symbol=inputs.company_symbol,
        method=method.value,
        as_of=inputs.as_of,
        scenario=inputs.scenario,
        inputs_snapshot=_inputs_snapshot(inputs),
        output_interval=output_interval,
        output_per_share=output_per_share if output_per_share is not None else Interval(0.0, 0.0),
        formula_trace=list(formula_trace or []),
        sensitivity=list(sensitivity or []),
        source_refs=list(source_refs or []),
        assumptions=list(assumptions or []),
        computability=computability,
        limitations=list(limitations or []),
        method_specific_metrics=dict(method_specific_metrics or {}),
        model_version=inputs.model_version,
    )


def _not_applicable_case(
    *,
    inputs: ValuationInput,
    method: ValuationMethod,
    reason: str,
    gaps: list[ValuationGap] | None = None,
    computability: str = Computability.NOT_APPLICABLE.value,
) -> ValuationCase:
    """构造 NOT_APPLICABLE 案例。"""
    limitations = [reason]
    if gaps:
        limitations.extend(f"{g.blocker_kind}: {g.reason}" for g in gaps)
    return ValuationCase(
        company_symbol=inputs.company_symbol,
        method=method.value,
        as_of=inputs.as_of,
        scenario=inputs.scenario,
        inputs_snapshot=_inputs_snapshot(inputs),
        computability=computability,
        limitations=limitations,
        model_version=inputs.model_version,
    )


def _inputs_snapshot(inputs: ValuationInput) -> dict[str, Any]:
    """生成输入快照（用于审计与回溯）。"""
    assumptions = inputs.assumptions if isinstance(inputs.assumptions, (list, tuple)) else []
    peers = inputs.comparable_companies if isinstance(inputs.comparable_companies, (list, tuple)) else []
    return {
        "company_symbol": inputs.company_symbol,
        "as_of": inputs.as_of,
        "scenario": inputs.scenario,
        "current_price": inputs.current_price,
        "shares_outstanding": inputs.shares_outstanding,
        "market_cap": inputs.market_cap,
        "total_debt": inputs.total_debt,
        "cash_and_equivalents": inputs.cash_and_equivalents,
        "minority_interest": inputs.minority_interest,
        "associates_investments": inputs.associates_investments,
        "diluted_shares": inputs.diluted_shares,
        "trailing_eps": inputs.trailing_eps,
        "book_value_per_share": inputs.book_value_per_share,
        "revenue_per_share": inputs.revenue_per_share,
        "consensus_eps": inputs.consensus_eps,
        "consensus_net_profit": inputs.consensus_net_profit,
        "consensus_revenue": inputs.consensus_revenue,
        "ebitda_ttm": inputs.ebitda_ttm,
        "fcf_ttm": inputs.fcf_ttm,
        "dividends_per_share": inputs.dividends_per_share,
        "fcff_series_length": len(inputs.fcff_series) if hasattr(inputs.fcff_series, "__len__") else 0,
        "fcfe_series_length": len(inputs.fcfe_series) if hasattr(inputs.fcfe_series, "__len__") else 0,
        "dividend_series_length": len(inputs.dividend_series) if hasattr(inputs.dividend_series, "__len__") else 0,
        "assumption_names": [
            a.name for a in assumptions
            if isinstance(a, ValuationAssumption) and hasattr(a, "name")
        ],
        "comparable_companies": [
            dict(peer)
            for peer in peers
            if isinstance(peer, Mapping)
        ],
        "industry": inputs.industry,
        "market_data_version": inputs.market_data_version,
        "financial_data_version": inputs.financial_data_version,
    }


# ---------------------------------------------------------------------------
# DCF 引擎
# ---------------------------------------------------------------------------

class DcfEngine:
    """FCFF / FCFE DCF 引擎。

    公式：
        PV(FCFF_t) = FCFF_t / (1+WACC)^t
        TV = FCFF_n * (1+g) / (WACC - g)
        PV(TV) = TV / (1+WACC)^n
        EV = sum(PV(FCFF_t)) + PV(TV)
        Equity = EV - total_debt + cash - minority_interest + associates
        PerShare = Equity / diluted_shares

    必需假设（每个都必须带 source_ref）：
        - wacc: 加权平均资本成本（FCFF 用）
        - cost_of_equity: 权益成本（FCFE 用）
        - terminal_growth: 终值增长率

    缺任一假设或假设无 source_ref 时返回 NOT_APPLICABLE + ValuationGap。
    """

    def compute_fcff(self, inputs: ValuationInput) -> tuple[ValuationCase, list[ValuationGap]]:
        """计算 FCFF DCF。

        Args:
            inputs: 估值输入，必须包含 fcff_series、wacc、terminal_growth 假设

        Returns:
            (ValuationCase, gaps) 二元组；不可计算时 case.computability=NOT_APPLICABLE
        """
        method = ValuationMethod.DCF_FCFF
        validator = ValuationAssumptionValidator()
        assumption_map, gaps = validator.validate(
            inputs.assumptions, ["wacc", "terminal_growth"],
        )
        if gaps:
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="FCFF DCF 缺关键假设或假设无 source_ref",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        fcff_series = _finite_series(inputs.fcff_series)
        if not fcff_series:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_fcff_series",
                reason="fcff_series 为空，无法计算 FCFF DCF",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="FCFF DCF 缺 fcff_series 序列",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        wacc = assumption_map["wacc"].value
        g = assumption_map["terminal_growth"].value
        # 防御：WACC 必须大于终值增长率，否则 TV 发散
        if wacc <= g:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="wacc_not_greater_than_growth",
                reason=f"WACC={wacc} 必须大于 terminal_growth={g}",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="WACC 不大于终值增长率，TV 发散",
                gaps=gaps, computability=Computability.NOT_APPLICABLE.value,
            ), gaps

        if _has_invalid_values(inputs.fcff_series, fcff_series):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="invalid_fcff_series_values",
                reason="fcff_series 中存在无法参与计算的非数值或非有限值，已跳过",
                as_of=inputs.as_of,
            ))
        n = len(fcff_series)

        # 1. 显式期 FCFF 现值
        pv_fcff_list: list[float] = []
        formula_trace: list[str] = []
        for t, fcff in enumerate(fcff_series, start=1):
            pv = fcff / ((1 + wacc) ** t)
            pv_fcff_list.append(pv)
            formula_trace.append(
                f"PV(FCFF_{t}) = {fcff:.4f} / (1+{wacc:.4f})^{t} = {pv:.4f}"
            )
        sum_pv_fcff = sum(pv_fcff_list)
        formula_trace.append(f"sum(PV(FCFF)) = {sum_pv_fcff:.4f}")

        # 2. 终值（Gordon Growth）
        last_fcff = fcff_series[-1]
        tv = last_fcff * (1 + g) / (wacc - g)
        pv_tv = tv / ((1 + wacc) ** n)
        formula_trace.append(
            f"TV = FCFF_{n} * (1+g) / (WACC - g) = {last_fcff:.4f} * (1+{g:.4f}) / ({wacc:.4f}-{g:.4f}) = {tv:.4f}"
        )
        formula_trace.append(
            f"PV(TV) = TV / (1+WACC)^{n} = {tv:.4f} / (1+{wacc:.4f})^{n} = {pv_tv:.4f}"
        )

        # 3. 企业价值
        ev = sum_pv_fcff + pv_tv
        formula_trace.append(f"EV = sum(PV(FCFF)) + PV(TV) = {sum_pv_fcff:.4f} + {pv_tv:.4f} = {ev:.4f}")

        # 4. 权益价值 = EV - 净债务 - 少数股东 + 联营投资
        net_debt = _net_debt(inputs)
        equity = ev - net_debt - inputs.minority_interest + inputs.associates_investments
        formula_trace.append(
            f"Equity = EV - net_debt - minority + associates = "
            f"{ev:.4f} - {net_debt:.4f} - {inputs.minority_interest:.4f} + {inputs.associates_investments:.4f} = {equity:.4f}"
        )

        # 5. 每股价值
        shares = _diluted_shares(inputs)
        per_share = equity / shares if shares > 0 else 0.0
        formula_trace.append(
            f"PerShare = Equity / diluted_shares = {equity:.4f} / {shares:.4f} = {per_share:.4f}"
        )

        # 6. 终值占比
        tv_pct = pv_tv / ev if ev != 0 else 0.0
        formula_trace.append(f"terminal_value_pct = PV(TV) / EV = {pv_tv:.4f} / {ev:.4f} = {tv_pct:.4f}")

        # 7. 敏感性矩阵：WACC ±1% step 0.5%，g ±0.5% step 0.25%
        sensitivity = SensitivityAnalyzer().wacc_x_growth(
            fcff_series=fcff_series,
            wacc_range=[wacc + d for d in (-0.01, -0.005, 0.0, 0.005, 0.01)],
            growth_range=[g + d for d in (-0.005, -0.0025, 0.0, 0.0025, 0.005)],
            net_debt=net_debt,
            minority_interest=inputs.minority_interest,
            associates_investments=inputs.associates_investments,
            shares=shares,
        )

        source_refs = [a.source_ref for a in inputs.assumptions if a.name in ("wacc", "terminal_growth") and a.has_source()]

        case = _build_case(
            inputs=inputs,
            method=method,
            output_interval=Interval(ev, ev),
            output_per_share=Interval(per_share, per_share),
            formula_trace=formula_trace,
            sensitivity=sensitivity,
            source_refs=source_refs,
            assumptions=[a for a in inputs.assumptions if a.name in ("wacc", "terminal_growth")],
            method_specific_metrics={
                "sum_pv_fcff": sum_pv_fcff,
                "pv_terminal_value": pv_tv,
                "terminal_value_pct": tv_pct,
                "wacc": wacc,
                "terminal_growth": g,
                "enterprise_value": ev,
                "equity_value": equity,
                "net_debt": net_debt,
                "diluted_shares": shares,
            },
        )
        return case, gaps

    def compute_fcfe(self, inputs: ValuationInput) -> tuple[ValuationCase, list[ValuationGap]]:
        """计算 FCFE DCF（直接折现到权益价值）。

        Args:
            inputs: 估值输入，必须包含 fcfe_series、cost_of_equity、terminal_growth

        Returns:
            (ValuationCase, gaps) 二元组
        """
        method = ValuationMethod.DCF_FCFE
        validator = ValuationAssumptionValidator()
        assumption_map, gaps = validator.validate(
            inputs.assumptions, ["cost_of_equity", "terminal_growth"],
        )
        if gaps:
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="FCFE DCF 缺关键假设或假设无 source_ref",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        fcfe_series = _finite_series(inputs.fcfe_series)
        if not fcfe_series:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_fcfe_series",
                reason="fcfe_series 为空，无法计算 FCFE DCF",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="FCFE DCF 缺 fcfe_series 序列",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        ke = assumption_map["cost_of_equity"].value
        g = assumption_map["terminal_growth"].value
        if ke <= g:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="ke_not_greater_than_growth",
                reason=f"cost_of_equity={ke} 必须大于 terminal_growth={g}",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="cost_of_equity 不大于终值增长率，TV 发散",
                gaps=gaps, computability=Computability.NOT_APPLICABLE.value,
            ), gaps

        if _has_invalid_values(inputs.fcfe_series, fcfe_series):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="invalid_fcfe_series_values",
                reason="fcfe_series 中存在无法参与计算的非数值或非有限值，已跳过",
                as_of=inputs.as_of,
            ))
        n = len(fcfe_series)
        pv_list: list[float] = []
        formula_trace: list[str] = []
        for t, fcfe in enumerate(fcfe_series, start=1):
            pv = fcfe / ((1 + ke) ** t)
            pv_list.append(pv)
            formula_trace.append(
                f"PV(FCFE_{t}) = {fcfe:.4f} / (1+{ke:.4f})^{t} = {pv:.4f}"
            )
        sum_pv = sum(pv_list)
        formula_trace.append(f"sum(PV(FCFE)) = {sum_pv:.4f}")

        last_fcfe = fcfe_series[-1]
        tv = last_fcfe * (1 + g) / (ke - g)
        pv_tv = tv / ((1 + ke) ** n)
        formula_trace.append(
            f"TV = FCFE_{n} * (1+g) / (ke - g) = {last_fcfe:.4f} * (1+{g:.4f}) / ({ke:.4f}-{g:.4f}) = {tv:.4f}"
        )
        formula_trace.append(
            f"PV(TV) = TV / (1+ke)^{n} = {tv:.4f} / (1+{ke:.4f})^{n} = {pv_tv:.4f}"
        )

        equity = sum_pv + pv_tv
        formula_trace.append(
            f"Equity = sum(PV(FCFE)) + PV(TV) = {sum_pv:.4f} + {pv_tv:.4f} = {equity:.4f}"
        )

        shares = _diluted_shares(inputs)
        per_share = equity / shares if shares > 0 else 0.0
        formula_trace.append(
            f"PerShare = Equity / diluted_shares = {equity:.4f} / {shares:.4f} = {per_share:.4f}"
        )

        tv_pct = pv_tv / equity if equity != 0 else 0.0
        formula_trace.append(f"terminal_value_pct = PV(TV) / Equity = {pv_tv:.4f} / {equity:.4f} = {tv_pct:.4f}")

        source_refs = [a.source_ref for a in inputs.assumptions if a.name in ("cost_of_equity", "terminal_growth") and a.has_source()]

        case = _build_case(
            inputs=inputs,
            method=method,
            output_interval=Interval(equity, equity),
            output_per_share=Interval(per_share, per_share),
            formula_trace=formula_trace,
            source_refs=source_refs,
            assumptions=[a for a in inputs.assumptions if a.name in ("cost_of_equity", "terminal_growth")],
            method_specific_metrics={
                "sum_pv_fcfe": sum_pv,
                "pv_terminal_value": pv_tv,
                "terminal_value_pct": tv_pct,
                "cost_of_equity": ke,
                "terminal_growth": g,
                "equity_value": equity,
                "diluted_shares": shares,
            },
        )
        return case, gaps


# ---------------------------------------------------------------------------
# Reverse DCF 引擎
# ---------------------------------------------------------------------------

class ReverseDcfEngine:
    """Reverse DCF：给定市值反推隐含增长率。

    固定 WACC，反推使 PV(FCFF) + PV(TV) = 目标企业价值的 g。
    目标 EV = market_cap + net_debt + minority - associates。
    使用二分法求解 g ∈ (-0.05, WACC - 0.001)。

    反推结果必须标记为 implied，不得冒充 consensus。
    """

    def reverse_engineer(self, inputs: ValuationInput) -> tuple[ValuationCase, list[ValuationGap]]:
        """反推市场隐含增长率。

        Args:
            inputs: 估值输入，必须包含 fcff_series、wacc 假设、market_cap

        Returns:
            (ValuationCase, gaps) 二元组；method_specific_metrics 含 implied_growth
        """
        method = ValuationMethod.REVERSE_DCF
        validator = ValuationAssumptionValidator()
        assumption_map, gaps = validator.validate(inputs.assumptions, ["wacc"])
        if gaps:
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF 缺 wacc 假设或假设无 source_ref",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        fcff_series = _finite_series(inputs.fcff_series)
        if not fcff_series:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_fcff_series",
                reason="fcff_series 为空，无法反推",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF 缺 fcff_series 序列",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        if inputs.market_cap <= 0:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_market_cap",
                reason="market_cap <= 0，无法反推隐含增长率",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF 缺 market_cap",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        wacc = assumption_map["wacc"].value
        net_debt = _net_debt(inputs)
        target_ev = (inputs.market_cap + net_debt
                     + inputs.minority_interest - inputs.associates_investments)
        if _has_invalid_values(inputs.fcff_series, fcff_series):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="invalid_fcff_series_values",
                reason="fcff_series 中存在无法参与计算的非数值或非有限值，已跳过",
                as_of=inputs.as_of,
            ))

        implied_g = self._solve_implied_growth(fcff_series, wacc, target_ev)
        if implied_g is None:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="implied_growth_not_converged",
                reason=f"二分法在 g∈[-0.05, {wacc-0.001:.4f}] 内未收敛到 target_ev={target_ev:.4f}",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF 二分法未收敛",
                gaps=gaps, computability=Computability.NOT_APPLICABLE.value,
            ), gaps

        # 用隐含 g 重新计算并产出公式轨迹
        ev_check, pv_tv_check = self._compute_ev(fcff_series, wacc, implied_g)
        formula_trace: list[str] = [
            f"target_ev = market_cap + net_debt + minority - associates = "
            f"{inputs.market_cap:.4f} + {net_debt:.4f} + {inputs.minority_interest:.4f} - {inputs.associates_investments:.4f} = {target_ev:.4f}",
            f"二分法求解 g 使得 EV(FCFF, g, WACC={wacc:.4f}) = target_ev",
            f"implied_growth (g) = {implied_g:.6f}",
            f"校验: EV( implied_g ) = {ev_check:.4f} (target={target_ev:.4f})",
            f"校验: PV(TV) = {pv_tv_check:.4f}",
        ]
        shares = _diluted_shares(inputs)
        per_share = inputs.current_price  # Reverse DCF 每股输出即为当前价（市场隐含）

        source_refs = [a.source_ref for a in inputs.assumptions if a.name == "wacc" and a.has_source()]

        case = _build_case(
            inputs=inputs,
            method=method,
            output_interval=Interval(target_ev, target_ev),
            output_per_share=Interval(per_share, per_share),
            formula_trace=formula_trace,
            source_refs=source_refs,
            assumptions=[a for a in inputs.assumptions if a.name == "wacc"],
            method_specific_metrics={
                "implied_growth": implied_g,
                "implied_terminal_growth": implied_g,
                "wacc": wacc,
                "market_cap": inputs.market_cap,
                "target_enterprise_value": target_ev,
                "pv_terminal_value": pv_tv_check,
                "diluted_shares": shares,
                # P2.5 验收点 2：Reverse DCF 与一致预期使用独立标签
                # - is_implied/source_kind: P1.8 原有标签
                # - expectation_source: P2.5 统一标签，与 ImpliedMetric 对齐
                "is_implied": True,
                "source_kind": "implied",
                "expectation_source": "implied",
            },
            limitations=["反推结果为市场隐含假设，不得冒充 consensus"],
        )
        return case, gaps

    def reverse_engineer_wacc(self, inputs: ValuationInput) -> tuple[ValuationCase, list[ValuationGap]]:
        """反推市场隐含 WACC。

        固定 terminal_growth，反推使 PV(FCFF) + PV(TV) = 目标企业价值的 WACC。

        Args:
            inputs: 估值输入，必须包含 fcff_series、terminal_growth 假设、market_cap

        Returns:
            (ValuationCase, gaps) 二元组；method_specific_metrics 含 implied_wacc
        """
        method = ValuationMethod.REVERSE_DCF
        validator = ValuationAssumptionValidator()
        assumption_map, gaps = validator.validate(inputs.assumptions, ["terminal_growth"])
        if gaps:
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF (WACC) 缺 terminal_growth 假设或假设无 source_ref",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        fcff_series = _finite_series(inputs.fcff_series)
        if not fcff_series:
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_fcff_series",
                reason="fcff_series 为空，无法反推 WACC",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF (WACC) 缺 fcff_series",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        if inputs.market_cap <= 0:
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_market_cap",
                reason="market_cap <= 0，无法反推 WACC",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF (WACC) 缺 market_cap",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        g = assumption_map["terminal_growth"].value
        net_debt = _net_debt(inputs)
        target_ev = (inputs.market_cap + net_debt
                     + inputs.minority_interest - inputs.associates_investments)
        if _has_invalid_values(inputs.fcff_series, fcff_series):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="invalid_fcff_series_values",
                reason="fcff_series 中存在无法参与计算的非数值或非有限值，已跳过",
                as_of=inputs.as_of,
            ))

        implied_wacc = self._solve_implied_wacc(fcff_series, g, target_ev)
        if implied_wacc is None:
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="implied_wacc_not_converged",
                reason=f"二分法在 wacc∈(g+0.001, 0.30] 内未收敛到 target_ev={target_ev:.4f}",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Reverse DCF (WACC) 二分法未收敛",
                gaps=gaps, computability=Computability.NOT_APPLICABLE.value,
            ), gaps

        ev_check, pv_tv_check = self._compute_ev(fcff_series, implied_wacc, g)
        formula_trace: list[str] = [
            f"target_ev = market_cap + net_debt + minority - associates = {target_ev:.4f}",
            f"固定 terminal_growth={g:.4f}，二分法求解 WACC",
            f"implied_wacc = {implied_wacc:.6f}",
            f"校验: EV(implied_wacc, g) = {ev_check:.4f} (target={target_ev:.4f})",
            f"校验: PV(TV) = {pv_tv_check:.4f}",
        ]
        source_refs = [a.source_ref for a in inputs.assumptions if a.name == "terminal_growth" and a.has_source()]

        case = _build_case(
            inputs=inputs,
            method=method,
            output_interval=Interval(target_ev, target_ev),
            output_per_share=Interval(inputs.current_price, inputs.current_price),
            formula_trace=formula_trace,
            source_refs=source_refs,
            assumptions=[a for a in inputs.assumptions if a.name == "terminal_growth"],
            method_specific_metrics={
                "implied_wacc": implied_wacc,
                "terminal_growth": g,
                "market_cap": inputs.market_cap,
                "target_enterprise_value": target_ev,
                "pv_terminal_value": pv_tv_check,
                # P2.5 验收点 2：Reverse DCF 与一致预期使用独立标签
                "is_implied": True,
                "source_kind": "implied",
                "expectation_source": "implied",
                "reverse_mode": "wacc",
            },
            limitations=["反推结果为市场隐含假设，不得冒充 consensus"],
        )
        return case, gaps

    @staticmethod
    def _compute_ev(fcff_series: list[float], wacc: float, g: float) -> tuple[float, float]:
        """计算给定 (wacc, g) 下的企业价值。

        Returns:
            (enterprise_value, pv_terminal_value)
        """
        n = len(fcff_series)
        sum_pv = 0.0
        for t, fcff in enumerate(fcff_series, start=1):
            sum_pv += fcff / ((1 + wacc) ** t)
        last_fcff = fcff_series[-1]
        tv = last_fcff * (1 + g) / (wacc - g)
        pv_tv = tv / ((1 + wacc) ** n)
        return sum_pv + pv_tv, pv_tv

    def _solve_implied_growth(
        self,
        fcff_series: list[float],
        wacc: float,
        target_ev: float,
        *,
        max_iter: int = 200,
        tol: float = 1e-6,
    ) -> float | None:
        """二分法求解隐含增长率 g。

        Args:
            fcff_series: FCFF 序列
            wacc: WACC
            target_ev: 目标企业价值
            max_iter: 最大迭代次数
            tol: 收敛容差

        Returns:
            隐含 g；不收敛返回 None
        """
        lo, hi = -0.05, wacc - 0.001
        if hi <= lo:
            return None
        ev_lo, _ = self._compute_ev(fcff_series, wacc, lo)
        ev_hi, _ = self._compute_ev(fcff_series, wacc, hi)
        # 检查目标是否在 [ev_lo, ev_hi] 范围内
        if target_ev < ev_lo or target_ev > ev_hi:
            # 扩展搜索范围仍无解时返回 None
            return None
        for _ in range(max_iter):
            mid = (lo + hi) / 2.0
            ev_mid, _ = self._compute_ev(fcff_series, wacc, mid)
            if abs(ev_mid - target_ev) < tol:
                return mid
            if ev_mid < target_ev:
                lo = mid  # 增大 g 使 EV 增大
            else:
                hi = mid
        return (lo + hi) / 2.0

    def _solve_implied_wacc(
        self,
        fcff_series: list[float],
        g: float,
        target_ev: float,
        *,
        max_iter: int = 200,
        tol: float = 1e-6,
    ) -> float | None:
        """二分法求解隐含 WACC。

        Args:
            fcff_series: FCFF 序列
            g: 终值增长率
            target_ev: 目标企业价值
            max_iter: 最大迭代次数
            tol: 收敛容差

        Returns:
            隐含 WACC；不收敛返回 None
        """
        lo, hi = g + 0.001, 0.30
        if hi <= lo:
            return None
        ev_lo, _ = self._compute_ev(fcff_series, lo, g)  # wacc 低 -> EV 高
        ev_hi, _ = self._compute_ev(fcff_series, hi, g)  # wacc 高 -> EV 低
        if target_ev > ev_lo or target_ev < ev_hi:
            return None
        for _ in range(max_iter):
            mid = (lo + hi) / 2.0
            ev_mid, _ = self._compute_ev(fcff_series, mid, g)
            if abs(ev_mid - target_ev) < tol:
                return mid
            # wacc 增大 -> EV 减小
            if ev_mid > target_ev:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# 倍数引擎
# ---------------------------------------------------------------------------

class MultiplesEngine:
    """历史 PE/PB/PS 分位 + Forward PE/PEG/EV-EBITDA/FCF Yield。"""

    def compute_historical(
        self,
        inputs: ValuationInput,
        method: ValuationMethod,
    ) -> tuple[ValuationCase, list[ValuationGap]]:
        """计算历史倍数分位估值。

        Args:
            inputs: 估值输入
            method: 必须为 HISTORICAL_PE / HISTORICAL_PB / HISTORICAL_PS 之一

        Returns:
            (ValuationCase, gaps) 二元组
        """
        gaps: list[ValuationGap] = []
        historical_multiples = inputs.historical_multiples if isinstance(inputs.historical_multiples, Mapping) else {}
        raw_series = historical_multiples.get(method.value, [])
        series = _finite_series(raw_series)
        if not series:
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind=f"missing_{method.value}_series",
                reason=f"historical_multiples 缺 {method.value} 序列",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason=f"历史 {method.value} 序列缺失",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        if _has_invalid_values(raw_series, series):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="invalid_historical_series_values",
                reason="historical_multiples 中存在无法参与计算的非数值或非有限值，已跳过",
                as_of=inputs.as_of,
            ))
        sorted_vals = sorted(series)
        p25 = _percentile(sorted_vals, 25)
        median = _percentile(sorted_vals, 50)
        p75 = _percentile(sorted_vals, 75)
        min_v = sorted_vals[0]
        max_v = sorted_vals[-1]

        # 推导 per-share 基础
        basis: float = 0.0
        basis_name: str = ""
        if method == ValuationMethod.HISTORICAL_PE:
            basis = inputs.trailing_eps or inputs.consensus_eps
            basis_name = "trailing_eps" if inputs.trailing_eps > 0 else "consensus_eps"
        elif method == ValuationMethod.HISTORICAL_PB:
            basis = inputs.book_value_per_share
            basis_name = "book_value_per_share"
        elif method == ValuationMethod.HISTORICAL_PS:
            shares = _diluted_shares(inputs)
            if inputs.revenue_per_share > 0:
                basis = inputs.revenue_per_share
                basis_name = "revenue_per_share"
            elif inputs.consensus_revenue > 0 and shares > 0:
                basis = inputs.consensus_revenue / shares
                basis_name = "consensus_revenue_per_share"
            else:
                gaps.append(ValuationGap(
                    company_symbol=inputs.company_symbol,
                    method=method.value,
                    blocker_kind="missing_revenue_or_shares",
                    reason="缺 consensus_revenue 或 diluted_shares",
                    as_of=inputs.as_of,
                ))
                return _not_applicable_case(
                    inputs=inputs, method=method,
                    reason="historical_ps 缺收入或股本",
                    gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
                ), gaps

        if not _is_positive(basis):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind=f"missing_{basis_name}",
                reason=f"{basis_name} <= 0，无法计算目标价",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason=f"{basis_name} 缺失或非正",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        # 当前倍数
        current_price = inputs.current_price
        current_multiple = current_price / basis if basis > 0 else 0.0
        # 当前分位数
        current_percentile = self._percentile_rank(sorted_vals, current_multiple)

        # 目标价区间 = p25 * basis ~ p75 * basis
        target_low = p25 * basis
        target_high = p75 * basis
        formula_trace = [
            f"historical {method.value} series (n={len(series)}): min={min_v:.4f} p25={p25:.4f} median={median:.4f} p75={p75:.4f} max={max_v:.4f}",
            f"{basis_name} = {basis:.4f}",
            f"current_multiple = current_price / {basis_name} = {current_price:.4f} / {basis:.4f} = {current_multiple:.4f}",
            f"current_percentile = {current_percentile:.4f}",
            f"target_low = p25 * {basis_name} = {p25:.4f} * {basis:.4f} = {target_low:.4f}",
            f"target_high = p75 * {basis_name} = {p75:.4f} * {basis:.4f} = {target_high:.4f}",
        ]

        case = _build_case(
            inputs=inputs,
            method=method,
            output_interval=Interval(p25, p75),
            output_per_share=Interval(target_low, target_high),
            formula_trace=formula_trace,
            method_specific_metrics={
                "p25": p25, "median": median, "p75": p75,
                "min": min_v, "max": max_v,
                "current_multiple": current_multiple,
                "current_percentile": current_percentile,
                "basis_name": basis_name,
                "basis_value": basis,
                "target_low": target_low,
                "target_high": target_high,
            },
        )
        return case, gaps

    def compute_forward(
        self,
        inputs: ValuationInput,
        method: ValuationMethod,
    ) -> tuple[ValuationCase, list[ValuationGap]]:
        """计算 Forward 倍数（PE/PEG/EV-EBITDA/FCF Yield）。

        Args:
            inputs: 估值输入
            method: 必须为 FORWARD_PE / PEG / EV_EBITDA / FCF_YIELD 之一

        Returns:
            (ValuationCase, gaps) 二元组
        """
        gaps: list[ValuationGap] = []

        # 行业过滤：金融业不用 EV/EBITDA / FCF Yield
        if inputs.is_financial_industry() and method.value in METHODS_NOT_FOR_FINANCIAL:
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="method_mismatch_financial_industry",
                reason=f"金融业不适用 {method.value}",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason=f"金融业不适用 {method.value}",
                gaps=gaps, computability=Computability.METHOD_MISMATCH.value,
            ), gaps

        if method == ValuationMethod.FORWARD_PE:
            return self._forward_pe(inputs), gaps
        if method == ValuationMethod.PEG:
            return self._peg(inputs), gaps
        if method == ValuationMethod.EV_EBITDA:
            return self._ev_ebitda(inputs), gaps
        if method == ValuationMethod.FCF_YIELD:
            return self._fcf_yield(inputs), gaps

        gaps.append(ValuationGap(
            company_symbol=inputs.company_symbol,
            method=method.value,
            blocker_kind="unknown_forward_method",
            reason=f"未知 forward 方法: {method.value}",
            as_of=inputs.as_of,
        ))
        return _not_applicable_case(
            inputs=inputs, method=method,
            reason=f"未知 forward 方法: {method.value}",
            gaps=gaps, computability=Computability.NOT_APPLICABLE.value,
        ), gaps

    def _forward_pe(self, inputs: ValuationInput) -> ValuationCase:
        """Forward PE = market_cap / consensus_net_profit。"""
        method = ValuationMethod.FORWARD_PE
        if not _is_positive(inputs.market_cap) or not _is_positive(inputs.consensus_net_profit):
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="Forward PE 缺 market_cap 或 consensus_net_profit",
                computability=Computability.INSUFFICIENT_INPUT.value,
            )
        forward_pe = inputs.market_cap / inputs.consensus_net_profit
        eps = inputs.consensus_eps
        per_share_low = forward_pe * eps * 0.9
        per_share_high = forward_pe * eps * 1.1
        formula_trace = [
            f"forward_pe = market_cap / consensus_net_profit = {inputs.market_cap:.4f} / {inputs.consensus_net_profit:.4f} = {forward_pe:.4f}",
            f"per_share_range = forward_pe * consensus_eps * [0.9, 1.1] = {per_share_low:.4f} ~ {per_share_high:.4f}",
        ]
        return _build_case(
            inputs=inputs, method=method,
            output_interval=Interval(forward_pe, forward_pe),
            output_per_share=Interval(per_share_low, per_share_high),
            formula_trace=formula_trace,
            method_specific_metrics={
                "forward_pe": forward_pe,
                "market_cap": inputs.market_cap,
                "consensus_net_profit": inputs.consensus_net_profit,
                "consensus_eps": eps,
            },
        )

    def _peg(self, inputs: ValuationInput) -> ValuationCase:
        """PEG = PE / growth_rate（缺 growth 不可计算）。"""
        method = ValuationMethod.PEG
        validator = ValuationAssumptionValidator()
        assumption_map, gaps = validator.validate(inputs.assumptions, ["growth_rate"])
        if gaps or not _is_positive(inputs.consensus_eps) or not _is_positive(inputs.current_price):
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="PEG 缺 growth_rate 假设或 EPS/价格缺失",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            )
        growth = assumption_map["growth_rate"].value
        if growth <= 0:
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="growth_rate <= 0，PEG 不可计算",
                computability=Computability.NOT_APPLICABLE.value,
            )
        pe = inputs.current_price / inputs.consensus_eps
        peg = pe / (growth * 100)  # PEG = PE / (growth%)
        formula_trace = [
            f"PE = current_price / consensus_eps = {inputs.current_price:.4f} / {inputs.consensus_eps:.4f} = {pe:.4f}",
            f"growth_rate = {growth:.4f} ({growth*100:.2f}%)",
            f"PEG = PE / (growth%) = {pe:.4f} / {growth*100:.2f} = {peg:.4f}",
        ]
        return _build_case(
            inputs=inputs, method=method,
            output_interval=Interval(peg, peg),
            output_per_share=Interval(inputs.current_price * 0.9, inputs.current_price * 1.1),
            formula_trace=formula_trace,
            assumptions=[a for a in inputs.assumptions if a.name == "growth_rate"],
            source_refs=[a.source_ref for a in inputs.assumptions if a.name == "growth_rate" and a.has_source()],
            method_specific_metrics={
                "pe": pe, "growth_rate": growth, "peg": peg,
            },
        )

    def _ev_ebitda(self, inputs: ValuationInput) -> ValuationCase:
        """EV/EBITDA = (market_cap + net_debt + minority - associates) / ebitda_ttm。"""
        method = ValuationMethod.EV_EBITDA
        if not _is_positive(inputs.ebitda_ttm):
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="EV/EBITDA 缺 ebitda_ttm 或非正",
                computability=Computability.INSUFFICIENT_INPUT.value,
            )
        net_debt = _net_debt(inputs)
        ev = (inputs.market_cap + net_debt
              + inputs.minority_interest - inputs.associates_investments)
        ev_ebitda = ev / inputs.ebitda_ttm
        formula_trace = [
            f"net_debt = total_debt - cash = {inputs.total_debt:.4f} - {inputs.cash_and_equivalents:.4f} = {net_debt:.4f}",
            f"EV = market_cap + net_debt + minority - associates = {inputs.market_cap:.4f} + {net_debt:.4f} + {inputs.minority_interest:.4f} - {inputs.associates_investments:.4f} = {ev:.4f}",
            f"EV/EBITDA = EV / ebitda_ttm = {ev:.4f} / {inputs.ebitda_ttm:.4f} = {ev_ebitda:.4f}",
        ]
        return _build_case(
            inputs=inputs, method=method,
            output_interval=Interval(ev_ebitda, ev_ebitda),
            output_per_share=Interval(inputs.current_price * 0.9, inputs.current_price * 1.1),
            formula_trace=formula_trace,
            method_specific_metrics={
                "ev": ev, "net_debt": net_debt,
                "ebitda_ttm": inputs.ebitda_ttm,
                "ev_ebitda": ev_ebitda,
            },
        )

    def _fcf_yield(self, inputs: ValuationInput) -> ValuationCase:
        """FCF Yield = fcf_ttm / market_cap。"""
        method = ValuationMethod.FCF_YIELD
        if not _is_positive(inputs.market_cap) or not _is_positive(inputs.fcf_ttm):
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="FCF Yield 缺 market_cap 或 fcf_ttm",
                computability=Computability.INSUFFICIENT_INPUT.value,
            )
        fcf_yield = inputs.fcf_ttm / inputs.market_cap
        formula_trace = [
            f"fcf_yield = fcf_ttm / market_cap = {inputs.fcf_ttm:.4f} / {inputs.market_cap:.4f} = {fcf_yield:.4f}",
        ]
        return _build_case(
            inputs=inputs, method=method,
            output_interval=Interval(fcf_yield, fcf_yield),
            output_per_share=Interval(inputs.current_price * 0.9, inputs.current_price * 1.1),
            formula_trace=formula_trace,
            method_specific_metrics={
                "fcf_ttm": inputs.fcf_ttm,
                "market_cap": inputs.market_cap,
                "fcf_yield": fcf_yield,
            },
        )

    @staticmethod
    def _percentile_rank(sorted_vals: list[float], value: float) -> float:
        """计算 value 在 sorted_vals 中的分位排名（0~1）。"""
        if not sorted_vals:
            return 0.0
        n = len(sorted_vals)
        # 计算小于等于 value 的占比
        rank = 0
        for v in sorted_vals:
            if v <= value:
                rank += 1
            else:
                break
        return rank / n


# ---------------------------------------------------------------------------
# 可比公司引擎
# ---------------------------------------------------------------------------

class ComparableCompanyEngine:
    """同业可比公司估值。

    取 comparable_companies 列表，每个含 symbol + pe/pb/ev_ebitda。
    计算同业分位数，目标价 = 同业中位 × 公司对应基础。
    """

    def compute(self, inputs: ValuationInput) -> tuple[ValuationCase, list[ValuationGap]]:
        """计算同业可比公司估值。

        Args:
            inputs: 估值输入，必须包含 comparable_companies 列表

        Returns:
            (ValuationCase, gaps) 二元组
        """
        method = ValuationMethod.COMPARABLE_COMPANY
        gaps: list[ValuationGap] = []
        raw_peers = inputs.comparable_companies if isinstance(inputs.comparable_companies, (list, tuple)) else []
        peers = [peer for peer in raw_peers if isinstance(peer, Mapping)]
        raw_peer_values = [peer.get("pe") for peer in peers]
        peer_pes = [value for value in _finite_series(raw_peer_values) if value > 0]
        if _has_invalid_values(raw_peer_values, _finite_series(raw_peer_values)):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="invalid_comparable_pe_values",
                reason="comparable_companies 中存在无法参与计算的 PE，已跳过",
                as_of=inputs.as_of,
            ))
        if not peer_pes:
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_comparable_pe",
                reason="comparable_companies 缺有效的 pe 字段",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="缺同业 PE 数据",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        if not _is_positive(inputs.consensus_eps):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_consensus_eps",
                reason="consensus_eps <= 0，无法计算目标价",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="缺 consensus_eps",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        sorted_pes = sorted(peer_pes)
        p25 = _percentile(sorted_pes, 25)
        median = _percentile(sorted_pes, 50)
        p75 = _percentile(sorted_pes, 75)
        target_low = p25 * inputs.consensus_eps
        target_high = p75 * inputs.consensus_eps
        formula_trace = [
            f"comparable companies (n={len(peer_pes)}): p25={p25:.4f} median={median:.4f} p75={p75:.4f}",
            f"consensus_eps = {inputs.consensus_eps:.4f}",
            f"target_low = peer_p25 * eps = {p25:.4f} * {inputs.consensus_eps:.4f} = {target_low:.4f}",
            f"target_high = peer_p75 * eps = {p75:.4f} * {inputs.consensus_eps:.4f} = {target_high:.4f}",
        ]
        case = _build_case(
            inputs=inputs,
            method=method,
            output_interval=Interval(p25, p75),
            output_per_share=Interval(target_low, target_high),
            formula_trace=formula_trace,
            method_specific_metrics={
                "peer_p25": p25, "peer_median": median, "peer_p75": p75,
                "peer_count": len(peer_pes),
                "consensus_eps": inputs.consensus_eps,
                "target_low": target_low,
                "target_high": target_high,
            },
        )
        return case, gaps


# ---------------------------------------------------------------------------
# DDM 引擎
# ---------------------------------------------------------------------------

class DdmEngine:
    """DDM 股利折现模型。

    适用于分红稳定的公司。
    PV(dividends) + PV(terminal_value)。
    缺 dividend 数据返回 NOT_APPLICABLE。
    """

    def compute(self, inputs: ValuationInput) -> tuple[ValuationCase, list[ValuationGap]]:
        """计算 DDM。

        Args:
            inputs: 估值输入，必须包含 dividend_series 与 cost_of_equity/terminal_growth 假设

        Returns:
            (ValuationCase, gaps) 二元组
        """
        method = ValuationMethod.DDM
        validator = ValuationAssumptionValidator()
        assumption_map, gaps = validator.validate(
            inputs.assumptions, ["cost_of_equity", "terminal_growth"],
        )
        if gaps:
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="DDM 缺关键假设或假设无 source_ref",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps
        div_series = _finite_series(inputs.dividend_series)
        if not div_series:
            gap = ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="missing_dividend_series",
                reason="dividend_series 为空，DDM 不可计算",
                as_of=inputs.as_of,
            )
            gaps.append(gap)
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="DDM 缺 dividend_series",
                gaps=gaps, computability=Computability.INSUFFICIENT_INPUT.value,
            ), gaps

        ke = assumption_map["cost_of_equity"].value
        g = assumption_map["terminal_growth"].value
        if ke <= g:
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="ke_not_greater_than_growth",
                reason=f"cost_of_equity={ke} 必须大于 terminal_growth={g}",
                as_of=inputs.as_of,
            ))
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason="cost_of_equity 不大于终值增长率",
                gaps=gaps, computability=Computability.NOT_APPLICABLE.value,
            ), gaps

        if _has_invalid_values(inputs.dividend_series, div_series):
            gaps.append(ValuationGap(
                company_symbol=inputs.company_symbol,
                method=method.value,
                blocker_kind="invalid_dividend_series_values",
                reason="dividend_series 中存在无法参与计算的非数值或非有限值，已跳过",
                as_of=inputs.as_of,
            ))
        n = len(div_series)
        pv_list: list[float] = []
        formula_trace: list[str] = []
        for t, d in enumerate(div_series, start=1):
            pv = d / ((1 + ke) ** t)
            pv_list.append(pv)
            formula_trace.append(f"PV(D_{t}) = {d:.4f} / (1+{ke:.4f})^{t} = {pv:.4f}")
        sum_pv = sum(pv_list)
        last_d = div_series[-1]
        tv = last_d * (1 + g) / (ke - g)
        pv_tv = tv / ((1 + ke) ** n)
        formula_trace.append(
            f"TV = D_{n} * (1+g) / (ke - g) = {last_d:.4f} * (1+{g:.4f}) / ({ke:.4f}-{g:.4f}) = {tv:.4f}"
        )
        formula_trace.append(f"PV(TV) = {tv:.4f} / (1+{ke:.4f})^{n} = {pv_tv:.4f}")
        price = sum_pv + pv_tv
        formula_trace.append(f"Price_per_share = sum(PV(D)) + PV(TV) = {sum_pv:.4f} + {pv_tv:.4f} = {price:.4f}")

        source_refs = [a.source_ref for a in inputs.assumptions if a.name in ("cost_of_equity", "terminal_growth") and a.has_source()]

        case = _build_case(
            inputs=inputs,
            method=method,
            output_interval=Interval(price, price),
            output_per_share=Interval(price, price),
            formula_trace=formula_trace,
            source_refs=source_refs,
            assumptions=[a for a in inputs.assumptions if a.name in ("cost_of_equity", "terminal_growth")],
            method_specific_metrics={
                "sum_pv_dividends": sum_pv,
                "pv_terminal_value": pv_tv,
                "terminal_value_pct": pv_tv / price if price != 0 else 0.0,
                "cost_of_equity": ke,
                "terminal_growth": g,
            },
        )
        return case, gaps


# ---------------------------------------------------------------------------
# 敏感性分析器
# ---------------------------------------------------------------------------

class SensitivityAnalyzer:
    """敏感性矩阵生成器。

    生成 WACC × g 双变量敏感性矩阵。
    输出每个 WACC 点和每个 g 点的输出区间（min/max over the other axis）。
    """

    def wacc_x_growth(
        self,
        fcff_series: list[float],
        wacc_range: list[float],
        growth_range: list[float],
        *,
        net_debt: float = 0.0,
        minority_interest: float = 0.0,
        associates_investments: float = 0.0,
        shares: float = 1.0,
    ) -> list[SensitivityPoint]:
        """生成 WACC × g 敏感性矩阵。

        Args:
            fcff_series: FCFF 预测期序列
            wacc_range: WACC 取值列表
            growth_range: 终值增长率取值列表
            net_debt: 净债务（用于计算权益价值）
            minority_interest: 少数股东权益
            associates_investments: 联营投资
            shares: 稀释股本（用于计算每股价值）

        Returns:
            SensitivityPoint 列表，每个变量（wacc/growth）输出一个点，
            output_low/high 为该变量取值下另一变量全范围内的最小/最大每股价值
        """
        fcff_series = _finite_series(fcff_series)
        wacc_range = _finite_series(wacc_range)
        growth_range = _finite_series(growth_range)
        if not fcff_series or not wacc_range or not growth_range:
            return []
        # 计算矩阵：matrix[wacc][growth] = per_share_value
        matrix: dict[float, dict[float, float]] = {}
        for w in wacc_range:
            matrix[w] = {}
            for g in growth_range:
                if w <= g:
                    matrix[w][g] = float("nan")
                    continue
                ev, _ = ReverseDcfEngine._compute_ev(fcff_series, w, g)
                equity = ev - net_debt - minority_interest + associates_investments
                per_share = equity / shares if shares > 0 else 0.0
                matrix[w][g] = per_share

        points: list[SensitivityPoint] = []
        # 每个 wacc 一个点：output_low/high = 该 wacc 下所有 g 的 min/max per_share
        for w in wacc_range:
            vals = [v for v in matrix[w].values() if not (isinstance(v, float) and v != v)]  # 排除 NaN
            if not vals:
                continue
            points.append(SensitivityPoint(
                variable_name="wacc",
                variable_value=w,
                output_low=min(vals),
                output_high=max(vals),
            ))
        # 每个 growth 一个点
        for g in growth_range:
            vals: list[float] = []
            for w in wacc_range:
                v = matrix[w].get(g)
                if v is not None and not (isinstance(v, float) and v != v):
                    vals.append(v)
            if not vals:
                continue
            points.append(SensitivityPoint(
                variable_name="growth",
                variable_value=g,
                output_low=min(vals),
                output_high=max(vals),
            ))
        return points


# ---------------------------------------------------------------------------
# 主引擎
# ---------------------------------------------------------------------------

class ValuationEngine:
    """主引擎：调度子引擎生成多方法估值报告。

    默认尝试所有适用方法；金融业自动跳过 EV/EBITDA / SOTP / FCF Yield / DCF。
    不适用方法返回 NOT_APPLICABLE，不参与 overall_range 汇总。
    overall_range = min(low) ~ max(high)，标注为 "min_max"，不平均。
    """

    DEFAULT_METHODS: tuple[ValuationMethod, ...] = (
        ValuationMethod.HISTORICAL_PE,
        ValuationMethod.HISTORICAL_PB,
        ValuationMethod.HISTORICAL_PS,
        ValuationMethod.FORWARD_PE,
        ValuationMethod.PEG,
        ValuationMethod.EV_EBITDA,
        ValuationMethod.FCF_YIELD,
        ValuationMethod.DCF_FCFF,
        ValuationMethod.DCF_FCFE,
        ValuationMethod.DDM,
        ValuationMethod.COMPARABLE_COMPANY,
        ValuationMethod.REVERSE_DCF,
    )

    def __init__(self) -> None:
        """初始化子引擎。"""
        self.dcf = DcfEngine()
        self.reverse_dcf = ReverseDcfEngine()
        self.multiples = MultiplesEngine()
        self.comparable = ComparableCompanyEngine()
        self.ddm = DdmEngine()

    def compute_report(
        self,
        inputs: ValuationInput,
        methods: list[str] | None = None,
    ) -> ValuationReport:
        """计算多方法估值报告。

        Args:
            inputs: 估值输入
            methods: 指定方法列表（ValuationMethod.value）；None 时使用全部默认方法

        Returns:
            ValuationReport 实例
        """
        method_values = methods if isinstance(methods, (list, tuple)) else [m.value for m in self.DEFAULT_METHODS]
        cases: list[ValuationCase] = []
        for method_value in method_values:
            case = self._compute_single(inputs, method_value)
            cases.append(case)

        # 汇总可计算案例
        computable = [c for c in cases if c.is_computable()]
        if computable:
            lows = [c.output_per_share.low for c in computable if c.output_per_share.low != 0]
            highs = [c.output_per_share.high for c in computable if c.output_per_share.high != 0]
            # 若每股区间为空，则回退到企业价值区间
            if not lows and not highs:
                lows = [c.output_interval.low for c in computable]
                highs = [c.output_interval.high for c in computable]
            overall_low = min(lows) if lows else 0.0
            overall_high = max(highs) if highs else 0.0
            overall_range = Interval(overall_low, overall_high)
        else:
            overall_range = Interval(0.0, 0.0)

        # 生成方法差异说明
        method_differences = self._explain_differences(computable)

        # 报告级限制
        limitations: list[str] = []
        not_applicable_methods = [c.method for c in cases if not c.is_computable()]
        if not_applicable_methods:
            limitations.append(
                f"以下方法不可计算: {', '.join(not_applicable_methods)}"
            )
        if not computable:
            limitations.append("无任何方法可计算，overall_range 退化为 [0, 0]")

        return ValuationReport(
            company_symbol=inputs.company_symbol,
            as_of=inputs.as_of,
            scenario=inputs.scenario,
            cases=cases,
            method_differences=method_differences,
            overall_range=overall_range,
            overall_range_method="min_max",
            limitations=limitations,
            model_version=inputs.model_version,
        )

    def compute_single(
        self,
        inputs: ValuationInput,
        method: str,
    ) -> ValuationCase:
        """计算单个方法。

        Args:
            inputs: 估值输入
            method: ValuationMethod.value

        Returns:
            ValuationCase 实例
        """
        return self._compute_single(inputs, method)

    def _compute_single(self, inputs: ValuationInput, method_value: str) -> ValuationCase:
        """调度子引擎计算单个方法。"""
        try:
            method = ValuationMethod(method_value)
        except ValueError:
            return _not_applicable_case(
                inputs=inputs,
                method=ValuationMethod.HISTORICAL_PE,  # 占位
                reason=f"未知估值方法: {method_value}",
                computability=Computability.NOT_APPLICABLE.value,
            )

        # 行业过滤：金融业跳过不适用方法
        if inputs.is_financial_industry() and method.value in METHODS_NOT_FOR_FINANCIAL:
            return _not_applicable_case(
                inputs=inputs, method=method,
                reason=f"金融业不适用 {method.value}",
                computability=Computability.METHOD_MISMATCH.value,
            )

        if method == ValuationMethod.DCF_FCFF:
            case, _ = self.dcf.compute_fcff(inputs)
            return case
        if method == ValuationMethod.DCF_FCFE:
            case, _ = self.dcf.compute_fcfe(inputs)
            return case
        if method == ValuationMethod.REVERSE_DCF:
            case, _ = self.reverse_dcf.reverse_engineer(inputs)
            return case
        if method == ValuationMethod.DDM:
            case, _ = self.ddm.compute(inputs)
            return case
        if method == ValuationMethod.COMPARABLE_COMPANY:
            case, _ = self.comparable.compute(inputs)
            return case
        if method in (ValuationMethod.HISTORICAL_PE, ValuationMethod.HISTORICAL_PB, ValuationMethod.HISTORICAL_PS):
            case, _ = self.multiples.compute_historical(inputs, method)
            return case
        if method in (ValuationMethod.FORWARD_PE, ValuationMethod.PEG,
                      ValuationMethod.EV_EBITDA, ValuationMethod.FCF_YIELD):
            case, _ = self.multiples.compute_forward(inputs, method)
            return case
        # SOTP / RESIDUAL_INCOME 暂未实现
        return _not_applicable_case(
            inputs=inputs, method=method,
            reason=f"方法 {method.value} 暂未实现",
            computability=Computability.NOT_APPLICABLE.value,
        )

    @staticmethod
    def _explain_differences(cases: list[ValuationCase]) -> list[dict[str, Any]]:
        """生成方法差异说明（不做简单平均）。"""
        if len(cases) <= 1:
            return []
        diffs: list[dict[str, Any]] = []
        sorted_cases = sorted(cases, key=lambda c: c.output_per_share.mid)
        for i in range(len(sorted_cases) - 1):
            c1 = sorted_cases[i]
            c2 = sorted_cases[i + 1]
            mid1 = c1.output_per_share.mid
            mid2 = c2.output_per_share.mid
            delta = mid2 - mid1
            pct = (delta / mid1) if mid1 != 0 else 0.0
            diffs.append({
                "method_a": c1.method,
                "method_b": c2.method,
                "mid_a": round(mid1, 6),
                "mid_b": round(mid2, 6),
                "delta": round(delta, 6),
                "delta_pct": round(pct, 6),
                "explanation": (
                    f"{c2.method} 中位值 {mid2:.4f} 相比 {c1.method} 中位值 {mid1:.4f} "
                    f"{'高' if delta > 0 else '低'} {abs(delta):.4f} ({abs(pct)*100:.2f}%)；"
                    "差异源于方法学不同（如 DCF 反映远期现金流折现，PE 反映当前盈利倍数），"
                    "不做简单平均，由使用者结合业务判断加权。"
                ),
            })
        # 汇总说明
        mids = [c.output_per_share.mid for c in cases]
        diffs.append({
            "type": "overall_summary",
            "methods": [c.method for c in cases],
            "mid_min": round(min(mids), 6) if mids else 0.0,
            "mid_max": round(max(mids), 6) if mids else 0.0,
            "mid_range": round(max(mids) - min(mids), 6) if mids else 0.0,
            "explanation": (
                f"各方法中位值范围 {min(mids):.4f} ~ {max(mids):.4f}，"
                "差异反映不同方法学假设；overall_range 采用 min_max 而非均值，"
                "避免单一方法偏差被平均掩盖。"
            ),
        })
        return diffs
