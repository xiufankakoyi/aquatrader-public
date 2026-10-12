"""假设-证据覆盖矩阵与置信度校准。

解决六个痛点中的前两个:
  1. 元认知:知道缺哪几块证据,下一步该搜什么
  2. 置信度校准:量化整体置信度并分档

核心对象:
  - EvidenceGap:单个突触/神经元上的证据缺口描述
  - ConfidenceScore:置信度评分,含 raw_score 与 bucket
  - GapAnalyzer:扫描超图,输出缺口列表与置信度更新

不引入外部 ML 库,全部用规则和统计。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from core.research_graph.schema import EvidenceLevel


EVIDENCE_LEVEL_QUALITY = {
    EvidenceLevel.PROVEN.value: 1.0,
    EvidenceLevel.LIKELY.value: 0.75,
    EvidenceLevel.SPECULATIVE.value: 0.35,
    EvidenceLevel.CONFLICTED.value: 0.15,
    EvidenceLevel.DISPROVEN.value: 0.0,
}


def get_evidence_stance(ev: Any) -> str:
    """Return canonical stance for Evidence-like objects."""
    metadata = getattr(ev, "metadata", {}) or {}
    stance = str(metadata.get("stance") or getattr(ev, "stance", "") or "").lower()
    if stance in {"support", "counter"}:
        return stance
    if getattr(ev, "contradicts", None):
        return "counter"
    return "support"


def get_evidence_source_type(ev: Any) -> str:
    """Return source_type with canonical Evidence priority."""
    return str(
        getattr(ev, "source_type", "")
        or getattr(ev, "provider_type", "")
        or "unknown"
    )


def get_evidence_quality(ev: Any, source_quality: dict[str, float] | None = None) -> float:
    """Combine canonical evidence_level and source authority."""
    source_quality = source_quality or {}
    source_type = get_evidence_source_type(ev)
    source_score = source_quality.get(source_type, source_quality.get("unknown", 0.5))
    legacy_grade = float(getattr(ev, "grade", 0.0) or 0.0)
    level = str(getattr(ev, "evidence_level", EvidenceLevel.SPECULATIVE.value) or "")
    level_score = EVIDENCE_LEVEL_QUALITY.get(level, EVIDENCE_LEVEL_QUALITY[EvidenceLevel.SPECULATIVE.value])
    return max(source_score, legacy_grade, level_score)


def get_evidence_time(ev: Any) -> str:
    """Return best available timestamp for canonical Evidence-like objects."""
    metadata = getattr(ev, "metadata", {}) or {}
    return str(
        getattr(ev, "publish_date", "")
        or metadata.get("retrieved_at", "")
        or getattr(ev, "retrieved_at", "")
        or getattr(ev, "timestamp", "")
        or ""
    )


@dataclass
class EvidenceGap:
    """单个知识缺口。"""

    target_id: str  # neuron_id 或 synapse_id
    target_type: str  # "neuron" / "synapse"
    dimension: str  # 缺口维度,如"support"/"counter"/"source_diversity"
    severity: float  # 0.0-1.0,越接近 1 越严重
    reason: str = ""
    suggested_query: str = ""  # 建议搜索查询
    expected_gain: float = 0.0  # 补上后预期置信度提升


@dataclass
class ConfidenceScore:
    """置信度评分结果。"""

    raw_score: float  # 0.0-1.0
    bucket: str  # STRONG / WEAK_DATA / DISPUTED / SPECULATIVE
    support_quality: float = 0.0
    source_diversity: float = 0.0
    consistency: float = 0.0
    freshness: float = 0.0
    uncertainty_sources: list[str] = field(default_factory=list)


class GapAnalyzer:
    """扫描神经元/突触,量化证据缺口与置信度。"""

    # 每种 evidence source 的基础质量分
    _SOURCE_QUALITY: dict[str, float] = {
        "announcement": 1.0,
        "financial_report": 1.0,
        "official": 0.9,
        "news": 0.7,
        "crawler": 0.6,
        "api": 0.5,
        "blog": 0.4,
        "forum": 0.3,
        "local_cache": 0.8,
        "unknown": 0.5,
    }

    # 置信度档位阈值
    _BUCKET_STRONG = 0.75
    _BUCKET_WEAK = 0.45

    def __init__(self, max_gap_severity: float = 1.0) -> None:
        """初始化。

        Args:
            max_gap_severity: 缺口严重度的上限归一化值。
        """
        self._max_gap_severity = max_gap_severity

    def analyze_neuron(
        self,
        neuron: Any,
        evidence_list: list[Any],
        now_iso: str = "",
    ) -> tuple[ConfidenceScore, list[EvidenceGap]]:
        """分析单个神经元的证据覆盖与置信度。

        Args:
            neuron: ResearchNeuron 对象。
            evidence_list: 关联的 Evidence 列表。
            now_iso: 当前时间 ISO 字符串,用于 freshness 计算。

        Returns:
            (confidence_score, gaps) 元组。
        """
        if not evidence_list:
            gaps = [
                EvidenceGap(
                    target_id=neuron.neuron_id,
                    target_type="neuron",
                    dimension="support",
                    severity=1.0,
                    reason="无任何证据",
                    suggested_query=f"{neuron.canonical_name} 供应商/产能/客户",
                    expected_gain=0.4,
                )
            ]
            return (
                ConfidenceScore(
                    raw_score=0.0,
                    bucket="SPECULATIVE",
                    uncertainty_sources=["无证据"],
                ),
                gaps,
            )

        support_evs = [e for e in evidence_list if get_evidence_stance(e) == "support"]
        counter_evs = [e for e in evidence_list if get_evidence_stance(e) == "counter"]

        support_quality = self._avg_evidence_quality(support_evs)
        source_diversity = self._source_diversity_score(evidence_list)
        consistency = self._consistency_score(support_evs, counter_evs)
        freshness = self._freshness_score(evidence_list, now_iso)

        # 整体置信度:证据质量 * 来源多样性 * 一致性 * 新鲜度
        raw = (
            support_quality * 0.4
            + source_diversity * 0.2
            + consistency * 0.25
            + freshness * 0.15
        )
        raw = max(0.0, min(1.0, raw))

        bucket = self._bucket(raw, support_evs, counter_evs)
        gaps = self._find_gaps(
            target_id=neuron.neuron_id,
            target_type="neuron",
            support_evs=support_evs,
            counter_evs=counter_evs,
            support_quality=support_quality,
            source_diversity=source_diversity,
            freshness=freshness,
            canonical_name=neuron.canonical_name,
        )

        uncertainty_sources = self._uncertainty_sources(
            support_quality, source_diversity, consistency, freshness, support_evs, counter_evs
        )

        return ConfidenceScore(
            raw_score=round(raw, 4),
            bucket=bucket,
            support_quality=round(support_quality, 4),
            source_diversity=round(source_diversity, 4),
            consistency=round(consistency, 4),
            freshness=round(freshness, 4),
            uncertainty_sources=uncertainty_sources,
        ), gaps

    def _avg_evidence_quality(self, evidence_list: list[Any]) -> float:
        """计算证据列表的平均质量。"""
        if not evidence_list:
            return 0.0
        total = 0.0
        for ev in evidence_list:
            total += get_evidence_quality(ev, self._SOURCE_QUALITY)
        return total / len(evidence_list)

    def _source_diversity_score(self, evidence_list: list[Any]) -> float:
        """来源多样性:不同 source_type 数量 / 理想数量(4)。"""
        if not evidence_list:
            return 0.0
        types = {get_evidence_source_type(ev) for ev in evidence_list}
        return min(1.0, len(types) / 4.0)

    def _consistency_score(
        self, support_evs: list[Any], counter_evs: list[Any]
    ) -> float:
        """一致性:支持证据越多、反证越少越一致。"""
        support = len(support_evs)
        counter = len(counter_evs)
        total = support + counter
        if total == 0:
            return 0.0
        # 有反证时按反证比例惩罚,但不能直接归零(保留少量反证是正常的)
        ratio = counter / total
        return max(0.0, 1.0 - ratio * 2.0)

    def _freshness_score(self, evidence_list: list[Any], now_iso: str) -> float:
        """新鲜度:最新证据距今越近越高。"""
        if not evidence_list:
            return 0.0
        if not now_iso:
            return 0.5
        try:
            from datetime import datetime, timezone

            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            newest = None
            for ev in evidence_list:
                ts = get_evidence_time(ev)
                if not ts:
                    continue
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if newest is None or dt > newest:
                    newest = dt
            if newest is None:
                return 0.5
            age_days = (now - newest).total_seconds() / 86400.0
            return math.exp(-age_days / 7.0)
        except Exception:
            return 0.5

    def _bucket(
        self, raw: float, support_evs: list[Any], counter_evs: list[Any]
    ) -> str:
        """根据 raw score 和反证情况分档。"""
        if raw >= self._BUCKET_STRONG and len(counter_evs) == 0:
            return "STRONG"  # 强共识有数据
        if raw >= self._BUCKET_STRONG and len(counter_evs) > 0:
            return "DISPUTED"  # 争议性结论
        if raw >= self._BUCKET_WEAK:
            return "WEAK_DATA"  # 弱共识缺数据
        return "SPECULATIVE"  # 纯猜测

    def _find_gaps(
        self,
        target_id: str,
        target_type: str,
        support_evs: list[Any],
        counter_evs: list[Any],
        support_quality: float,
        source_diversity: float,
        freshness: float,
        canonical_name: str,
    ) -> list[EvidenceGap]:
        """找出具体知识缺口。"""
        gaps: list[EvidenceGap] = []

        if len(support_evs) < 2:
            gaps.append(
                EvidenceGap(
                    target_id=target_id,
                    target_type=target_type,
                    dimension="support",
                    severity=0.8,
                    reason="支持证据数量不足(<2)",
                    suggested_query=f"{canonical_name} 产能/客户/供应商",
                    expected_gain=0.25,
                )
            )

        if support_quality < 0.6:
            gaps.append(
                EvidenceGap(
                    target_id=target_id,
                    target_type=target_type,
                    dimension="source_quality",
                    severity=0.7,
                    reason="支持证据来源权威性不足",
                    suggested_query=f"{canonical_name} 公告 年报",
                    expected_gain=0.2,
                )
            )

        if source_diversity < 0.5:
            gaps.append(
                EvidenceGap(
                    target_id=target_id,
                    target_type=target_type,
                    dimension="source_diversity",
                    severity=0.6,
                    reason="来源类型单一,需要交叉验证",
                    suggested_query=f"{canonical_name} 新闻 研报",
                    expected_gain=0.15,
                )
            )

        if len(counter_evs) == 0 and len(support_evs) >= 2:
            gaps.append(
                EvidenceGap(
                    target_id=target_id,
                    target_type=target_type,
                    dimension="counter",
                    severity=0.5,
                    reason="缺少反证检查,结论可能过于乐观",
                    suggested_query=f"{canonical_name} 风险 不及预期 澄清",
                    expected_gain=0.1,
                )
            )

        if freshness < 0.5:
            gaps.append(
                EvidenceGap(
                    target_id=target_id,
                    target_type=target_type,
                    dimension="freshness",
                    severity=0.5,
                    reason="最新证据较旧",
                    suggested_query=f"{canonical_name} 最新 进展",
                    expected_gain=0.1,
                )
            )

        return gaps

    def _uncertainty_sources(
        self,
        support_quality: float,
        source_diversity: float,
        consistency: float,
        freshness: float,
        support_evs: list[Any],
        counter_evs: list[Any],
    ) -> list[str]:
        """拆解不确定性来源。"""
        sources: list[str] = []
        if len(support_evs) < 2:
            sources.append("数据不足")
        if support_quality < 0.6:
            sources.append("来源质量低")
        if source_diversity < 0.5:
            sources.append("缺乏交叉验证")
        if consistency < 0.7:
            sources.append("证据存在矛盾")
        if freshness < 0.5:
            sources.append("信息过时")
        if len(counter_evs) > len(support_evs):
            sources.append("反证多于支持")
        if not sources:
            sources.append("证据充分")
        return sources

    def find_top_gaps(
        self,
        neurons: list[Any],
        evidence_map: dict[str, list[Any]],
        top_k: int = 5,
        now_iso: str = "",
    ) -> list[EvidenceGap]:
        """扫描所有神经元,返回最严重的 top_k 个缺口。

        Args:
            neurons: ResearchNeuron 列表。
            evidence_map: neuron_id -> Evidence 列表。
            top_k: 返回缺口数量。
            now_iso: 当前时间 ISO 字符串。

        Returns:
            EvidenceGap 列表,按 severity 排序。
        """
        all_gaps: list[EvidenceGap] = []
        for neuron in neurons:
            _, gaps = self.analyze_neuron(
                neuron, evidence_map.get(neuron.neuron_id, []), now_iso
            )
            all_gaps.extend(gaps)
        all_gaps.sort(key=lambda g: g.severity, reverse=True)
        return all_gaps[:top_k]
