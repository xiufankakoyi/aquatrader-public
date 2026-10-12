"""Path-based Alpha Discovery 的路径级数据模型。

本模块定义 Graph-first Alpha Discovery 主线的核心路径对象，
替代过去用裸 tuple / list 传递路径的方式，使路径发现、剪枝、
证据绑定、LLM 验证、Claim 晋升形成统一闭环。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value or "").strip()


def _score(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, min(1.0, number)) if math.isfinite(number) else 0.0


def _count(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text:
            result.append(text)
    return result


def _edges(value: Any) -> list[list[str]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [list(item) for item in value if isinstance(item, (list, tuple)) and len(item) == 3]


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _serialized(value: Any) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            result.append(dict(item))
            continue
        serializer = getattr(item, "to_dict", None)
        if callable(serializer):
            try:
                encoded = serializer()
            except (TypeError, ValueError, AttributeError):
                continue
            if isinstance(encoded, Mapping):
                result.append(dict(encoded))
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False



@dataclass
class PathCandidate:
    """一条待评估的结构路径，不包含完整证据结论。

    Attributes:
        path_id: 路径唯一标识。
        nodes: 路径上的节点名称列表（含 root 与 leaf）。
        edges: 路径上的边键 (head, relation, tail) 列表，长度比 nodes 少 1。
        relations: 路径上的关系类型列表。
        axes: 路径覆盖的结构轴集合。
        root_entity: 路径根实体。
        target_entity: 路径目标实体（通常为 leaf）。
        hop_count: 跳数（edges 长度）。
        path_type: 路径类型，例如 supplier_dependency_path / material_bottleneck_path。
        generated_by: 生成器标识，如 structure_navigator / event_boosted。
        base_score: 生成阶段的基础结构分 [0, 1]。
        node_types: 各节点类型列表（与 nodes 对齐）。
        meta_path: 匹配的 meta-path 名称，若无则为空。
    """

    path_id: str
    nodes: list[str] = field(default_factory=list)
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    axes: list[str] = field(default_factory=list)
    root_entity: str = ""
    target_entity: str = ""
    hop_count: int = 0
    path_type: str = "unknown"
    generated_by: str = "structure_navigator"
    base_score: float = 0.0
    node_types: list[str] = field(default_factory=list)
    meta_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path_id": _text(self.path_id),
            "nodes": _strings(self.nodes),
            "edges": _edges(self.edges),
            "relations": _strings(self.relations),
            "axes": _strings(self.axes),
            "root_entity": _text(self.root_entity),
            "target_entity": _text(self.target_entity),
            "hop_count": _count(self.hop_count),
            "path_type": _text(self.path_type) or "unknown",
            "generated_by": _text(self.generated_by),
            "base_score": _score(self.base_score),
            "node_types": _strings(self.node_types),
            "meta_path": _text(self.meta_path),
        }


@dataclass
class PathEvidenceBundle:
    """一条路径绑定到的全部证据包。

    Attributes:
        path_candidate: 对应的路径候选。
        stable_claims: 路径上稳定图的 Claim 对象列表。
        event_claims: 路径相关的事件图 EventClaim 列表。
        evidence_chunks: 原始证据片段列表，每项为 dict。
        source_summary: 来源类型汇总 {source_type: count}。
        missing_evidence: 缺失证据描述列表。
        contradiction_signals: 检测到的潜在冲突信号列表。
        coverage_score: 证据覆盖率 [0, 1]。
    """

    path_candidate: PathCandidate = field(default_factory=lambda: PathCandidate(path_id=""))
    stable_claims: list[Any] = field(default_factory=list)
    event_claims: list[Any] = field(default_factory=list)
    evidence_chunks: list[dict[str, Any]] = field(default_factory=list)
    source_summary: dict[str, int] = field(default_factory=dict)
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)
    contradiction_signals: list[dict[str, Any]] = field(default_factory=list)
    coverage_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path_candidate": self.path_candidate.to_dict() if isinstance(self.path_candidate, PathCandidate) else {},
            "stable_claims": _serialized(self.stable_claims),
            "event_claims": _serialized(self.event_claims),
            "evidence_chunks": _dicts(self.evidence_chunks),
            "source_summary": _mapping(self.source_summary),
            "missing_evidence": _dicts(self.missing_evidence),
            "contradiction_signals": _dicts(self.contradiction_signals),
            "coverage_score": _score(self.coverage_score),
        }


@dataclass
class PathVerificationResult:
    """LLM 对路径的结构化验证结果。

    Attributes:
        path_id: 路径唯一标识。
        is_valid: 路径是否被验证为有效。
        validity_score: 有效性得分 [0, 1]。
        evidence_strength: 证据强度 [0, 1]。
        bottleneck_type: 瓶颈类型，如 import_dependency / capacity / certification。
        supported_edges: 被支持的边键列表。
        weak_edges: 证据弱的边键列表。
        unsupported_edges: 不被支持的边键列表。
        missing_evidence: 缺失证据列表。
        contradictions: 检测到的冲突列表。
        verifier_notes: 验证器简要说明。
    """

    path_id: str
    is_valid: bool = False
    validity_score: float = 0.0
    evidence_strength: float = 0.0
    bottleneck_type: str = "unknown"
    supported_edges: list[tuple[str, str, str]] = field(default_factory=list)
    weak_edges: list[tuple[str, str, str]] = field(default_factory=list)
    unsupported_edges: list[tuple[str, str, str]] = field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    verifier_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path_id": _text(self.path_id),
            "is_valid": _bool(self.is_valid),
            "validity_score": _score(self.validity_score),
            "evidence_strength": _score(self.evidence_strength),
            "bottleneck_type": _text(self.bottleneck_type) or "unknown",
            "supported_edges": _edges(self.supported_edges),
            "weak_edges": _edges(self.weak_edges),
            "unsupported_edges": _edges(self.unsupported_edges),
            "missing_evidence": _dicts(self.missing_evidence),
            "contradictions": _dicts(self.contradictions),
            "verifier_notes": _text(self.verifier_notes),
        }


@dataclass
class PromotionDecision:
    """路径级晋升决策。

    Attributes:
        path_id: 路径唯一标识。
        promotable_claims: 允许晋升的 claim ID 列表。
        rejected_claims: 被拒绝的 claim ID 列表。
        downgrade_claims: 被降级的 claim ID 列表。
        required_evidence: 仍需补充的证据列表。
        ontology_check_result: 本体检查结论 dict。
        evidence_check_result: 证据检查结论 dict。
        contradiction_check_result: 冲突检查结论 dict。
        decision_reason: 决策原因说明。
    """

    path_id: str
    promotable_claims: list[str] = field(default_factory=list)
    rejected_claims: list[str] = field(default_factory=list)
    downgrade_claims: list[str] = field(default_factory=list)
    required_evidence: list[dict[str, Any]] = field(default_factory=list)
    ontology_check_result: dict[str, Any] = field(default_factory=dict)
    evidence_check_result: dict[str, Any] = field(default_factory=dict)
    contradiction_check_result: dict[str, Any] = field(default_factory=dict)
    decision_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path_id": _text(self.path_id),
            "promotable_claims": _strings(self.promotable_claims),
            "rejected_claims": _strings(self.rejected_claims),
            "downgrade_claims": _strings(self.downgrade_claims),
            "required_evidence": _dicts(self.required_evidence),
            "ontology_check_result": _mapping(self.ontology_check_result),
            "evidence_check_result": _mapping(self.evidence_check_result),
            "contradiction_check_result": _mapping(self.contradiction_check_result),
            "decision_reason": _text(self.decision_reason),
        }


@dataclass
class PathQualityReport:
    """统一路径质量报告。

    Attributes:
        path_id: 路径唯一标识。
        structural_score: 结构符合 ontology / meta-path 程度 [0, 1]。
        evidence_score: 证据覆盖关键 edges 程度 [0, 1]。
        source_score: 来源可靠性 [0, 1]。
        consistency_score: 无冲突程度 [0, 1]。
        freshness_score: 事件新鲜度 [0, 1]。
        bottleneck_score: 表达卡脖子 / 不可替代 / 扩产难的程度 [0, 1]。
        diversity_score: 提供新结构视角的程度 [0, 1]。
        final_quality_score: 最终综合质量分 [0, 1]。
        quality_label: 质量标签，如 STRONG_ALPHA_PATH / PROBE_FIRST_PATH。
    """

    path_id: str
    structural_score: float = 0.0
    evidence_score: float = 0.0
    source_score: float = 0.0
    consistency_score: float = 0.0
    freshness_score: float = 0.0
    bottleneck_score: float = 0.0
    diversity_score: float = 0.0
    final_quality_score: float = 0.0
    quality_label: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path_id": _text(self.path_id),
            "structural_score": _score(self.structural_score),
            "evidence_score": _score(self.evidence_score),
            "source_score": _score(self.source_score),
            "consistency_score": _score(self.consistency_score),
            "freshness_score": _score(self.freshness_score),
            "bottleneck_score": _score(self.bottleneck_score),
            "diversity_score": _score(self.diversity_score),
            "final_quality_score": _score(self.final_quality_score),
            "quality_label": _text(self.quality_label) or "UNKNOWN",
        }


@dataclass
class AlphaPathResult:
    """AlphaDiscoveryEngine 对单条路径的最终输出。

    Attributes:
        path_id: 路径唯一标识。
        path: 节点列表。
        path_type: 路径类型。
        stable_score: 稳定图结构分 [0, 1]。
        event_score: 事件信号分 [0, 1]。
        evidence_score: 证据分 [0, 1]。
        verification_score: LLM 验证分 [0, 1]。
        contradiction_score: 冲突风险分 [0, 1]。
        final_score: 最终综合分 [0, 1]。
        evidence_chunks: 原始证据片段。
        promoted_claims: 晋升的 claim ID 列表。
        rejected_claims: 拒绝的 claim ID 列表。
        missing_evidence: 缺失证据列表。
        next_probe_tasks: 下一步探测任务列表。
        explanation: 可解释文本。
        quality_label: 质量标签。
    """

    path_id: str
    path: list[str] = field(default_factory=list)
    path_type: str = "unknown"
    stable_score: float = 0.0
    event_score: float = 0.0
    evidence_score: float = 0.0
    verification_score: float = 0.0
    contradiction_score: float = 0.0
    final_score: float = 0.0
    evidence_chunks: list[dict[str, Any]] = field(default_factory=list)
    promoted_claims: list[str] = field(default_factory=list)
    rejected_claims: list[str] = field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)
    next_probe_tasks: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    quality_label: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path_id": _text(self.path_id),
            "path": _strings(self.path),
            "path_type": _text(self.path_type) or "unknown",
            "stable_score": _score(self.stable_score),
            "event_score": _score(self.event_score),
            "evidence_score": _score(self.evidence_score),
            "verification_score": _score(self.verification_score),
            "contradiction_score": _score(self.contradiction_score),
            "final_score": _score(self.final_score),
            "evidence_chunks": _dicts(self.evidence_chunks),
            "promoted_claims": _strings(self.promoted_claims),
            "rejected_claims": _strings(self.rejected_claims),
            "missing_evidence": _dicts(self.missing_evidence),
            "next_probe_tasks": _dicts(self.next_probe_tasks),
            "explanation": _text(self.explanation),
            "quality_label": _text(self.quality_label) or "UNKNOWN",
        }
