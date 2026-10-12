"""SAG (Structured Argument Graph) 知识库。

三层结构：Node / Edge / Claim。
支持添加 claim、合并节点/边、按 source 加权、subgraph 查询、持久化。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    GENERIC_BLACKLIST,
    GENERIC_SUBSTRINGS,
    CLAIM_STATUS_SET,
    ClaimStatus,
    NodeType,
    RelationType,
    StructuralAxis,
    get_source_weight,
    infer_axis,
)
from core.research.perilla.event.event_graph_schema import (
    EVENT_CLAIM_STATUSES,
    EventClaim,
    EventClaimStatus,
    EventEdge,
    EventType,
    PromotionCandidate,
)
from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _bounded_int(value: Any, default: int, maximum: int = 10000) -> int:
    number = _finite_float(value, default)
    return max(0, min(maximum, int(number)))


def _edge_key(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    return (
        _normalize_name(value[0]),
        _text(value[1]),
        _normalize_name(value[2]),
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _decode(factory: Any, payload: Mapping[str, Any]) -> Any | None:
    try:
        return factory(payload)
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class Claim:
    """一条结构化 claim（证据声明），v2 支持状态机、冲突仲裁与 temporal 信息。"""

    claim_id: str
    head: str
    relation: str
    tail: str
    evidence: str
    source_type: str
    source_title: str
    timestamp: str
    confidence: float                    # 原始 extractor_confidence 或旧 confidence
    claim_score: float = 0.0             # 综合评分
    status: str = ClaimStatus.RAW.value
    status_history: list[dict[str, Any]] = field(default_factory=list)
    conflict_status: str = "none"        # none / conflicted / resolved / deprecated
    contradiction_group_id: str | None = None
    contradicts_claim_ids: list[str] = field(default_factory=list)
    superseded_by_claim_id: str | None = None
    contradiction_penalty: float = 0.0
    valid_from: str = ""
    valid_to: str | None = None
    source_id: str = ""                  # 唯一来源标识（URL/公告编号/研报 ID）
    extractor_type: str = "rule"         # llm / rule / seed / migration
    evidence_span: str = ""              # evidence 在原文中的位置/span
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        """兼容旧 claim dict，缺失字段使用默认值。"""
        if not isinstance(data, Mapping):
            raise TypeError("claim payload must be a mapping")
        status = _text(data.get("status"), ClaimStatus.RAW.value)
        if status not in CLAIM_STATUS_SET:
            status = ClaimStatus.RAW.value
        return cls(
            claim_id=_text(data["claim_id"]),
            head=_text(data["head"]),
            relation=_text(data["relation"]),
            tail=_text(data["tail"]),
            evidence=_text(data["evidence"]),
            source_type=_text(data["source_type"]),
            source_title=_text(data["source_title"]),
            timestamp=_text(data["timestamp"]),
            confidence=_finite_float(data.get("confidence")),
            claim_score=_finite_float(data.get("claim_score")),
            status=status,
            status_history=list(data.get("status_history", [])) if isinstance(data.get("status_history", []), (list, tuple)) else [],
            conflict_status=_text(data.get("conflict_status"), "none"),
            contradiction_group_id=_text(data.get("contradiction_group_id")) or None,
            contradicts_claim_ids=_string_list(data.get("contradicts_claim_ids", [])),
            superseded_by_claim_id=_text(data.get("superseded_by_claim_id")) or None,
            contradiction_penalty=_finite_float(data.get("contradiction_penalty")),
            valid_from=_text(data.get("valid_from", data.get("timestamp", ""))),
            valid_to=_text(data.get("valid_to")) or None,
            source_id=_text(data.get("source_id")),
            extractor_type=_text(data.get("extractor_type"), "migration") or "migration",
            evidence_span=_text(data.get("evidence_span")),
            metadata=_mapping(data.get("metadata", {})),
        )


@dataclass
class Node:
    """知识库节点。"""

    name: str
    aliases: list[str] = field(default_factory=list)
    node_type: str = NodeType.GENERIC.value
    industry: str = ""
    tags: list[str] = field(default_factory=list)
    source_count: int = 0
    last_updated: str = ""
    axes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Node":
        if not isinstance(data, Mapping):
            raise TypeError("node payload must be a mapping")
        return cls(
            name=_text(data["name"]),
            aliases=_string_list(data.get("aliases", [])),
            node_type=_text(data.get("node_type"), NodeType.GENERIC.value),
            industry=_text(data.get("industry")),
            tags=_string_list(data.get("tags", [])),
            source_count=_bounded_int(data.get("source_count"), 0, 1_000_000_000),
            last_updated=_text(data.get("last_updated")),
            axes=_string_list(data.get("axes", [])),
            metadata=_mapping(data.get("metadata", {})),
        )


@dataclass
class Edge:
    """知识库边，v2 区分 supporting / contradicting / deprecated claim。"""

    head: str
    relation: str
    tail: str
    confidence: float = 0.0
    claim_ids: list[str] = field(default_factory=list)
    source_weights: dict[str, float] = field(default_factory=dict)
    supporting_claim_ids: list[str] = field(default_factory=list)
    contradicting_claim_ids: list[str] = field(default_factory=list)
    deprecated_claim_ids: list[str] = field(default_factory=list)
    confidence_reason: str = ""
    conflict_status: str = "none"
    first_seen: str = ""
    last_updated: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Edge":
        if not isinstance(data, Mapping):
            raise TypeError("edge payload must be a mapping")
        return cls(
            head=_text(data["head"]),
            relation=_text(data["relation"]),
            tail=_text(data["tail"]),
            confidence=_finite_float(data.get("confidence")),
            claim_ids=_string_list(data.get("claim_ids", [])),
            source_weights={
                str(key): _finite_float(value)
                for key, value in _mapping(data.get("source_weights", {})).items()
            },
            supporting_claim_ids=_string_list(data.get("supporting_claim_ids", [])),
            contradicting_claim_ids=_string_list(data.get("contradicting_claim_ids", [])),
            deprecated_claim_ids=_string_list(data.get("deprecated_claim_ids", [])),
            confidence_reason=_text(data.get("confidence_reason")),
            conflict_status=_text(data.get("conflict_status"), "none"),
            first_seen=_text(data.get("first_seen")),
            last_updated=_text(data.get("last_updated")),
            metadata=_mapping(data.get("metadata", {})),
        )


def _now_iso() -> str:
    return datetime.now().isoformat()


def _parse_event_time(time_str: str) -> datetime | None:
    """解析事件时间字符串，支持 ISO 格式；无法解析则返回 None。"""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(str(time_str).strip())
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _normalize_name(name: str) -> str:
    """归一化节点名称：去空格、去后缀。"""
    if name is None:
        return ""
    name = str(name).strip()
    name = name.replace(" ", "").replace("　", "")
    # 保留中英文数字，去掉明显后缀
    name = name.rstrip("。、；，")
    return name


class SAGKnowledgeBase:
    """SAG 知识库：Node / Edge / Claim 三层结构。"""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        ontology_registry: OntologyRegistry | None = None,
    ) -> None:
        """初始化知识库。

        Args:
            storage_path: 持久化文件路径，None 则不持久化
            ontology_registry: Ontology 注册表，None 则使用默认值
        """
        self.storage_path = Path(storage_path) if storage_path else None
        self.ontology_registry = ontology_registry or OntologyRegistry.default()
        self._load_failed = False
        self._lock = threading.RLock()
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self.claims: dict[str, Claim] = {}
        self.event_edges: dict[tuple[str, str, str], EventEdge] = {}
        self.event_claims: dict[str, EventClaim] = {}
        self._node_event_index: dict[str, set[str]] = defaultdict(set)
        self._edge_event_index: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self._event_type_index: dict[str, set[str]] = defaultdict(set)
        self._event_status_index: dict[str, set[str]] = defaultdict(set)
        self._recent_event_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        """从文件加载知识库。"""
        if not self.storage_path or not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping):
                raise TypeError("SAG payload must be a mapping")
            for n in _mappings(data.get("nodes", [])):
                node = _decode(Node.from_dict, n)
                if node is None:
                    continue
                node.name = _normalize_name(node.name)
                if node.name:
                    self.nodes[node.name] = node
            for e in _mappings(data.get("edges", [])):
                edge = _decode(Edge.from_dict, e)
                if edge is None:
                    continue
                edge.head = _normalize_name(edge.head)
                edge.relation = _text(edge.relation)
                edge.tail = _normalize_name(edge.tail)
                if edge.head and edge.relation and edge.tail:
                    self.edges[(edge.head, edge.relation, edge.tail)] = edge
            for c in _mappings(data.get("claims", [])):
                claim = _decode(Claim.from_dict, c)
                if claim is None:
                    continue
                if claim.claim_id:
                    self.claims[_text(claim.claim_id)] = claim
            for e in _mappings(data.get("event_edges", [])):
                edge = _decode(EventEdge.from_dict, e)
                if edge is None:
                    continue
                edge.subject = _normalize_name(edge.subject)
                edge.event_type = _text(edge.event_type)
                edge.object = _normalize_name(edge.object)
                if edge.subject and edge.event_type and edge.object:
                    self.event_edges[(edge.subject, edge.event_type, edge.object)] = edge
            for c in _mappings(data.get("event_claims", [])):
                claim = _decode(EventClaim.from_dict, c)
                if claim is None:
                    continue
                if claim.event_id:
                    self.event_claims[_text(claim.event_id)] = claim
            self._rebuild_event_indexes()
            logger.info(
                "加载 SAG: %d nodes, %d edges, %d claims, %d event_edges, %d event_claims",
                len(self.nodes),
                len(self.edges),
                len(self.claims),
                len(self.event_edges),
                len(self.event_claims),
            )
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as e:
            self._load_failed = True
            logger.exception("Failed to load SAG knowledge base from %s: %s", self.storage_path, e)

    def save(self) -> bool:
        """持久化知识库。"""
        if not self.storage_path:
            return True
        if self._load_failed and self.storage_path.exists():
            logger.error(
                "Refusing to overwrite SAG storage after load failure: %s",
                self.storage_path,
            )
            return False
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    "updated_at": _now_iso(),
                    "nodes": [n.to_dict() for n in self.nodes.values()],
                    "edges": [e.to_dict() for e in self.edges.values()],
                    "claims": [c.to_dict() for c in self.claims.values()],
                    "event_edges": [e.to_dict() for e in self.event_edges.values()],
                    "event_claims": [c.to_dict() for c in self.event_claims.values()],
                }
            tmp_path = self.storage_path.with_name(f"{self.storage_path.name}.tmp")
            serialized = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
            tmp_path.write_text(serialized, encoding="utf-8")
            tmp_path.replace(self.storage_path)
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.exception("Failed to save SAG knowledge base to %s: %s", self.storage_path, e)
            return False

    def add_claim(
        self,
        head: str,
        relation: str,
        tail: str,
        evidence: str,
        source_type: str,
        source_title: str,
        timestamp: str | None = None,
        confidence: float | None = None,
        head_type: str | None = None,
        tail_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Claim | None:
        """添加一条 claim，并自动更新 Node 和 Edge。

        Args:
            head: 头实体
            relation: 关系
            tail: 尾实体
            evidence: 证据文本
            source_type: 来源类型
            source_title: 来源标题
            timestamp: 时间戳，None 则使用当前时间
            confidence: 置信度，None 则根据 source_type 计算
            head_type: 头节点类型提示
            tail_type: 尾节点类型提示
            metadata: 额外元数据

        Returns:
            创建的 Claim，如果参数无效则返回 None
        """
        head = _normalize_name(head)
        tail = _normalize_name(tail)
        relation = "" if relation is None else str(relation).strip()
        evidence = "" if evidence is None else str(evidence).strip()

        if not head or not tail or not relation or not evidence:
            return None
        if head == tail:
            return None
        try:
            relation_valid = self.ontology_registry.is_valid_relation(relation)
        except (AttributeError, TypeError, ValueError):
            relation_valid = False
        if not relation_valid:
            logger.warning("未知关系类型: %s", relation)
            return None
        if head in GENERIC_BLACKLIST or tail in GENERIC_BLACKLIST:
            return None
        if any(gs in head for gs in GENERIC_SUBSTRINGS) or any(gs in tail for gs in GENERIC_SUBSTRINGS):
            return None

        timestamp = str(timestamp).strip() if timestamp else _now_iso()
        source_type = "" if source_type is None else str(source_type).strip()
        source_title = "" if source_title is None else str(source_title).strip()
        if confidence is None:
            confidence = get_source_weight(source_type)
        confidence = max(0.0, min(1.0, _finite_float(confidence)))

        claim_id = _stable_hash(
            f"{head}|{relation}|{tail}|{evidence}|{source_title}|{timestamp}"
        )

        with self._lock:
            # 如果完全相同的 claim 已存在，更新其置信度（取高者）
            if claim_id in self.claims:
                existing = self.claims[claim_id]
                if confidence > existing.confidence:
                    existing.confidence = confidence
                    existing.timestamp = timestamp
                claim = existing
            else:
                claim = Claim(
                    claim_id=claim_id,
                    head=head,
                    relation=relation,
                    tail=tail,
                    evidence=evidence,
                    source_type=source_type,
                    source_title=source_title,
                    timestamp=timestamp,
                    confidence=confidence,
                    metadata=_mapping(metadata),
                )
                self.claims[claim_id] = claim

            # 更新节点
            md = _mapping(metadata)
            raw_axes = md.get("structure_axis")
            if isinstance(raw_axes, (list, tuple, set)):
                head_axes = {_text(axis) for axis in raw_axes if _text(axis)}
            elif raw_axes:
                head_axes = {_text(raw_axes)}
            else:
                head_axes = set()
            tail_axes = head_axes.copy()
            self._ensure_node(head, head_type, source_type, axes=head_axes)
            self._ensure_node(tail, tail_type, source_type, axes=tail_axes)

            # 更新边
            self._update_edge(claim)

            return claim

    def _ensure_node(
        self,
        name: str,
        node_type: str | None = None,
        source_type: str = "",
        axes: set[str] | None = None,
    ) -> Node:
        """确保节点存在，必要时更新类型和统计。"""
        norm = _normalize_name(name)
        now = _now_iso()
        try:
            inferred_axes = {_text(axis) for axis in infer_axis(norm) if _text(axis)}
        except (AttributeError, TypeError, ValueError):
            inferred_axes = set()
        if axes:
            inferred_axes.update(_text(axis) for axis in axes if _text(axis))
        axes_list = sorted(inferred_axes)

        if norm in self.nodes:
            node = self.nodes[norm]
            node.source_count += 1
            node.last_updated = now
            try:
                valid_node_type = bool(node_type and self.ontology_registry.is_valid_node_type(node_type))
            except (AttributeError, TypeError, ValueError):
                valid_node_type = False
            if valid_node_type and node.node_type == NodeType.GENERIC.value:
                node.node_type = node_type
            for axis in axes_list:
                if axis not in node.axes:
                    node.axes.append(axis)
        else:
            try:
                valid_node_type = bool(node_type and self.ontology_registry.is_valid_node_type(node_type))
            except (AttributeError, TypeError, ValueError):
                valid_node_type = False
            node_type = node_type if valid_node_type else NodeType.GENERIC.value
            node = Node(
                name=norm,
                node_type=node_type,
                source_count=1,
                last_updated=now,
                axes=axes_list,
            )
            self.nodes[norm] = node
        return node

    def _update_edge(self, claim: Claim) -> Edge:
        """根据 claim 更新边。"""
        key = (claim.head, claim.relation, claim.tail)
        now = _now_iso()
        source_weight = get_source_weight(claim.source_type)

        if key in self.edges:
            edge = self.edges[key]
            if claim.claim_id not in edge.claim_ids:
                edge.claim_ids.append(claim.claim_id)
            edge.source_weights[claim.source_type] = max(
                edge.source_weights.get(claim.source_type, 0),
                source_weight,
            )
            edge.last_updated = now
        else:
            edge = Edge(
                head=claim.head,
                relation=claim.relation,
                tail=claim.tail,
                claim_ids=[claim.claim_id],
                source_weights={claim.source_type: source_weight},
                first_seen=now,
                last_updated=now,
            )
            self.edges[key] = edge

        # 旧的简单置信度计算保留，正式版本由 update_edge_confidence 接管
        return edge

    def _rebuild_event_indexes(self) -> None:
        """根据当前 event_claims 重建内存索引。"""
        with self._lock:
            self._node_event_index.clear()
            self._edge_event_index.clear()
            self._event_type_index.clear()
            self._event_status_index.clear()
            self._recent_event_ids.clear()
            for claim in self.event_claims.values():
                self._index_event_claim(claim)

    def _index_event_claim(self, claim: EventClaim) -> None:
        """将单个 EventClaim 加入内存索引。"""
        for node_id in claim.linked_node_ids:
            self._node_event_index[node_id].add(claim.event_id)
        for edge_key in claim.linked_edge_keys:
            self._edge_event_index[edge_key].add(claim.event_id)
        self._event_type_index[claim.event_type].add(claim.event_id)
        self._event_status_index[claim.status].add(claim.event_id)
        if self._is_recent_event(claim):
            self._recent_event_ids.add(claim.event_id)

    def _unindex_event_claim(self, event_id: str) -> None:
        """将 EventClaim 从内存索引中移除。"""
        for node_id, ids in list(self._node_event_index.items()):
            ids.discard(event_id)
            if not ids:
                del self._node_event_index[node_id]
        for edge_key, ids in list(self._edge_event_index.items()):
            ids.discard(event_id)
            if not ids:
                del self._edge_event_index[edge_key]
        for event_type, ids in list(self._event_type_index.items()):
            ids.discard(event_id)
            if not ids:
                del self._event_type_index[event_type]
        for status, ids in list(self._event_status_index.items()):
            ids.discard(event_id)
            if not ids:
                del self._event_status_index[status]
        self._recent_event_ids.discard(event_id)

    def _is_recent_event(self, claim: EventClaim) -> bool:
        """判断事件是否为近期事件（90 天内或无法解析时间）。"""
        time_str = claim.source_time or claim.time
        if not time_str:
            return True
        parsed = _parse_event_time(time_str)
        if parsed is None:
            return True
        delta = datetime.now() - parsed
        return delta.days <= 90

    def _recompute_event_edge_links(self, edge_key: tuple[str, str, str]) -> None:
        """根据该边上所有 claim 重新聚合 linked_node_ids 与 linked_stable_edge_keys。"""
        edge = self.event_edges.get(edge_key)
        if not edge:
            return
        linked_nodes: set[str] = set()
        linked_edges: set[tuple[str, str, str]] = set()
        for event_id in edge.claim_ids:
            claim = self.event_claims.get(event_id)
            if not claim:
                continue
            linked_nodes.update(claim.linked_node_ids)
            linked_edges.update(claim.linked_edge_keys)
        edge.linked_node_ids = sorted(linked_nodes)
        edge.linked_stable_edge_keys = sorted(linked_edges)

    @staticmethod
    def _stable_event_id(
        subject: str,
        event_type: str,
        object: str,
        time: str,
        evidence: str,
        source_title: str,
    ) -> str:
        """为 EventClaim 生成稳定 ID。"""
        text = f"{subject}|{event_type}|{object}|{time}|{evidence}|{source_title}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def add_event_claim(
        self,
        event_type: str,
        subject: str,
        object: str,
        time: str,
        evidence: str,
        source_type: str,
        source_title: str,
        score: float | None = None,
        status: str | None = None,
        source_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> EventClaim | None:
        """添加一条事件 Claim，自动更新 EventEdge。

        Args:
            event_type: 事件类型，必须在 ontology registry 中
            subject: 事件主体
            object: 事件对象
            time: 事件发生时间（任意字符串，如 2026Q2）
            evidence: 证据文本
            source_type: 来源类型
            source_title: 来源标题
            score: 事件得分
            status: 事件状态，None 则默认 ACTIVE
            source_id: 来源文档 ID
            metadata: 额外元数据

        Returns:
            创建或更新的 EventClaim；参数无效时返回 None
        """
        subject = _normalize_name(subject)
        object = _normalize_name(object)
        event_type = "" if event_type is None else str(event_type).strip()
        evidence = "" if evidence is None else str(evidence).strip()

        if not subject or not object or not event_type or not evidence:
            return None
        try:
            event_type_valid = self.ontology_registry.is_valid_event_type(event_type)
        except (AttributeError, TypeError, ValueError):
            event_type_valid = False
        if not event_type_valid:
            logger.warning("未知事件类型: %s", event_type)
            return None

        status = str(status).strip() if status else EventClaimStatus.ACTIVE.value
        if status not in EVENT_CLAIM_STATUSES:
            status = EventClaimStatus.ACTIVE.value

        source_type = "" if source_type is None else str(source_type).strip()
        source_title = "" if source_title is None else str(source_title).strip()
        time = "" if time is None else str(time).strip()
        source_id = _text(source_id)
        event_id = self._stable_event_id(
            subject, event_type, object, time, evidence, source_title
        )

        if score is None:
            score = get_source_weight(source_type)
        score = max(0.0, min(1.0, _finite_float(score)))

        with self._lock:
            # 如果完全相同的事件 claim 已存在，更新 score/confidence（取高者）
            if event_id in self.event_claims:
                existing = self.event_claims[event_id]
                if score > existing.score:
                    existing.score = score
                    existing.confidence = score
                    existing.time = time
                claim = existing
            else:
                claim = EventClaim(
                    event_id=event_id,
                    event_type=event_type,
                    raw_event_type=None,
                    subject=subject,
                    object=object,
                    time=time,
                    evidence=evidence,
                    source_type=source_type,
                    source_title=source_title,
                    confidence=score,
                    score=score,
                    status=status,
                    source_id=source_id,
                    metadata=_mapping(metadata),
                )
                self.event_claims[event_id] = claim

            # 事件主体/对象也作为稳定图节点存在，便于跨图查询
            self._ensure_node(subject, node_type=None, source_type=source_type)
            self._ensure_node(object, node_type=None, source_type=source_type)

            # 将已存在的稳定节点关联到事件 claim
            if claim.subject and claim.subject in self.nodes and claim.subject not in claim.linked_node_ids:
                claim.linked_node_ids.append(claim.subject)
            if claim.object and claim.object in self.nodes and claim.object not in claim.linked_node_ids:
                claim.linked_node_ids.append(claim.object)

            # 更新索引
            self._unindex_event_claim(claim.event_id)
            self._index_event_claim(claim)

            # 更新事件边
            edge_key = (claim.subject, claim.event_type, claim.object)
            self._update_event_edge(claim)
            self._recompute_event_edge_links(edge_key)
            self.update_event_edge_confidence(edge_key)
            return claim

    def _update_event_edge(self, claim: EventClaim) -> EventEdge:
        """根据 EventClaim 更新事件边。"""
        key = (claim.subject, claim.event_type, claim.object)
        now = _now_iso()
        source_weight = get_source_weight(claim.source_type)

        if key in self.event_edges:
            edge = self.event_edges[key]
            if claim.event_id not in edge.claim_ids:
                edge.claim_ids.append(claim.event_id)
            edge.source_weights[claim.source_type] = max(
                edge.source_weights.get(claim.source_type, 0),
                source_weight,
            )
            edge.last_updated = now
        else:
            edge = EventEdge(
                subject=claim.subject,
                event_type=claim.event_type,
                object=claim.object,
                claim_ids=[claim.event_id],
                source_weights={claim.source_type: source_weight},
                first_seen=now,
                last_updated=now,
            )
            self.event_edges[key] = edge
        return edge

    def update_event_edge_confidence(
        self, edge_key: tuple[str, str, str]
    ) -> EventEdge | None:
        """根据 event 边上所有 claim 重算置信度与聚合统计。

        规则：
        - active / confirmed / promotion_candidate / promoted -> supporting
        - stale -> deprecated
        - retracted / expired -> contradicting
        """
        with self._lock:
            edge = self.event_edges.get(edge_key)
            if not edge:
                return None

            supporting: list[str] = []
            contradicting: list[str] = []
            deprecated: list[str] = []
            source_types: set[str] = set()
            latest_event_time: str = ""

            one_minus = 1.0
            confidence_reasons: list[str] = []
            for eid in edge.claim_ids:
                claim = self.event_claims.get(eid)
                if not claim:
                    continue

                source_type = _text(claim.source_type)
                source_types.add(source_type)
                # 追踪最新事件时间
                if claim.time and (not latest_event_time or claim.time > latest_event_time):
                    latest_event_time = claim.time
                if claim.source_time and (
                    not latest_event_time or claim.source_time > latest_event_time
                ):
                    latest_event_time = claim.source_time

                if claim.status in (
                    EventClaimStatus.RETRACTED.value,
                    EventClaimStatus.EXPIRED.value,
                    EventClaimStatus.CONTRADICTED.value,
                ):
                    contradicting.append(eid)
                    continue
                if claim.status == EventClaimStatus.STALE.value:
                    deprecated.append(eid)
                    continue
                if claim.status in (
                    EventClaimStatus.ACTIVE.value,
                    EventClaimStatus.CONFIRMED.value,
                    EventClaimStatus.PROMOTION_CANDIDATE.value,
                    EventClaimStatus.PROMOTED.value,
                ):
                    supporting.append(eid)
                    score = max(0.0, min(1.0, _finite_float(claim.score or claim.confidence)))
                    diversity_factor = min(1.2, 1.0 + 0.05 * max(0, len(source_types) - 1))
                    one_minus *= 1.0 - score * diversity_factor
                    confidence_reasons.append(f"{source_type}:{score:.2f}")

            edge.supporting_claim_ids = supporting
            edge.contradicting_claim_ids = contradicting
            edge.deprecated_claim_ids = deprecated
            edge.confidence = max(0.0, min(0.99, 1.0 - one_minus))
            if contradicting:
                edge.conflict_status = (
                    "needs_review" if len(source_types) >= 2 else "conflicted"
                )
            else:
                edge.conflict_status = "none"
            edge.confidence_reason = (
                f"supporting={len(supporting)}, contradicting={len(contradicting)}, "
                f"deprecated={len(deprecated)}, sources={sorted(source_types)}, "
                f"scores=[{','.join(confidence_reasons[:5])}]"
            )
            edge.latest_event_time = latest_event_time
            edge.source_count = len(source_types)
            edge.claim_count = len(edge.claim_ids)
            edge.last_updated = _now_iso()
            return edge

    def get_event_neighbors(
        self,
        name: str,
        event_type: str | None = None,
        direction: str = "out",
    ) -> list[EventEdge]:
        """获取事件图邻居。"""
        norm = _normalize_name(name)
        with self._lock:
            result: list[EventEdge] = []
            for edge in self.event_edges.values():
                if event_type and edge.event_type != event_type:
                    continue
                if direction == "out" and edge.subject == norm:
                    result.append(edge)
                elif direction == "in" and edge.object == norm:
                    result.append(edge)
                elif direction == "both" and (edge.subject == norm or edge.object == norm):
                    result.append(edge)
            return result

    def get_event_subgraph(
        self,
        subject: str,
        max_depth: int = 3,
        min_confidence: float = 0.0,
    ) -> dict[str, Any]:
        """从事件主体出发做 BFS，返回事件子图。"""
        root = _normalize_name(subject)
        max_depth = _bounded_int(max_depth, 3, 1000)
        min_confidence = max(0.0, min(1.0, _finite_float(min_confidence)))
        with self._lock:
            visited: set[str] = {root}
            edges_out: list[EventEdge] = []
            queue: deque[tuple[str, int]] = deque([(root, 0)])

            while queue:
                current, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for edge in self.event_edges.values():
                    if edge.subject != current:
                        continue
                    if _finite_float(edge.confidence) < min_confidence:
                        continue
                    edges_out.append(edge)
                    if edge.object not in visited:
                        visited.add(edge.object)
                        queue.append((edge.object, depth + 1))

            return {
                "root": root,
                "nodes": sorted(visited),
                "edges": [e.to_dict() for e in edges_out],
            }

    def get_event_claim(self, event_id: str) -> EventClaim | None:
        """按 ID 获取 EventClaim。"""
        with self._lock:
            return self.event_claims.get(_text(event_id))

    def get_event_claims(
        self,
        event_type: str | None = None,
        status: str | None = None,
        subject: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[EventClaim]:
        """按条件筛选 EventClaim。

        Args:
            event_type: 事件类型过滤。
            status: 状态过滤。
            subject: 主体过滤。
            min_confidence: 最低置信度。

        Returns:
            匹配的 EventClaim 列表。
        """
        min_confidence = max(0.0, min(1.0, _finite_float(min_confidence)))
        edge_key = _edge_key(edge_key)
        if edge_key is None:
            return None
        with self._lock:
            result: list[EventClaim] = []
            norm_subject = _normalize_name(subject) if subject else None
            for claim in self.event_claims.values():
                if event_type and claim.event_type != event_type:
                    continue
                if status and claim.status != status:
                    continue
                if norm_subject and claim.subject != norm_subject:
                    continue
                if max(_finite_float(claim.confidence), _finite_float(claim.score)) < min_confidence:
                    continue
                result.append(claim)
            return result

    def get_event_edges(
        self,
        event_type: str | None = None,
        subject: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[EventEdge]:
        """按条件筛选 EventEdge。

        Args:
            event_type: 事件类型过滤。
            subject: 主体过滤。
            min_confidence: 最低边置信度。

        Returns:
            匹配的 EventEdge 列表。
        """
        min_confidence = max(0.0, min(1.0, _finite_float(min_confidence)))
        with self._lock:
            result: list[EventEdge] = []
            norm_subject = _normalize_name(subject) if subject else None
            for edge in self.event_edges.values():
                if event_type and edge.event_type != event_type:
                    continue
                if norm_subject and edge.subject != norm_subject:
                    continue
                if _finite_float(edge.confidence) < min_confidence:
                    continue
                result.append(edge)
            return result

    def link_event_to_stable_node(self, event_claim_id: str, node_id: str) -> bool:
        """将事件 claim 关联到稳定图节点。

        Args:
            event_claim_id: 事件 claim ID。
            node_id: 稳定图节点 ID。

        Returns:
            关联成功返回 True，否则返回 False。
        """
        with self._lock:
            claim = self.event_claims.get(_text(event_claim_id))
            if not claim:
                return False
            node_id = _normalize_name(node_id)
            if node_id not in self.nodes:
                return False
            if node_id in claim.linked_node_ids:
                return True
            claim.linked_node_ids.append(node_id)
            self._unindex_event_claim(claim.event_id)
            self._index_event_claim(claim)
            self._recompute_event_edge_links((claim.subject, claim.event_type, claim.object))
            return True

    def link_event_to_stable_edge(self, event_claim_id: str, edge_key: tuple[str, str, str]) -> bool:
        """将事件 claim 关联到稳定图边。

        Args:
            event_claim_id: 事件 claim ID。
            edge_key: 稳定图边键 (head, relation, tail)。

        Returns:
            关联成功返回 True，否则返回 False。
        """
        edge_key = _edge_key(edge_key)
        if edge_key is None:
            return False
        with self._lock:
            claim = self.event_claims.get(_text(event_claim_id))
            if not claim:
                return False
            if edge_key not in self.edges:
                return False
            if edge_key in claim.linked_edge_keys:
                return True
            claim.linked_edge_keys.append(edge_key)
            self._unindex_event_claim(claim.event_id)
            self._index_event_claim(claim)
            self._recompute_event_edge_links((claim.subject, claim.event_type, claim.object))
            return True

    def get_event_claims_for_node(self, node_id: str) -> list[EventClaim]:
        """获取关联到指定稳定节点的所有 EventClaim。"""
        node_id = _normalize_name(node_id)
        with self._lock:
            return [
                self.event_claims[eid]
                for eid in self._node_event_index.get(node_id, set())
                if eid in self.event_claims
            ]

    def get_event_claims_for_edge(self, edge_key: tuple[str, str, str]) -> list[EventClaim]:
        """获取关联到指定稳定边的所有 EventClaim。"""
        edge_key = _edge_key(edge_key)
        if edge_key is None:
            return []
        with self._lock:
            return [
                self.event_claims[eid]
                for eid in self._edge_event_index.get(edge_key, set())
                if eid in self.event_claims
            ]

    def get_event_edges_for_path(self, path: list[str]) -> list[EventEdge]:
        """获取路径上任意节点相关的事件边。

        Args:
            path: 节点 ID 列表。

        Returns:
            subject 或 object 出现在 path 中的 EventEdge 列表。
        """
        path_set = {_normalize_name(node) for node in path if _normalize_name(node)} if isinstance(path, (list, tuple, set)) else set()
        with self._lock:
            return [
                edge for edge in self.event_edges.values()
                if edge.subject in path_set or edge.object in path_set
            ]

    def get_recent_event_claims(
        self,
        since: str | None = None,
        event_types: list[str] | None = None,
        min_confidence: float | None = None,
    ) -> list[EventClaim]:
        """获取近期事件 claim。

        Args:
            since: 起始时间字符串，None 则使用内存中的近期索引。
            event_types: 事件类型过滤。
            min_confidence: 最低置信度（取 confidence 与 score 的较大值）。

        Returns:
            匹配的 EventClaim 列表。
        """
        with self._lock:
            candidate_ids = self._recent_event_ids if since is None else self.event_claims.keys()
            result: list[EventClaim] = []
            for event_id in candidate_ids:
                claim = self.event_claims.get(event_id)
                if not claim:
                    continue
                if event_types is not None and claim.event_type not in event_types:
                    continue
                if min_confidence is not None and max(
                    _finite_float(claim.confidence), _finite_float(claim.score)
                ) < _finite_float(min_confidence):
                    continue
                if since is not None:
                    time_str = claim.source_time or claim.time
                    if time_str and time_str < since:
                        continue
                result.append(claim)
            return result

    def update_event_scores(self, now: str | None = None) -> None:
        """重新计算所有 EventClaim 的新鲜度/衰减分，以及 EventEdge 的聚合分。

        Args:
            now: 参考时间字符串，None 则使用当前时间。
        """
        reference = _parse_event_time(now) if now else datetime.now()
        if reference is None:
            reference = datetime.now()

        with self._lock:
            for claim in self.event_claims.values():
                claim.freshness_score = self._compute_event_freshness(claim, reference)
                claim.decay_score = 1.0 - claim.freshness_score

            active_statuses = {
                EventClaimStatus.ACTIVE.value,
                EventClaimStatus.CONFIRMED.value,
            }
            for edge in self.event_edges.values():
                active_claims = [
                    self.event_claims[eid]
                    for eid in edge.claim_ids
                    if eid in self.event_claims and self.event_claims[eid].status in active_statuses
                ]
                if active_claims:
                    edge.aggregate_freshness_score = max(c.freshness_score for c in active_claims)
                    edge.aggregate_event_impact_score = sum(
                        c.confidence * c.freshness_score for c in active_claims
                    ) / len(active_claims)
                else:
                    edge.aggregate_freshness_score = 0.0
                    edge.aggregate_event_impact_score = 0.0

    def _compute_event_freshness(self, claim: EventClaim, reference: datetime) -> float:
        """计算单个事件 claim 的新鲜度得分。"""
        time_str = claim.source_time or claim.time
        if not time_str:
            return 0.1
        parsed = _parse_event_time(time_str)
        if parsed is None:
            return 0.1
        delta_days = (reference - parsed).days
        if delta_days <= 7:
            return 1.0
        if delta_days <= 30:
            return 0.7
        if delta_days <= 90:
            return 0.4
        return 0.1

    def upsert_event_edge(self, edge: EventEdge) -> EventEdge:
        """显式写入或更新 EventEdge。

        Args:
            edge: 待写入的事件边。

        Returns:
            写入后的事件边。
        """
        with self._lock:
            key = (edge.subject, edge.event_type, edge.object)
            existing = self.event_edges.get(key)
            if existing:
                existing.claim_ids = sorted(set(existing.claim_ids + edge.claim_ids))
                existing.source_weights.update(edge.source_weights)
                existing.last_updated = _now_iso()
                self.update_event_edge_confidence(key)
                return existing
            self.event_edges[key] = edge
            self.update_event_edge_confidence(key)
            return edge

    def update_event_claim_status(
        self,
        event_id: str,
        new_status: str,
        reason: str,
        changed_by: str = "system",
    ) -> EventClaim | None:
        """更新 EventClaim 状态并记录历史。

        Args:
            event_id: 事件 claim ID。
            new_status: 新状态。
            reason: 状态变化原因。
            changed_by: 变更来源。

        Returns:
            更新后的 EventClaim，不存在则返回 None。
        """
        if new_status not in EVENT_CLAIM_STATUSES:
            logger.warning("未知 event claim status: %s", new_status)
            return None
        with self._lock:
            claim = self.event_claims.get(_text(event_id))
            if not claim:
                return None
            old_status = claim.status
            if old_status == new_status:
                return claim
            claim.status = new_status
            status_history = list(claim.status_history) if isinstance(claim.status_history, (list, tuple)) else []
            status_history.append({
                "old_status": old_status,
                "new_status": new_status,
                "reason": reason,
                "changed_at": _now_iso(),
                "changed_by": changed_by,
            })
            self._unindex_event_claim(claim.event_id)
            self._index_event_claim(claim)
            self.update_event_edge_confidence((claim.subject, claim.event_type, claim.object))
            return claim

    def event_claim_count(self) -> int:
        """返回事件 claim 数量。"""
        with self._lock:
            return len(self.event_claims)

    def event_edge_count(self) -> int:
        """返回事件边数量。"""
        with self._lock:
            return len(self.event_edges)

    def update_claim_status(
        self,
        claim_id: str,
        new_status: str,
        reason: str,
        trigger_claim_id: str | None = None,
        changed_by: str = "system",
    ) -> Claim | None:
        """更新 claim 状态并强制写入 history。

        Args:
            claim_id: claim ID
            new_status: 新状态
            reason: 状态变化原因
            trigger_claim_id: 触发状态变化的 claim ID
            changed_by: system / user / migration / llm / rule

        Returns:
            更新后的 Claim，不存在则返回 None
        """
        if new_status not in CLAIM_STATUS_SET:
            logger.warning("未知 claim status: %s", new_status)
            return None

        with self._lock:
            claim = self.claims.get(_text(claim_id))
            if not claim:
                return None

            old_status = claim.status
            if old_status == new_status:
                return claim

            changed_at = _now_iso()
            claim.status = new_status
            status_history = list(claim.status_history) if isinstance(claim.status_history, (list, tuple)) else []
            status_history.append({
                "old_status": old_status,
                "new_status": new_status,
                "reason": "" if reason is None else str(reason),
                "trigger_claim_id": trigger_claim_id,
                "changed_at": changed_at,
                "changed_by": changed_by,
            })
            claim.status_history = status_history[-50:]
            claim.valid_from = claim.valid_from or changed_at
            return claim

    def update_edge_confidence(self, edge_key: tuple[str, str, str]) -> Edge | None:
        """根据 edge 上所有 claim 的 claim_score 和来源多样性重算置信度。

        公式：edge_confidence = 1 - prod(1 - claim_score_i * source_diversity_factor_i)
        其中 source_diversity_factor = 1 + 0.05 * (distinct_source_count - 1)，上限 1.2。
        contradicted / deprecated claim 不直接参与正向计算，仅标记。

        Args:
            edge_key: (head, relation, tail)

        Returns:
            更新后的 Edge，不存在则返回 None
        """
        edge_key = _edge_key(edge_key)
        if edge_key is None:
            return None
        with self._lock:
            edge = self.edges.get(edge_key)
            if not edge:
                return None

            supporting: list[str] = []
            contradicting: list[str] = []
            deprecated: list[str] = []
            source_types: set[str] = set()

            one_minus = 1.0
            confidence_reasons: list[str] = []
            for cid in edge.claim_ids:
                claim = self.claims.get(cid)
                if not claim:
                    continue

                source_type = _text(claim.source_type)
                source_types.add(source_type)
                # 仅按 claim 状态判断支持/冲突/废弃；conflict_status 只表示"卷入冲突"，
                # 不能作为 contradicting 依据，否则冲突仲裁中的高权重主 claim 会被错误降级。
                if claim.status in (
                    ClaimStatus.CONTRADICTED.value,
                    ClaimStatus.REJECTED.value,
                ):
                    contradicting.append(cid)
                    continue
                if claim.status == ClaimStatus.DEPRECATED.value:
                    deprecated.append(cid)
                    continue
                if claim.status in (
                    ClaimStatus.TRUSTED.value,
                    ClaimStatus.SUPPORTED.value,
                    ClaimStatus.CANDIDATE.value,
                    ClaimStatus.PENDING_VALIDATION.value,
                ):
                    supporting.append(cid)
                    score = max(0.0, min(1.0, _finite_float(claim.claim_score)))
                    diversity_factor = min(1.2, 1.0 + 0.05 * max(0, len(source_types) - 1))
                    one_minus *= (1.0 - score * diversity_factor)
                    confidence_reasons.append(f"{source_type}:{score:.2f}")

            edge.supporting_claim_ids = supporting
            edge.contradicting_claim_ids = contradicting
            edge.deprecated_claim_ids = deprecated
            edge.confidence = max(0.0, min(0.99, 1.0 - one_minus))

            if contradicting:
                edge.conflict_status = "needs_review" if len(source_types) >= 2 else "conflicted"
            else:
                edge.conflict_status = "none"

            edge.confidence_reason = (
                f"supporting={len(supporting)}, contradicting={len(contradicting)}, "
                f"deprecated={len(deprecated)}, sources={sorted(source_types)}, "
                f"scores=[{','.join(confidence_reasons[:5])}]"
            )
            edge.last_updated = _now_iso()
            return edge

    def get_subgraph(
        self,
        root: str,
        max_depth: int = 3,
        min_confidence: float = 0.0,
    ) -> dict[str, Any]:
        """从 root 出发做 BFS，返回 subgraph。

        Args:
            root: 根节点名
            max_depth: 最大深度
            min_confidence: 边置信度阈值

        Returns:
            {"nodes": [...], "edges": [...], "root": root}
        """
        root = _normalize_name(root)
        max_depth = _bounded_int(max_depth, 3, 1000)
        min_confidence = max(0.0, min(1.0, _finite_float(min_confidence)))
        with self._lock:
            visited: set[str] = {root}
            edges_out: list[Edge] = []
            queue: deque[tuple[str, int]] = deque([(root, 0)])

            while queue:
                current, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for edge in self.edges.values():
                    if edge.head != current:
                        continue
                    if edge.confidence < min_confidence:
                        continue
                    edges_out.append(edge)
                    if edge.tail not in visited:
                        visited.add(edge.tail)
                        queue.append((edge.tail, depth + 1))

            nodes_out = [self.nodes[name] for name in visited if name in self.nodes]
            return {
                "root": root,
                "nodes": [n.to_dict() for n in nodes_out],
                "edges": [e.to_dict() for e in edges_out],
            }

    def get_claims_for_edge(self, head: str, relation: str, tail: str) -> list[Claim]:
        """获取某条边的所有 claim。"""
        key = _edge_key((head, relation, tail))
        if key is None:
            return []
        with self._lock:
            edge = self.edges.get(key)
            if not edge:
                return []
            return [self.claims[cid] for cid in edge.claim_ids if cid in self.claims]

    def get_neighbors(
        self,
        name: str,
        relation: str | None = None,
        direction: str = "out",
    ) -> list[Edge]:
        """获取节点的邻居边。"""
        name = _normalize_name(name)
        with self._lock:
            result: list[Edge] = []
            for edge in self.edges.values():
                if relation and edge.relation != relation:
                    continue
                if direction in ("out", "both") and edge.head == name:
                    result.append(edge)
                if direction in ("in", "both") and edge.tail == name:
                    result.append(edge)
            return result

    def has_node(self, name: str) -> bool:
        with self._lock:
            return _normalize_name(name) in self.nodes

    def has_edge(self, head: str, relation: str, tail: str) -> bool:
        key = _edge_key((head, relation, tail))
        if key is None:
            return False
        with self._lock:
            return key in self.edges

    def node_count(self) -> int:
        with self._lock:
            return len(self.nodes)

    def edge_count(self) -> int:
        with self._lock:
            return len(self.edges)

    def claim_count(self) -> int:
        with self._lock:
            return len(self.claims)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": _now_iso(),
                "nodes": [n.to_dict() for n in self.nodes.values()],
                "edges": [e.to_dict() for e in self.edges.values()],
                "claims": [c.to_dict() for c in self.claims.values()],
                "event_edges": [e.to_dict() for e in self.event_edges.values()],
                "event_claims": [c.to_dict() for c in self.event_claims.values()],
            }
