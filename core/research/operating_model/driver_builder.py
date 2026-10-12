"""P1.3 经营驱动模型构建器。

按 P1 计划 §5 Task P1.3：
1. 从 P0 已落库的 observed_facts（historical_disclosed）构建驱动模型。
2. 历史事实只能入 drivers（observation_kind=historical_disclosed），
   不得自动成为未来假设（不入 assumptions）。
3. 前瞻假设必须显式提供（forward_disclosed_indicators 或外部输入），
   且必须有 source_ref；定性词不得转数值。
4. 多产品共享收入基数检测（detect_duplicate_revenue_base）。
5. 单位、币种、期间不一致阻断（OperatingModel.validate_consistency）。
6. 缺 blocker 输入输出 gap，不输出精确预测。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Iterable

from .contracts import (
    BUSINESS_MODELS,
    DRIVER_KINDS,
    OBSERVATION_KIND_FORWARD,
    OBSERVATION_KIND_HISTORICAL,
    QUALITATIVE_WORDS_FORBIDDEN,
    DriverKind,
    ForwardAssumption,
    OperatingDriver,
    OperatingGap,
    OperatingModel,
    Segment,
    validate_business_model,
    validate_driver_kind,
    validate_observation_kind,
)


# 同一 (segment, period) 下，多个 product_revenue 驱动总和与公司总收入差异阈值
DUPLICATE_REVENUE_BASE_THRESHOLD: float = 0.05  # 5%


def _to_float(value: Any) -> float | None:
    """安全转 float。

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
    return number if math.isfinite(number) else None


def _normalize_period(period: Any) -> str:
    """标准化期间为 YYYY-MM-DD。

    Args:
        period: 原始期间字符串

    Returns:
        YYYY-MM-DD 格式；空值返回空串
    """
    if not period:
        return ""
    text = str(period).strip().replace("/", "").replace("-", "")
    if len(text) > 8 and text.isdigit():
        text = text[:8]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(period)


def _normalize_metric_to_kind(metric: str) -> str:
    """把 observed_fact 的 metric 名映射到 DRIVER_KINDS。

    覆盖 P0 observed_facts 中的常见 metric 名（中英文）。

    Args:
        metric: 原始 metric 名

    Returns:
        DRIVER_KINDS 之一；未命中返回空串
    """
    if not metric:
        return ""
    text = str(metric).strip().lower()
    # 容量类
    if any(k in text for k in ("名义产能", "nameplate_capacity", "design_capacity")):
        return "nameplate_capacity"
    if any(k in text for k in ("有效产能", "effective_capacity", "实际产能")):
        return "effective_capacity"
    if any(k in text for k in ("投产", "爬坡", "ramp_up", "commissioning")):
        return "ramp_up_date"
    # 利用率/良率/销量
    if any(k in text for k in ("利用率", "utilization", "capacity_utilization")):
        return "utilization_rate"
    if any(k in text for k in ("良率", "yield", "合格率")):
        return "yield_rate"
    if any(k in text for k in ("销量", "出货量", "sales_volume", "shipment")):
        return "sales_volume"
    # 价格/成本
    if text in ("asp", "平均售价", "average_selling_price") or "asp" in text:
        return "asp"
    if any(k in text for k in ("价格传导", "price_passthrough")):
        return "price_passthrough"
    if any(k in text for k in ("单位成本", "unit_cost")):
        return "unit_cost"
    # 收入类
    if any(k in text for k in ("产品收入", "product_revenue")):
        return "product_revenue"
    if any(k in text for k in ("分部收入", "segment_revenue")):
        return "segment_revenue_exposure"
    # 利润率/费用率/税率
    if any(k in text for k in ("毛利率", "gross_margin")):
        return "gross_margin"
    if any(k in text for k in ("费用率", "expense_ratio")):
        return "expense_ratio"
    if any(k in text for k in ("税率", "tax_rate")):
        return "tax_rate"
    # 市场
    if any(k in text for k in ("市场规模", "market_size", "tam")):
        return "market_size"
    if any(k in text for k in ("渗透率", "penetration")):
        return "penetration"
    if any(k in text for k in ("单位价值", "unit_value")):
        return "unit_value"
    if any(k in text for k in ("份额", "market_share", "share")):
        return "market_share"
    # 股本/稀释
    if any(k in text for k in ("股本", "share_count", "total_shares")):
        return "share_count"
    if any(k in text for k in ("稀释", "dilution")):
        return "dilution_factor"
    return ""


def detect_duplicate_revenue_base(
    drivers: Iterable[OperatingDriver],
    company_revenue: float | None = None,
    threshold: float = DUPLICATE_REVENUE_BASE_THRESHOLD,
) -> list[str]:
    """检测多产品共享收入基数导致的重复计算。

    场景：同一 period 下多个 product_revenue 驱动来自不同产品但
    实际上共享同一收入基数（如 "新能源车" 与 "乘用车 EV" 重叠），
    导致产品收入之和超过公司总收入。

    Args:
        drivers: 全部驱动列表
        company_revenue: 公司总收入（可选，用于检测产品收入之和 vs 总收入）
        threshold: 差异阈值（占 company_revenue 的比例）

    Returns:
        重复计算告警列表
    """
    warnings: list[str] = []
    # 按 period 聚合所有 segment 的 product_revenue 总和
    revenue_by_period: dict[str, float] = {}
    for d in drivers if isinstance(drivers, (list, tuple, set)) else []:
        if not isinstance(d, OperatingDriver):
            continue
        if d.kind == "product_revenue" and d.value is not None and d.period:
            revenue_by_period[d.period] = revenue_by_period.get(d.period, 0.0) + d.value

    # 同期间产品收入之和 vs 公司总收入
    company_total = _to_float(company_revenue)
    threshold_value = _to_float(threshold)
    if (
        company_total is not None
        and company_total > 0
        and threshold_value is not None
        and threshold_value >= 0
    ):
        company_revenue = company_total
        threshold = threshold_value
        for period, total in revenue_by_period.items():
            if total > company_total * (1.0 + threshold_value):
                warnings.append(
                    f"期间 {period} 的产品收入之和 "
                    f"{total:.2f} 超过公司总收入 {company_revenue:.2f} "
                    f"（超出 {threshold:.0%}），可能存在重复计算"
                )

    return warnings


def _is_qualitative_only(text: str) -> bool:
    """判断文本是否仅由定性词组成（禁止转为数值幅度）。

    规则：文本包含定性词且不含"具体数值+单位"或"百分比"等定量表达时，
    视为定性词文本，禁止转成数值幅度。

    Args:
        text: 假设文本

    Returns:
        True 表示仅含定性词，禁止转为数值
    """
    if not text:
        return False
    cleaned = text.strip()
    if not cleaned:
        return False
    has_qualitative = any(w in cleaned for w in QUALITATIVE_WORDS_FORBIDDEN)
    if not has_qualitative:
        return False
    # 检测是否含具体数值表达（含 % 或数字+单位）
    import re
    # 含百分比（如 "10%"、"50 %"）
    if re.search(r"\d+(\.\d+)?\s*%", cleaned):
        return False
    # 含数值 + 单位（如 "100 GWh"、"50 吨"、"1.5 亿元"）
    if re.search(r"\d+(\.\d+)?\s*(GWh|吨|kg|元|万元|亿元|Wh|千件|万件|百万)", cleaned):
        return False
    # 排除年份：把 "2025"、"2024" 等连续 4 位数字视为期间而非数值
    cleaned_no_year = re.sub(r"\b(19|20)\d{2}\b", "", cleaned)
    # 若去除年份后仍有数字，视为含定量
    if any(ch.isdigit() for ch in cleaned_no_year):
        return False
    # 否则视为纯定性
    return True


def _build_driver_from_fact(fact: dict[str, Any]) -> OperatingDriver | None:
    """从 observed_fact dict 构建 OperatingDriver。

    Args:
        fact: observed_fact dict（含 product_name/period/metric/value/unit/source_evidence_id 等）

    Returns:
        OperatingDriver；metric 无法映射到 kind 时返回 None
    """
    if not isinstance(fact, Mapping):
        return None
    metric = str(fact.get("metric") or "")
    kind = _normalize_metric_to_kind(metric)
    if not kind:
        return None
    value = _to_float(fact.get("value"))
    unit = str(fact.get("unit") or "")
    period = _normalize_period(fact.get("period"))
    source_ref = str(
        fact.get("source_evidence_id") or fact.get("claim_id") or ""
    )
    product_name = str(fact.get("product_name") or "")
    grounding = "grounding_ok" if source_ref and value is not None else "missing"

    return OperatingDriver(
        name=product_name or kind,
        kind=kind,
        value=value,
        unit=unit,
        currency="",  # observed_fact 通常不直接带 currency，由 model 统一
        period=period,
        source_ref=source_ref,
        observation_kind=OBSERVATION_KIND_HISTORICAL,
        grounding_status=grounding,
        reported_value=value,
        segment_name=product_name,
    )


def _build_assumption_from_disclosed(
    disclosed: dict[str, Any],
) -> ForwardAssumption | None:
    """从 forward_disclosed_indicator 构建 ForwardAssumption。

    Args:
        disclosed: forward_disclosed dict（含 metric/period/value/source_evidence_id 等）

    Returns:
        ForwardAssumption；缺 source_ref 或仅含定性词时返回 None（应输出 gap）
    """
    if not isinstance(disclosed, Mapping):
        return None
    metric = str(disclosed.get("metric") or "")
    kind = _normalize_metric_to_kind(metric)
    if not kind:
        return None
    source_ref = str(
        disclosed.get("source_evidence_id") or disclosed.get("claim_id") or ""
    )
    if not source_ref:
        return None  # 缺 source_ref → 走 gap

    value = _to_float(disclosed.get("value"))
    unit = str(disclosed.get("unit") or "")
    period = _normalize_period(disclosed.get("period"))
    text = str(disclosed.get("quote") or disclosed.get("assumption_text") or "")

    # 检查定性词转数值
    if value is not None and _is_qualitative_only(text):
        return None  # 触发 gap

    return ForwardAssumption(
        name=str(disclosed.get("product_name") or disclosed.get("object_name") or kind),
        kind=kind,
        value=value,
        unit=unit,
        currency="",
        period=period,
        source_ref=source_ref,
        assumption_text=text,
        basis="disclosed_forward_plan",
    )


def _segment_index(model: OperatingModel, segment_name: str) -> Segment:
    """获取或创建分部。"""
    for seg in model.segments:
        if seg.segment_name == segment_name:
            return seg
    new_seg = Segment(segment_name=segment_name)
    model.segments.append(new_seg)
    return new_seg


def build_operating_model(
    *,
    symbol: str,
    period: str,
    business_model: str,
    observed_facts: list[dict[str, Any]] | None = None,
    forward_disclosed: list[dict[str, Any]] | None = None,
    forward_assumptions: list[ForwardAssumption] | None = None,
    company_revenue: float | None = None,
    currency: str = "CNY",
    unit: str = "元",
    source_trace_id: str = "",
) -> OperatingModel:
    """构建公司经营驱动模型。

    按 P1 计划 §5 Task P1.3：
    - 历史事实（observed_facts）→ drivers（observation_kind=historical_disclosed）
    - 前瞻披露（forward_disclosed）→ assumptions（需 source_ref；定性词转数值则改 gap）
    - 显式前瞻假设（forward_assumptions）→ assumptions（需 source_ref）
    - 缺 source_ref 或定性词转数值 → gap
    - 多产品共享收入基数检测
    - 单位/币种/期间一致性校验

    Args:
        symbol: 公司股票代码
        period: 模型基准期间 YYYY-MM-DD
        business_model: 商业模式（BUSINESS_MODELS 之一）
        observed_facts: P0 observed_facts 列表（每项含 metric/period/value 等）
        forward_disclosed: P0 forward_disclosed_indicators 列表
        forward_assumptions: 显式前瞻假设（须带 source_ref）
        company_revenue: 公司总收入（用于检测重复收入基数）
        currency: 模型统一币种
        unit: 模型统一单位
        source_trace_id: 模型来源追溯 ID

    Returns:
        OperatingModel 实例，含 drivers/assumptions/gaps/warnings/errors

    Raises:
        ValueError: 当 business_model 非法
    """
    validate_business_model(business_model)

    model = OperatingModel(
        symbol=symbol,
        business_model=business_model,
        period=_normalize_period(period),
        currency=currency,
        unit=unit,
        source_trace_id=source_trace_id or f"operating_model:{symbol}",
    )

    # ---- 1. 历史事实 → drivers ----
    for fact in observed_facts if isinstance(observed_facts, (list, tuple)) else []:
        driver = _build_driver_from_fact(fact)
        if driver is None:
            continue  # metric 无法映射到 kind，跳过
        segment_name = driver.segment_name or "_company"
        if segment_name == "_company":
            model.drivers.append(driver)
        else:
            seg = _segment_index(model, segment_name)
            seg.drivers.append(driver)

    # ---- 2. 前瞻披露 → assumptions or gaps ----
    for disclosed in forward_disclosed if isinstance(forward_disclosed, (list, tuple)) else []:
        assumption = _build_assumption_from_disclosed(disclosed)
        if assumption is not None:
            model.assumptions.append(assumption)
        else:
            metric = str(disclosed.get("metric") or "")
            kind = _normalize_metric_to_kind(metric) or "unknown"
            model.gaps.append(OperatingGap(
                name=str(disclosed.get("product_name") or disclosed.get("object_name") or metric),
                kind=kind,
                period=_normalize_period(disclosed.get("period")),
                reason="forward_evidence_missing_or_qualitative_only",
                blocker_kind=(
                    "qualitative_only"
                    if _is_qualitative_only(str(disclosed.get("quote") or ""))
                    else "forward_evidence_missing"
                ),
            ))

    # ---- 3. 显式前瞻假设 → assumptions（需 source_ref） ----
    for assumption in (
        forward_assumptions if isinstance(forward_assumptions, (list, tuple)) else []
    ):
        if not isinstance(assumption, ForwardAssumption):
            continue
        if not assumption.source_ref:
            model.gaps.append(OperatingGap(
                name=assumption.name,
                kind=assumption.kind,
                period=assumption.period,
                reason="forward_assumption_missing_source_ref",
                blocker_kind="forward_evidence_missing",
            ))
            continue
        if assumption.value is not None and _is_qualitative_only(assumption.assumption_text):
            model.gaps.append(OperatingGap(
                name=assumption.name,
                kind=assumption.kind,
                period=assumption.period,
                reason="qualitative_only_text_cannot_convert_to_value",
                blocker_kind="qualitative_only",
            ))
            continue
        model.assumptions.append(assumption)

    # ---- 4. 检测重复收入基数 ----
    model.duplicate_revenue_warnings = detect_duplicate_revenue_base(
        model.all_drivers(), company_revenue=company_revenue
    )

    # ---- 5. 一致性校验 ----
    model.consistency_errors = model.validate_consistency()

    return model
