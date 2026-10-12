"""P2.6 差异化观点构建器。

按 P2 计划 §208-227 Task P2.6：
- 强制规则：
  1. market_view 不可由系统臆测，必须来自共识或市场隐含模型
  2. internal_view 必须来自 P1 可审计模型
  3. 差异必须落到具体经营驱动，而不是「我们更乐观」
  4. 催化只描述验证事件和时间，不输出交易动作

设计：
- VariantPerceptionBuilder：构建器，从 P1.7 ConsensusSummary / P2.5 ImpliedExpectationSummary /
  P1.7 CompanyGuidance 构建 market_view；从 P1.x 模型构建 internal_view
- build() 强制校验，不合规返回 draft + rejection_reason
- promote_to_active() 二次校验，仅合规时可提升为 active
- create_revision() 创建新版本，旧版本通过仓储 supersede 标记
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .contracts import (
    DriverDifference,
    InvalidationCondition,
    InternalView,
    MarketView,
    REVIEW_FREQUENCY_QUARTERLY,
    THESIS_STATUS_ACTIVE,
    THESIS_STATUS_DRAFT,
    ThesisCatalyst,
    ValidationSchedule,
    ValuationImplication,
    VariantPerception,
    utc_now,
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _get_field(obj: Any, name: str, default: Any = "") -> Any:
    """从对象或字典安全取字段。"""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _string_list(value: Any) -> list[str]:
    return [item.strip() for item in _as_list(value) if isinstance(item, str) and item.strip()]


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# 构建器
# ---------------------------------------------------------------------------


class VariantPerceptionBuilder:
    """差异化观点构建器。

    所有 build_* 方法仅做适配与封装，不臆测数据；
    强制规则校验在 build() / promote_to_active() 中统一执行。
    """

    # ------------------------------------------------------------------
    # MarketView 构造（强制规则 1：必须来自共识/隐含/指引）
    # ------------------------------------------------------------------

    def build_market_view_from_consensus(
        self,
        *,
        consensus_summary: Any,
        summary: str = "",
    ) -> MarketView:
        """从 P1.7 ConsensusSummary 构造市场观点。

        Args:
            consensus_summary: ConsensusSummary 对象或字典（含 consensus_id / company_symbol /
                as_of / median / source_count / status 等字段）
            summary: 观点简述（自然语言）

        Returns:
            MarketView：source_type=consensus
        """
        if consensus_summary is None:
            return MarketView()

        source_ref = str(
            _get_field(consensus_summary, "consensus_id", "")
            or _get_field(consensus_summary, "summary_id", "")
        )
        as_of = str(_get_field(consensus_summary, "as_of", ""))
        retrieved_at = str(_get_field(consensus_summary, "created_at", "")) or utc_now()

        metric_values: dict[str, Any] = {
            "median": _get_field(consensus_summary, "median", 0.0),
            "source_count": _get_field(consensus_summary, "source_count", 0),
            "status": _get_field(consensus_summary, "status", ""),
        }
        # 附加 period / metric（如果存在）
        metric = _get_field(consensus_summary, "metric", "")
        period = _get_field(consensus_summary, "period", "")
        if metric:
            metric_values["metric"] = metric
        if period:
            metric_values["period"] = period

        return MarketView(
            source_type="consensus",
            source_ref=source_ref,
            as_of=as_of,
            summary=summary or f"市场一致预期：{metric} {period} 中位数 {metric_values['median']}",
            metric_values=metric_values,
            retrieved_at=retrieved_at,
        )

    def build_market_view_from_implied(
        self,
        *,
        implied_summary: Any,
        summary: str = "",
    ) -> MarketView:
        """从 P2.5 ImpliedExpectationSummary 构造市场观点。

        Args:
            implied_summary: ImpliedExpectationSummary 对象或字典（含 summary_id /
                company_symbol / as_of / metrics / status 等字段）
            summary: 观点简述

        Returns:
            MarketView：source_type=implied
        """
        if implied_summary is None:
            return MarketView()

        source_ref = str(_get_field(implied_summary, "summary_id", ""))
        as_of = str(_get_field(implied_summary, "as_of", ""))
        retrieved_at = str(_get_field(implied_summary, "created_at", "")) or utc_now()

        # metrics 可能是 list[ImpliedMetric] 或 list[dict]
        metrics_raw = _get_field(implied_summary, "metrics", [])
        metric_values: dict[str, Any] = {}
        if isinstance(metrics_raw, list):
            for m in metrics_raw:
                kind = _get_field(m, "metric_kind", "")
                value = _get_field(m, "value", None)
                if kind:
                    metric_values[kind] = value
        metric_values["status"] = _get_field(implied_summary, "status", "")

        return MarketView(
            source_type="implied",
            source_ref=source_ref,
            as_of=as_of,
            summary=summary or "市场隐含预期（基于当前市值反推）",
            metric_values=metric_values,
            retrieved_at=retrieved_at,
        )

    def build_market_view_from_guidance(
        self,
        *,
        guidance: Any,
        summary: str = "",
    ) -> MarketView:
        """从 P1.7 CompanyGuidance 构造市场观点。

        Args:
            guidance: CompanyGuidance 对象或字典（含 guidance_id / company_symbol / values 等字段）
            summary: 观点简述

        Returns:
            MarketView：source_type=company_guidance
        """
        if guidance is None:
            return MarketView()

        source_ref = str(_get_field(guidance, "guidance_id", ""))
        as_of = str(_get_field(guidance, "as_of", ""))
        retrieved_at = str(_get_field(guidance, "retrieved_at", "")) or utc_now()

        # values 是 list[ExpectationValue]
        values_raw = _get_field(guidance, "values", [])
        metric_values: dict[str, Any] = {}
        if isinstance(values_raw, list):
            for v in values_raw:
                metric = _get_field(v, "metric", "")
                if metric:
                    metric_values[metric] = {
                        "low": _get_field(v, "low", None),
                        "high": _get_field(v, "high", None),
                        "period": _get_field(v, "period", ""),
                    }

        return MarketView(
            source_type="company_guidance",
            source_ref=source_ref,
            as_of=as_of,
            summary=summary or "公司正式指引",
            metric_values=metric_values,
            retrieved_at=retrieved_at,
        )

    # ------------------------------------------------------------------
    # InternalView 构造（强制规则 2：必须来自 P1 可审计模型）
    # ------------------------------------------------------------------

    def build_internal_view_from_p1(
        self,
        *,
        model_name: str,
        model_version: str,
        source_ref: str,
        as_of: str,
        summary: str = "",
        metric_values: dict[str, Any] | None = None,
        computed_at: str = "",
    ) -> InternalView:
        """从 P1 可审计模型构造内部观点。

        Args:
            model_name: P1 模型名（如 operating_driver_builder / valuation_engine）
            model_version: 模型版本
            source_ref: 来源引用 ID（如 operating_model_id / valuation_case_id）
            as_of: PIT 时点
            summary: 观点简述
            metric_values: 关键指标值
            computed_at: 计算时间

        Returns:
            InternalView
        """
        return InternalView(
            model_name=model_name,
            model_version=model_version,
            source_ref=source_ref,
            as_of=as_of,
            summary=summary,
            metric_values=_as_dict(metric_values),
            computed_at=computed_at or utc_now(),
        )

    # ------------------------------------------------------------------
    # DriverDifference / ValuationImplication 构造（强制规则 3）
    # ------------------------------------------------------------------

    def build_driver_difference(
        self,
        *,
        driver_name: str,
        market_value: float | None,
        internal_value: float | None,
        unit: str = "",
        period: str = "",
        reasoning: str,
        evidence_refs: list[str] | None = None,
    ) -> DriverDifference:
        """构造单一经营驱动差异。

        Args:
            driver_name: 驱动名（对应 P1.3 OperatingDriver.name）
            market_value: 市场观点中的值（None 表示市场未覆盖）
            internal_value: 内部模型中的值
            unit: 单位
            period: 期间
            reasoning: 差异推理（必须非空）
            evidence_refs: 支撑证据 ID 列表

        Returns:
            DriverDifference：自动计算 delta / relative_delta
        """
        market_value = _optional_float(market_value)
        internal_value = _optional_float(internal_value)
        delta: float | None = None
        relative_delta: float | None = None
        if market_value is not None and internal_value is not None:
            delta = internal_value - market_value
            if abs(market_value) > 1e-12:
                relative_delta = delta / abs(market_value)
        elif internal_value is not None and market_value is None:
            # 市场未覆盖时，delta 仅表示内部值（标记为新增覆盖）
            delta = internal_value

        return DriverDifference(
            driver_name=driver_name,
            market_value=market_value,
            internal_value=internal_value,
            delta=delta,
            relative_delta=relative_delta,
            unit=unit,
            period=period,
            reasoning=reasoning,
            evidence_refs=_string_list(evidence_refs),
        )

    def build_valuation_implication(
        self,
        *,
        direction: str,
        magnitude_band: str,
        reasoning: str,
        evidence_refs: list[str] | None = None,
    ) -> ValuationImplication:
        """构造估值含义。"""
        return ValuationImplication(
            direction=direction,
            magnitude_band=magnitude_band,
            reasoning=reasoning,
            evidence_refs=_string_list(evidence_refs),
        )

    # ------------------------------------------------------------------
    # Catalyst / ValidationSchedule / InvalidationCondition 构造（强制规则 4）
    # ------------------------------------------------------------------

    def build_catalyst(
        self,
        *,
        event_description: str,
        expected_date: str,
        validation_type: str,
        evidence_refs: list[str] | None = None,
    ) -> ThesisCatalyst:
        """构造催化剂。

        强制规则 4：催化只描述验证事件和时间，不输出交易动作。
        本方法不强制校验禁词（校验在 build() / promote_to_active() 中执行）；
        调用方应确保 event_description 仅描述验证事件。
        """
        return ThesisCatalyst(
            event_description=event_description,
            expected_date=expected_date,
            validation_type=validation_type,
            evidence_refs=_string_list(evidence_refs),
        )

    def build_validation_schedule(
        self,
        *,
        next_review_date: str,
        frequency: str = REVIEW_FREQUENCY_QUARTERLY,
        triggers: list[str] | None = None,
    ) -> ValidationSchedule:
        """构造验证时间表。"""
        return ValidationSchedule(
            next_review_date=next_review_date,
            frequency=frequency,
            triggers=_string_list(triggers),
        )

    def build_invalidation_condition(
        self,
        *,
        condition_description: str,
        threshold: float | None = None,
        monitor_metric: str = "",
        monitor_source: str = "",
    ) -> InvalidationCondition:
        """构造证伪条件。

        condition_description 必须具体可验证，不接受「业绩不及预期」类空泛描述。
        """
        return InvalidationCondition(
            condition_description=condition_description,
            threshold=threshold,
            monitor_metric=monitor_metric,
            monitor_source=monitor_source,
        )

    # ------------------------------------------------------------------
    # 主构建：组装 VariantPerception
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        symbol: str,
        as_of: str,
        market_view: MarketView,
        internal_view: InternalView,
        difference: str,
        driver_differences: list[DriverDifference],
        valuation_implication: ValuationImplication | None = None,
        supporting_fact_ids: list[str] | None = None,
        counter_evidence_ids: list[str] | None = None,
        catalysts: list[ThesisCatalyst] | None = None,
        validation_schedule: ValidationSchedule | None = None,
        invalidation_conditions: list[InvalidationCondition] | None = None,
        uncertainty: float = 0.0,
        owner: str = "",
        reviewer: str = "",
        model_versions: dict[str, str] | None = None,
        version: int = 1,
        revision_of: str = "",
    ) -> VariantPerception:
        """组装 VariantPerception。

        本方法不抛异常；返回的 VariantPerception 总是 status=draft。
        若需提升为 active，调用 promote_to_active() 二次校验。

        Args:
            symbol: 公司代码
            as_of: PIT 时点
            market_view: 市场观点
            internal_view: 内部观点
            difference: 差异总结
            driver_differences: 驱动差异列表
            valuation_implication: 估值含义（可空，默认 neutral/small）
            supporting_fact_ids: 支撑事实 ID 列表
            counter_evidence_ids: 反证 ID 列表
            catalysts: 催化剂列表
            validation_schedule: 验证时间表
            invalidation_conditions: 证伪条件列表
            uncertainty: 不确定性（0-1）
            owner: 主分析师
            reviewer: 复核人
            model_versions: 模型版本字典
            version: 版本号
            revision_of: 旧版本 thesis_id

        Returns:
            VariantPerception（status=draft）
        """
        safe_drivers = [item for item in _as_list(driver_differences) if isinstance(item, DriverDifference)]
        safe_catalysts = [item for item in _as_list(catalysts) if isinstance(item, ThesisCatalyst)]
        safe_conditions = [
            item for item in _as_list(invalidation_conditions) if isinstance(item, InvalidationCondition)
        ]
        thesis = VariantPerception(
            symbol=symbol,
            as_of=as_of,
            market_view=market_view if isinstance(market_view, MarketView) else MarketView(),
            internal_view=internal_view if isinstance(internal_view, InternalView) else InternalView(),
            difference=difference,
            driver_differences=safe_drivers,
            valuation_implication=(
                valuation_implication
                if isinstance(valuation_implication, ValuationImplication)
                else ValuationImplication()
            ),
            supporting_fact_ids=_string_list(supporting_fact_ids),
            counter_evidence_ids=_string_list(counter_evidence_ids),
            catalysts=safe_catalysts,
            validation_schedule=(
                validation_schedule
                if isinstance(validation_schedule, ValidationSchedule)
                else ValidationSchedule()
            ),
            invalidation_conditions=safe_conditions,
            uncertainty=max(0.0, min(1.0, _safe_float(uncertainty))),
            owner=owner,
            reviewer=reviewer,
            model_versions={str(k): str(v) for k, v in _as_dict(model_versions).items()},
            status=THESIS_STATUS_DRAFT,
            version=max(1, _safe_int(version)),
            revision_of=revision_of,
        )
        return thesis

    # ------------------------------------------------------------------
    # 提升 draft → active（验收点 2）
    # ------------------------------------------------------------------

    def promote_to_active(self, thesis: VariantPerception) -> bool:
        """尝试把 draft 提升为 active。

        验收点 2：缺市场观点或内部模型时不能形成正式 VariantPerception。

        Args:
            thesis: 待提升的 VariantPerception

        Returns:
            True 表示提升成功（status=active）；False 表示拒绝（rejection_reason/details 填充）
        """
        if not isinstance(thesis, VariantPerception):
            return False
        can, reason, details = thesis.can_promote_to_active()
        if can:
            thesis.status = THESIS_STATUS_ACTIVE
            thesis.rejection_reason = ""
            thesis.rejection_details = ""
            return True

        thesis.status = THESIS_STATUS_DRAFT
        thesis.rejection_reason = reason
        thesis.rejection_details = details
        return False

    # ------------------------------------------------------------------
    # 创建新版本（验收点 3：版本不可覆盖）
    # ------------------------------------------------------------------

    def create_revision(
        self,
        *,
        old_thesis: VariantPerception,
        new_market_view: MarketView | None = None,
        new_internal_view: InternalView | None = None,
        new_difference: str | None = None,
        new_driver_differences: list[DriverDifference] | None = None,
        new_valuation_implication: ValuationImplication | None = None,
        new_catalysts: list[ThesisCatalyst] | None = None,
        new_validation_schedule: ValidationSchedule | None = None,
        new_invalidation_conditions: list[InvalidationCondition] | None = None,
        new_uncertainty: float | None = None,
        new_model_versions: dict[str, str] | None = None,
    ) -> VariantPerception:
        """基于旧版本创建新版本。

        验收点 3：版本不可覆盖。新版本 thesis_id 与旧版本不同（version 递增），
        revision_of 指向旧版本 thesis_id。旧版本数据不会被修改，需由仓储 supersede 标记。

        Args:
            old_thesis: 旧版本 VariantPerception
            new_*: 新版本字段（None 表示沿用旧版本）

        Returns:
            新版本 VariantPerception（status=draft，version=old.version+1，revision_of=old.thesis_id）
        """
        if not isinstance(old_thesis, VariantPerception):
            raise TypeError("old_thesis must be a VariantPerception")
        new_version = max(1, _safe_int(old_thesis.version)) + 1
        new_thesis = VariantPerception(
            symbol=old_thesis.symbol,
            as_of=old_thesis.as_of,
            market_view=new_market_view or old_thesis.market_view,
            internal_view=new_internal_view or old_thesis.internal_view,
            difference=new_difference if new_difference is not None else old_thesis.difference,
            driver_differences=[
                item
                for item in _as_list(
                    new_driver_differences
                    if new_driver_differences is not None
                    else old_thesis.driver_differences
                )
                if isinstance(item, DriverDifference)
            ],
            valuation_implication=(
                new_valuation_implication
                if isinstance(new_valuation_implication, ValuationImplication)
                else old_thesis.valuation_implication
            ),
            supporting_fact_ids=_string_list(old_thesis.supporting_fact_ids),
            counter_evidence_ids=_string_list(old_thesis.counter_evidence_ids),
            catalysts=[
                item
                for item in _as_list(
                    new_catalysts if new_catalysts is not None else old_thesis.catalysts
                )
                if isinstance(item, ThesisCatalyst)
            ],
            validation_schedule=(
                new_validation_schedule
                if isinstance(new_validation_schedule, ValidationSchedule)
                else old_thesis.validation_schedule
            ),
            invalidation_conditions=[
                item
                for item in _as_list(
                    new_invalidation_conditions
                    if new_invalidation_conditions is not None
                    else old_thesis.invalidation_conditions
                )
                if isinstance(item, InvalidationCondition)
            ],
            uncertainty=(
                _safe_float(new_uncertainty)
                if new_uncertainty is not None
                else _safe_float(old_thesis.uncertainty)
            ),
            owner=old_thesis.owner,
            reviewer=old_thesis.reviewer,
            model_versions={
                str(k): str(v)
                for k, v in _as_dict(
                    new_model_versions
                    if new_model_versions is not None
                    else old_thesis.model_versions
                ).items()
            },
            status=THESIS_STATUS_DRAFT,
            version=new_version,
            revision_of=old_thesis.thesis_id,
        )
        return new_thesis


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_variant_perception_builder() -> VariantPerceptionBuilder:
    """构造默认 VariantPerceptionBuilder。"""
    return VariantPerceptionBuilder()
