"""TemporalKernel：基于核函数和Hawkes过程的时间衰减与事件传播。

替代原有的硬编码分段衰减（7天/30天/90天硬切），提供：
1. 指数核衰减：不同来源类型有不同半衰期
2. Hawkes自激过程：事件密集到达时产生叠加效应
3. 平滑时间加权：避免"31天前vs29天前"的断崖式差异
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.research.perilla.alpha.weight_registry import WeightRegistry

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _normalize_datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@dataclass
class EventImpact:
    """单个事件的影响值。"""
    source_type: str
    event_time: datetime
    base_confidence: float
    is_boost: bool = True  # True=利好, False=风险
    event_category: str = "general"
    decay_half_life_days: float = 30.0


class TemporalKernel:
    """时间核函数与事件衰减计算器。"""

    def __init__(self, registry: WeightRegistry | None = None) -> None:
        self.registry = registry or WeightRegistry.get_instance()
        config = self.registry.get_temporal_config()
        config = config if isinstance(config, Mapping) else {}
        self._half_lives = config.get("half_lives", {})
        if not isinstance(self._half_lives, Mapping):
            self._half_lives = {}
        hawkes_cfg = config.get("hawkes", {})
        if not isinstance(hawkes_cfg, Mapping):
            hawkes_cfg = {}
        self._alpha = max(0.0, _finite_float(hawkes_cfg.get("self_excitation_alpha", 0.1), 0.1))
        self._beta = max(0.0, _finite_float(hawkes_cfg.get("decay_beta", 0.05), 0.05))
        self._max_excitation = max(0.0, _finite_float(hawkes_cfg.get("max_excitation", 0.3), 0.3))

        # 事件时间缓存：node_id -> list[(timestamp_dt, confidence, is_boost)]
        self._node_events: dict[str, list[tuple[datetime, float, bool]]] = {}

    def get_half_life(self, source_type: str) -> float:
        """获取指定来源类型的半衰期。"""
        if not isinstance(source_type, str):
            source_type = "default"
        cfg = self._half_lives.get(source_type) or self._half_lives.get("default", {})
        if not isinstance(cfg, Mapping):
            return 30.0
        return max(0.1, _finite_float(cfg.get("half_life_days", 30.0), 30.0))

    def temporal_weight(
        self,
        event_time: datetime | str,
        source_type: str = "default",
        reference_time: datetime | None = None,
    ) -> float:
        """计算单个事件的时间衰减权重。

        指数核：w = exp(-ln(2) * age_days / half_life)
        - 当天事件: w=1.0
        - 一个半衰期后: w=0.5
        - 三个半衰期后: w=0.125（基本衰减完）

        Args:
            event_time: 事件时间（datetime或ISO字符串）
            source_type: 来源类型
            reference_time: 参考时间，None则用当前时间

        Returns:
            时间权重（0-1）
        """
        ref = _normalize_datetime(reference_time) or datetime.now(timezone.utc).replace(tzinfo=None)
        event_time = _normalize_datetime(event_time)
        if event_time is None:
            return 0.1

        age_days = max(0.0, (ref - event_time).total_seconds() / 86400.0)
        half_life = self.get_half_life(source_type)
        return max(0.0, min(1.0, math.exp(-math.log(2) * age_days / half_life)))

    def compute_aggregate_temporal_score(
        self,
        events: list[EventImpact],
        reference_time: datetime | None = None,
    ) -> dict[str, float]:
        """计算一组事件的聚合时间影响分数。

        使用Noisy-OR聚合多个事件的时间衰减权重，
        并叠加Hawkes自激效应。

        Args:
            events: 事件列表
            reference_time: 参考时间

        Returns:
            {"boost_score": 利好分, "risk_score": 风险分,
             "freshness_score": 新鲜度, "excitation": 自激效应}
        """
        ref = _normalize_datetime(reference_time) or datetime.now(timezone.utc).replace(tzinfo=None)

        boost_weight = 1.0  # Noisy-OR product
        risk_weight = 1.0
        freshness_values: list[float] = []
        event_times_days: list[float] = []

        valid_event_count = 0
        for ev in events if isinstance(events, (list, tuple)) else ():
            if not isinstance(ev, EventImpact):
                continue
            event_time = _normalize_datetime(ev.event_time)
            if event_time is None:
                continue
            valid_event_count += 1
            w = self.temporal_weight(event_time, ev.source_type, ref)
            w = w * max(0.0, min(1.0, _finite_float(ev.base_confidence, 0.0)))
            freshness_values.append(w)
            age_days = max(0.0, (ref - event_time).total_seconds() / 86400.0)
            event_times_days.append(age_days)

            if ev.is_boost:
                boost_weight *= (1.0 - min(0.99, w))
            else:
                risk_weight *= (1.0 - min(0.99, w))

        boost_score = 1.0 - boost_weight
        risk_score = 1.0 - risk_weight
        freshness_score = max(freshness_values) if freshness_values else 0.0

        # Hawkes自激：近7天内事件越多激发越强
        excitation = 0.0
        if event_times_days:
            ref_day = 0.0
            for t_i in event_times_days:
                if t_i <= 30:  # 只考虑30天内的事件
                    excitation += self._alpha * math.exp(-self._beta * (ref_day - (-t_i)))
            excitation = min(self._max_excitation, excitation)

        return {
            "event_boost_score": boost_score,
            "event_risk_score": risk_score,
            "freshness_score": freshness_score,
            "excitation_score": excitation,
            "event_count": valid_event_count,
        }

    def compute_event_overlay_for_path(
        self,
        path_claims: list[dict[str, Any]],
        reference_time: datetime | None = None,
    ) -> dict[str, float]:
        """为路径上的所有claim计算事件overlay。

        Args:
            path_claims: 路径上的claim列表，每个claim需包含
                         source_type, timestamp, confidence, 以及是否为风险事件
            reference_time: 参考时间

        Returns:
            event_overlay字典（兼容frontier_scorer期望的键名）
        """
        events: list[EventImpact] = []
        valid_claims: list[Mapping[str, Any]] = []
        for c in path_claims if isinstance(path_claims, (list, tuple)) else ():
            if not isinstance(c, Mapping):
                continue
            ts = c.get("timestamp", "")
            event_time = _normalize_datetime(ts)
            if event_time is None:
                continue
            valid_claims.append(c)
            source_type = c.get("source_type", "default")
            source_type = source_type if isinstance(source_type, str) else "default"
            conf = max(0.0, min(1.0, _finite_float(c.get("confidence", 0.5), 0.5)))
            is_risk = c.get("is_risk", False) is True or c.get("contradicts", False) is True
            events.append(EventImpact(
                source_type=source_type,
                event_time=event_time,
                base_confidence=conf,
                is_boost=not is_risk,
            ))

        if not events:
            return {
                "event_boost_score": 0.0,
                "event_risk_score": 0.0,
                "freshness_score": 0.0,
                "contradiction_pressure": 0.0,
                "promotion_pressure": 0.0,
            }

        scores = self.compute_aggregate_temporal_score(events, reference_time)
        # 从风险事件中分离矛盾压力
        contradiction_pressure = 0.0
        for c in valid_claims:
            if c.get("contradicts", False) is True:
                w = self.temporal_weight(c.get("timestamp", ""), c.get("source_type", "default"), reference_time)
                contradiction_pressure = max(
                    contradiction_pressure,
                    w * max(0.0, min(1.0, _finite_float(c.get("confidence", 0.5), 0.5))),
                )

        # promotion_pressure：近期高置信度利好密度
        promotion_pressure = scores["excitation_score"] if scores["event_boost_score"] > 0.3 else 0.0

        return {
            "event_boost_score": scores["event_boost_score"],
            "event_risk_score": scores["event_risk_score"],
            "freshness_score": scores["freshness_score"],
            "contradiction_pressure": contradiction_pressure,
            "promotion_pressure": promotion_pressure,
        }


def compute_event_freshness_kernel(
    claim_timestamp: str,
    source_type: str = "default",
    reference_time: datetime | None = None,
) -> float:
    """便捷函数：计算单个claim的新鲜度分数。

    Args:
        claim_timestamp: ISO时间字符串
        source_type: 来源类型
        reference_time: 参考时间

    Returns:
        新鲜度分数（0-1），使用指数核
    """
    kernel = TemporalKernel()
    return kernel.temporal_weight(claim_timestamp, source_type, reference_time)
