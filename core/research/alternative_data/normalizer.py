"""P3.3 另类数据标准化器。

按 P3 计划 Task P3.3 硬规则：
- 另类信号必须先成为观察或假设，不能直接升级事实
- 覆盖变化不会被误判为公司经营变化
- 实体映射方法必须明确

本模块提供：
- AlternativeDataNormalizer：将原始另类数据标准化为 AlternativeDataSignal
- CoverageChangeDetector：检测数据源覆盖范围变化，防止误判为公司经营变化
- EntityMapper：实体映射工具（按契约声明的 entity_mapping_method 执行）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from collections.abc import Mapping
from math import isfinite

from core.research.alternative_data.contracts import (
    AlternativeDataError,
    AlternativeDataSourceContract,
    AlternativeDataSourceStatus,
    AlternativeDataSignal,
    CoverageChange,
    CoverageChangeError,
    SignalTier,
    assert_authorized_source,
    assert_coverage_change_not_operational,
    assert_signal_not_fact,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _entity_set(values: Any) -> set[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {item.strip() for item in values if isinstance(item, str) and item.strip()}


class NormalizerError(ValueError):
    """标准化器错误。"""


class EntityMapper:
    """实体映射工具。

    按数据源契约声明的 entity_mapping_method 执行实体映射：
    - direct: 原始实体 ID 直接使用（如股票代码）
    - alias_table: 通过别名表映射
    - fuzzy_match: 模糊匹配公司名称
    """

    def __init__(self, mapping_method: str) -> None:
        """初始化实体映射器。

        Args:
            mapping_method: 实体映射方法（direct/alias_table/fuzzy_match）
        """
        self._method = _text(mapping_method)
        self._alias_table: dict[str, str] = {}

    @property
    def method(self) -> str:
        """实体映射方法。"""
        return self._method

    def set_alias_table(self, table: dict[str, str]) -> None:
        """设置别名表（仅 alias_table 方法使用）。

        Args:
            table: 别名 -> 标准实体 ID 的映射
        """
        if not isinstance(table, Mapping):
            self._alias_table = {}
            return
        self._alias_table = {
            alias_text: entity_text
            for alias, entity in table.items()
            if (alias_text := _text(alias)) and (entity_text := _text(entity))
        }

    def map_entity(self, raw_entity_id: str) -> str:
        """将原始实体 ID 映射为标准实体 ID。

        Args:
            raw_entity_id: 原始实体 ID

        Returns:
            标准实体 ID

        Raises:
            NormalizerError: 映射方法不支持或映射失败
        """
        if not isinstance(raw_entity_id, str) or not raw_entity_id.strip():
            raise NormalizerError("原始实体 ID 不能为空")
        raw_entity_id = raw_entity_id.strip()
        if self._method == "direct":
            return raw_entity_id.strip()
        if self._method == "alias_table":
            mapped = self._alias_table.get(raw_entity_id)
            if mapped is None:
                raise NormalizerError(
                    f"别名表中未找到实体映射：{raw_entity_id}"
                )
            return str(mapped)
        if self._method == "fuzzy_match":
            # 模糊匹配：简化实现，实际应接入公司名称标准化服务
            # 这里仅做精确匹配作为降级处理
            mapped = self._alias_table.get(raw_entity_id)
            return str(mapped) if mapped is not None else raw_entity_id.strip()
        raise NormalizerError(
            f"不支持的实体映射方法：{self._method}"
        )


class CoverageChangeDetector:
    """数据源覆盖变化检测器。

    按 P3.3 硬规则「覆盖变化不会被误判为公司经营变化」：
    - 对比新旧覆盖范围，识别新增/移除的实体
    - 生成 CoverageChange 标记，明确声明不代表公司经营变化
    - CoverageChange 不得包含任何经营变化措辞
    """

    def __init__(self, source_id: str) -> None:
        """初始化覆盖变化检测器。

        Args:
            source_id: 数据源 ID
        """
        self._source_id = source_id
        self._last_coverage: set[str] = set()

    def update_coverage(
        self, current_entities: list[str], detected_at: str = ""
    ) -> list[CoverageChange]:
        """更新覆盖范围并检测变化。

        Args:
            current_entities: 当前覆盖的实体 ID 列表
            detected_at: 检测时间（ISO 格式）；默认当前时间

        Returns:
            CoverageChange 列表（added 和 removed）
        """
        timestamp = _text(detected_at) or datetime.now(timezone.utc).isoformat()
        current_set = _entity_set(current_entities)
        added = current_set - self._last_coverage
        removed = self._last_coverage - current_set
        changes: list[CoverageChange] = []
        if added:
            change = CoverageChange(
                source_id=self._source_id,
                change_type="added",
                affected_entities=sorted(added),
                detected_at=timestamp,
            )
            assert_coverage_change_not_operational(change)
            changes.append(change)
        if removed:
            change = CoverageChange(
                source_id=self._source_id,
                change_type="removed",
                affected_entities=sorted(removed),
                detected_at=timestamp,
            )
            assert_coverage_change_not_operational(change)
            changes.append(change)
        self._last_coverage = current_set
        return changes

    def set_baseline(self, entities: list[str]) -> None:
        """设置覆盖基线（不触发变化检测）。"""
        self._last_coverage = _entity_set(entities)


class AlternativeDataNormalizer:
    """另类数据标准化器。

    按 P3.3 硬规则：
    - 另类信号必须先成为观察或假设，不能直接升级事实
    - 来自另类数据源的信号默认 tier=OBSERVATION
    - 实体映射必须按契约声明的 entity_mapping_method 执行
    - 数据源必须 active 且已授权才能标准化信号
    """

    def __init__(self, source: AlternativeDataSourceContract) -> None:
        """初始化标准化器。

        Args:
            source: 数据源契约

        Raises:
            NormalizerError: 数据源状态非 active
        """
        if not isinstance(source, AlternativeDataSourceContract):
            raise NormalizerError("数据源契约无效")
        if source.status is not AlternativeDataSourceStatus.ACTIVE:
            raise NormalizerError(
                f"数据源必须 active 才能标准化信号（当前 {source.status.value}）"
            )
        try:
            assert_authorized_source(source)
        except AlternativeDataError as exc:
            raise NormalizerError(str(exc)) from exc
        self._source = source
        self._entity_mapper = EntityMapper(source.entity_mapping_method)
        self._coverage_detector = CoverageChangeDetector(source.source_id)

    @property
    def source(self) -> AlternativeDataSourceContract:
        """数据源契约。"""
        return self._source

    @property
    def entity_mapper(self) -> EntityMapper:
        """实体映射器。"""
        return self._entity_mapper

    @property
    def coverage_detector(self) -> CoverageChangeDetector:
        """覆盖变化检测器。"""
        return self._coverage_detector

    def set_alias_table(self, table: dict[str, str]) -> None:
        """设置实体别名表。"""
        self._entity_mapper.set_alias_table(table)

    def normalize_signal(
        self,
        signal_id: str,
        raw_entity_id: str,
        metric_name: str,
        metric_value: float,
        timestamp: str,
        tier: SignalTier = SignalTier.OBSERVATION,
        raw_payload: dict[str, Any] | None = None,
    ) -> AlternativeDataSignal:
        """将原始数据标准化为 AlternativeDataSignal。

        按 P3.3 硬规则「另类信号必须先成为观察或假设，不能直接升级事实」：
        - 默认 tier=OBSERVATION
        - 不允许直接设置为 FACT

        Args:
            signal_id: 信号唯一 ID
            raw_entity_id: 原始实体 ID
            metric_name: 指标名
            metric_value: 指标值
            timestamp: 信号时间戳（ISO 格式）
            tier: 信号分层（默认 OBSERVATION）
            raw_payload: 原始数据负载

        Returns:
            标准化后的 AlternativeDataSignal

        Raises:
            NormalizerError: 实体映射失败
            AlternativeDataError: 信号 tier 为 FACT
        """
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise NormalizerError("signal_id 不能为空")
        if not isinstance(metric_name, str) or not metric_name.strip():
            raise NormalizerError("metric_name 不能为空")
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise NormalizerError("timestamp 不能为空")
        signal_id = signal_id.strip()
        metric_name = metric_name.strip()
        timestamp = timestamp.strip()
        try:
            metric_value = float(metric_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise NormalizerError("metric_value 必须为有限数") from exc
        if not isfinite(metric_value):
            raise NormalizerError("metric_value 必须为有限数")
        if isinstance(tier, str):
            try:
                tier = SignalTier(tier)
            except ValueError as exc:
                raise NormalizerError(f"不支持的信号层级：{tier}") from exc
        if not isinstance(tier, SignalTier):
            raise NormalizerError("tier 必须为 SignalTier")
        # 实体映射
        entity_id = self._entity_mapper.map_entity(raw_entity_id)
        # 构建信号
        signal = AlternativeDataSignal(
            signal_id=signal_id,
            source_id=self._source.source_id,
            entity_id=entity_id,
            metric_name=metric_name,
            metric_value=metric_value,
            timestamp=timestamp,
            tier=tier,
            raw_payload=dict(raw_payload) if isinstance(raw_payload, Mapping) else {},
        )
        # 硬规则校验：另类信号不得直接升级为 FACT
        assert_signal_not_fact(signal)
        return signal

    def detect_coverage_change(
        self, current_entities: list[str], detected_at: str = ""
    ) -> list[CoverageChange]:
        """检测数据源覆盖范围变化。

        按 P3.3 硬规则「覆盖变化不会被误判为公司经营变化」：
        - 生成 CoverageChange 标记
        - 明确声明不代表公司经营变化

        Args:
            current_entities: 当前覆盖的实体 ID 列表
            detected_at: 检测时间

        Returns:
            CoverageChange 列表
        """
        return self._coverage_detector.update_coverage(
            current_entities, detected_at
        )
