"""P2.9 发现评估器。

按 P2 计划 §301-323 Task P2.9：
- 变化事件 → 有效事实转化率
- 来源命中率与单位成本
- 行业与市场环境稳定性

设计要点：
- evaluate_event_conversion(): 统计变化事件 → 有效事实的转化率
- evaluate_source_metrics(): 单个来源的命中率与单位成本
- evaluate_sources_batch(): 批量评估多来源
- 不评估股价涨跌（仅评估基本面事件转化）
"""
from __future__ import annotations

import math
from typing import Any

from .contracts import (
    EVENT_STATUS_EVIDENCE_SUPPORTED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_MODEL_ELIGIBLE,
    EVENT_STATUS_OBSERVED,
    EVENT_STATUS_REJECTED,
    EVENT_STATUSES,
    EventConversionStat,
    SourceMetrics,
    utc_now,
)


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

ERR_INVALID_EVENT_STATUS: str = "invalid_event_status"
ERR_INVALID_SOURCE_ID: str = "invalid_source_id"
ERR_INVALID_QUERY_COUNT: str = "invalid_query_count"


# ---------------------------------------------------------------------------
# 发现评估器
# ---------------------------------------------------------------------------


class DiscoveryEvaluator:
    """发现评估器。

    评估变化事件 → 有效事实转化率，以及来源命中率与单位成本。
    """

    def evaluate_event_conversion(
        self,
        *,
        events: list[Any],
        as_of: str = "",
        period: str = "",
    ) -> EventConversionStat:
        """评估变化事件 → 有效事实转化率。

        有效事实 = status in {evidence_supported, model_eligible}
        （observed 仅观察未验证；rejected 被拒；expired 过期）

        Args:
            events: 事件列表（每个事件含 status 字段）
            as_of: PIT 时点
            period: 统计期间

        Returns:
            EventConversionStat

        Raises:
            ValueError: 事件含非法 status
        """
        if not as_of:
            as_of = utc_now()

        if not isinstance(events, list):
            events = []
        total = len(events)
        effective = 0
        rejected = 0
        expired = 0

        for ev in events:
            status = self._get_event_status(ev)
            if status not in EVENT_STATUSES:
                raise ValueError(
                    f"非法事件 status：{status}（合法：{', '.join(EVENT_STATUSES)}）"
                )
            if status in (EVENT_STATUS_EVIDENCE_SUPPORTED, EVENT_STATUS_MODEL_ELIGIBLE):
                effective += 1
            elif status == EVENT_STATUS_REJECTED:
                rejected += 1
            elif status == EVENT_STATUS_EXPIRED:
                expired += 1

        conversion_rate = effective / total if total > 0 else 0.0

        return EventConversionStat(
            total_events=total,
            effective_facts=effective,
            rejected_events=rejected,
            expired_events=expired,
            conversion_rate=conversion_rate,
            as_of=as_of,
            period=period,
        )

    def evaluate_source_metrics(
        self,
        *,
        source_id: str,
        hit_count: int,
        total_queries: int,
        total_cost: float = 0.0,
        as_of: str = "",
    ) -> SourceMetrics:
        """评估单一来源的命中率与单位成本。

        Args:
            source_id: 来源 ID
            hit_count: 命中数（被采用为有效证据）
            total_queries: 总查询数
            total_cost: 总成本
            as_of: PIT 时点

        Returns:
            SourceMetrics

        Raises:
            ValueError: source_id 为空 / total_queries < 0 / hit_count < 0 / hit > queries
        """
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id 必填")
        try:
            hit_count = int(hit_count)
            total_queries = int(total_queries)
            total_cost = float(total_cost)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("source metrics must be numeric") from None
        if not math.isfinite(total_cost):
            raise ValueError("total_cost must be finite")
        if total_queries < 0:
            raise ValueError(f"total_queries 不能为负：{total_queries}")
        if hit_count < 0:
            raise ValueError(f"hit_count 不能为负：{hit_count}")
        if hit_count > total_queries:
            raise ValueError(
                f"hit_count({hit_count}) > total_queries({total_queries})"
            )

        if not as_of:
            as_of = utc_now()

        unit_cost = total_cost / total_queries if total_queries > 0 else 0.0
        hit_rate = hit_count / total_queries if total_queries > 0 else 0.0

        return SourceMetrics(
            source_id=source_id,
            hit_count=hit_count,
            total_queries=total_queries,
            total_cost=total_cost,
            unit_cost=unit_cost,
            hit_rate=hit_rate,
            as_of=as_of,
        )

    def evaluate_sources_batch(
        self,
        sources: list[dict[str, Any]],
        as_of: str = "",
    ) -> list[SourceMetrics]:
        """批量评估多来源。

        Args:
            sources: 字典列表，每个含 source_id / hit_count / total_queries / total_cost
            as_of: PIT 时点

        Returns:
            SourceMetrics 列表
        """
        if not as_of:
            as_of = utc_now()
        if not isinstance(sources, list):
            return []
        result: list[SourceMetrics] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            try:
                result.append(
                    self.evaluate_source_metrics(
                        source_id=source.get("source_id", ""),
                        hit_count=source.get("hit_count", 0),
                        total_queries=source.get("total_queries", 0),
                        total_cost=source.get("total_cost", 0.0),
                        as_of=as_of,
                    )
                )
            except ValueError:
                continue
        return result

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _get_event_status(event: Any) -> str:
        """从事件对象获取 status 字段（支持 dataclass / dict / object）。"""
        if isinstance(event, dict):
            return str(event.get("status", ""))
        return str(getattr(event, "status", ""))


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_discovery_evaluator() -> DiscoveryEvaluator:
    """构造默认 DiscoveryEvaluator。"""
    return DiscoveryEvaluator()


__all__ = [
    "ERR_INVALID_EVENT_STATUS",
    "ERR_INVALID_SOURCE_ID",
    "ERR_INVALID_QUERY_COUNT",
    "DiscoveryEvaluator",
    "build_discovery_evaluator",
]
