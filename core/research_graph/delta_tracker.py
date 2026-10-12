"""边际变化跟踪。

解决痛点 4:
  - 哪些新闻是新信息,哪些只是重复旧闻?
  - 新证据让置信度变化了多少?
  - 置信度变化超过阈值时触发重新审查

实现:
  - 每次 tick 保存快照:neuron confidence + 关联 evidence 集合
  - 下一次 tick 对比,输出 delta 报告
  - 对显著变化(置信度 delta 绝对值 > threshold 或 bucket 跃迁)生成
    REQUEST_HUMAN_REVIEW pulse
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NeuronDelta:
    """单个神经元的边际变化。"""

    neuron_id: str
    canonical_name: str
    confidence_before: float
    confidence_after: float
    confidence_delta: float
    bucket_before: str
    bucket_after: str
    new_evidence_ids: list[str] = field(default_factory=list)
    removed_evidence_ids: list[str] = field(default_factory=list)


@dataclass
class DeltaReport:
    """整个超图的边际变化报告。"""

    tick: int
    significant_deltas: list[NeuronDelta] = field(default_factory=list)
    review_triggered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化 dict。"""
        return {
            "tick": self.tick,
            "significant_deltas": [
                {
                    "neuron_id": d.neuron_id,
                    "canonical_name": d.canonical_name,
                    "confidence_before": d.confidence_before,
                    "confidence_after": d.confidence_after,
                    "confidence_delta": d.confidence_delta,
                    "bucket_before": d.bucket_before,
                    "bucket_after": d.bucket_after,
                    "new_evidence_ids": list(d.new_evidence_ids),
                    "removed_evidence_ids": list(d.removed_evidence_ids),
                }
                for d in self.significant_deltas
            ],
            "review_triggered": list(self.review_triggered),
        }


class DeltaTracker:
    """置信度与证据集合的快照跟踪器。"""

    def __init__(
        self,
        confidence_threshold: float = 0.1,
        bucket_change_triggers_review: bool = True,
    ) -> None:
        """初始化。

        Args:
            confidence_threshold: 置信度变化绝对值超过该阈值视为显著变化。
            bucket_change_triggers_review: 档位跃迁是否触发人工审查。
        """
        self._confidence_threshold = confidence_threshold
        self._bucket_change_triggers_review = bucket_change_triggers_review
        self._last_confidence: dict[str, float] = {}
        self._last_bucket: dict[str, str] = {}
        self._last_evidence: dict[str, set[str]] = {}

    def update(
        self,
        tick: int,
        neurons: list[Any],
        evidence_map: dict[str, list[Any]],
    ) -> DeltaReport:
        """对比当前状态与上一次快照,返回变化报告。

        Args:
            tick: 当前 tick 数。
            neurons: ResearchNeuron 列表。
            evidence_map: neuron_id -> Evidence 列表。

        Returns:
            DeltaReport 边际变化报告。
        """
        report = DeltaReport(tick=tick)
        current_ids: set[str] = set()

        for neuron in neurons:
            nid = neuron.neuron_id
            current_ids.add(nid)
            current_conf = float(getattr(neuron, "confidence", 0.0))
            current_bucket = (getattr(neuron, "source_node_ref", {}) or {}).get(
                "confidence_bucket", "SPECULATIVE"
            )
            current_ev_ids = {
                getattr(ev, "evidence_id", "") for ev in evidence_map.get(nid, [])
            }
            current_ev_ids.discard("")

            prev_conf = self._last_confidence.get(nid)
            prev_bucket = self._last_bucket.get(nid)
            prev_ev_ids = self._last_evidence.get(nid, set())

            if prev_conf is None:
                # 首次记录,只保存快照,不报告变化
                self._last_confidence[nid] = current_conf
                self._last_bucket[nid] = current_bucket
                self._last_evidence[nid] = current_ev_ids
                continue

            delta = current_conf - prev_conf
            new_ev = list(current_ev_ids - prev_ev_ids)
            removed_ev = list(prev_ev_ids - current_ev_ids)
            bucket_changed = prev_bucket != current_bucket

            significant = (
                abs(delta) >= self._confidence_threshold
                or len(new_ev) > 0
                or (bucket_changed and self._bucket_change_triggers_review)
            )

            if significant:
                report.significant_deltas.append(
                    NeuronDelta(
                        neuron_id=nid,
                        canonical_name=neuron.canonical_name,
                        confidence_before=prev_conf,
                        confidence_after=current_conf,
                        confidence_delta=round(delta, 4),
                        bucket_before=prev_bucket or "UNKNOWN",
                        bucket_after=current_bucket,
                        new_evidence_ids=new_ev,
                        removed_evidence_ids=removed_ev,
                    )
                )

                if abs(delta) >= self._confidence_threshold or bucket_changed:
                    report.review_triggered.append(nid)

            self._last_confidence[nid] = current_conf
            self._last_bucket[nid] = current_bucket
            self._last_evidence[nid] = current_ev_ids

        # 清理已删除神经元
        for nid in list(self._last_confidence.keys()):
            if nid not in current_ids:
                self._last_confidence.pop(nid, None)
                self._last_bucket.pop(nid, None)
                self._last_evidence.pop(nid, None)

        return report
