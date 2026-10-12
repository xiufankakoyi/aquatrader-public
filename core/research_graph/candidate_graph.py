"""CandidateGraph:知识超图扩展的候选图谱持久层。

与主 GraphStore 的区别:
- CandidateStore 存储 DISCOVERED/CANDIDATE/SUPPORTED 等未完全验证的节点和边
- 不直接写入 ACTIVE/TRUSTED,必须经 EvidenceActivator + Verifier 升级
- 候选节点/边携带完整元数据:status/confidence/activation_score/evidence_slots/
  source_trace/first_discovered_by/last_seen_at
- PRUNED/REJECTED 状态不参与召回和推理
- 使用 LanceDB 持久化,与 DocumentChunkStore 共享同一 db 模式

设计原则:
- KISS:用 dict 做内存索引 + LanceDB 持久化,不引入新框架
- DRY:复用 utc_now() 等工具函数
- 防御性:add_node/add_edge 前校验必填字段,status 不允许 ACTIVE/TRUSTED 直接写入
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa

from core.research_graph.candidate_schema import (
    CandidateStatus,
    EXPLORATION_VISIBLE_STATUSES,
    RESEARCH_VISIBLE_STATUSES,
    is_exploration_visible,
    is_research_visible,
)
from core.research_graph.models import utc_now

logger = logging.getLogger(__name__)


@dataclass
class CandidateNode:
    """候选节点:扩展引擎发现但尚未完全验证的知识节点。"""

    candidate_id: str
    name: str
    node_type: str
    status: str = CandidateStatus.DISCOVERED.value
    confidence: float = 0.3
    activation_score: float = 0.0
    evidence_slots: list[dict[str, Any]] = field(default_factory=list)
    source_trace: list[dict[str, Any]] = field(default_factory=list)
    first_discovered_by: str = ""
    last_seen_at: str = field(default_factory=utc_now)
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def mapping_confidence(self) -> float:
        """实体映射置信度；历史节点缺失时保守返回 0。"""
        return float((self.attributes or {}).get("mapping_confidence", 0.0) or 0.0)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateNode":
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})


@dataclass
class CandidateEdge:
    """候选边:扩展引擎发现但尚未完全验证的关系。"""

    candidate_id: str
    subject_id: str
    object_id: str
    relation: str
    status: str = CandidateStatus.DISCOVERED.value
    confidence: float = 0.3
    activation_score: float = 0.0
    evidence_slots: list[dict[str, Any]] = field(default_factory=list)
    source_trace: list[dict[str, Any]] = field(default_factory=list)
    first_discovered_by: str = ""
    last_seen_at: str = field(default_factory=utc_now)
    qualifiers: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def claim_id(self) -> str:
        return str((self.qualifiers or {}).get("claim_id", ""))

    @property
    def direction(self) -> str:
        return str((self.qualifiers or {}).get("direction", "forward"))

    @property
    def valid_from(self) -> str:
        return str((self.qualifiers or {}).get("valid_from", ""))

    @property
    def valid_to(self) -> str:
        return str((self.qualifiers or {}).get("valid_to", ""))

    @property
    def conflict_status(self) -> str:
        return str((self.qualifiers or {}).get("conflict_status", "none"))

    @property
    def mapping_confidence(self) -> float:
        return float((self.qualifiers or {}).get("mapping_confidence", 0.0) or 0.0)

    def set_claim_semantics(
        self, *, claim_id: str = "", direction: str = "forward",
        valid_from: str = "", valid_to: str = "", conflict_status: str = "none",
        mapping_confidence: float = 0.0,
    ) -> None:
        """把新语义写入兼容的 qualifiers，不改变现有 LanceDB schema。"""
        self.qualifiers = dict(self.qualifiers or {})
        self.qualifiers.update({
            "claim_id": claim_id,
            "direction": direction,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "conflict_status": conflict_status,
            "mapping_confidence": max(0.0, min(1.0, float(mapping_confidence))),
        })

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateEdge":
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})


def _candidate_node_schema() -> pa.Schema:
    return pa.schema([
        pa.field("candidate_id", pa.string()),
        pa.field("name", pa.string()),
        pa.field("node_type", pa.string()),
        pa.field("status", pa.string()),
        pa.field("confidence", pa.float64()),
        pa.field("activation_score", pa.float64()),
        pa.field("evidence_slots", pa.string()),
        pa.field("source_trace", pa.string()),
        pa.field("first_discovered_by", pa.string()),
        pa.field("last_seen_at", pa.string()),
        pa.field("aliases", pa.string()),
        pa.field("description", pa.string()),
        pa.field("attributes", pa.string()),
        pa.field("created_at", pa.string()),
        pa.field("updated_at", pa.string()),
    ])


def _candidate_edge_schema() -> pa.Schema:
    return pa.schema([
        pa.field("candidate_id", pa.string()),
        pa.field("subject_id", pa.string()),
        pa.field("object_id", pa.string()),
        pa.field("relation", pa.string()),
        pa.field("status", pa.string()),
        pa.field("confidence", pa.float64()),
        pa.field("activation_score", pa.float64()),
        pa.field("evidence_slots", pa.string()),
        pa.field("source_trace", pa.string()),
        pa.field("first_discovered_by", pa.string()),
        pa.field("last_seen_at", pa.string()),
        pa.field("qualifiers", pa.string()),
        pa.field("created_at", pa.string()),
        pa.field("updated_at", pa.string()),
    ])


class CandidateStore:
    """候选图谱持久化存储(LanceDB 后端)。

    与 DocumentChunkStore 同目录,表名:
    - candidate_nodes
    - candidate_edges
    """

    NODES_TABLE = "candidate_nodes"
    EDGES_TABLE = "candidate_edges"

    def __init__(self, db_path: str | Path = "data/vector_store/research_chunks.lance") -> None:
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.db_path))
        self._ensure_tables()
        self._nodes_cache: dict[str, CandidateNode] = {}
        self._edges_cache: dict[str, CandidateEdge] = {}
        self._load_cache()

    def _ensure_tables(self) -> None:
        existing = set(self.db.list_tables().tables)
        if self.NODES_TABLE not in existing:
            empty = pa.table({f.name: [] for f in _candidate_node_schema()}, schema=_candidate_node_schema())
            self.db.create_table(self.NODES_TABLE, empty)
        if self.EDGES_TABLE not in existing:
            empty = pa.table({f.name: [] for f in _candidate_edge_schema()}, schema=_candidate_edge_schema())
            self.db.create_table(self.EDGES_TABLE, empty)

    def _load_cache(self) -> None:
        """启动时从 LanceDB 加载到内存索引。"""
        self._nodes_cache.clear()
        self._edges_cache.clear()
        try:
            nodes_table = self.db.open_table(self.NODES_TABLE)
            for row in nodes_table.search().to_list():
                node = self._row_to_node(row)
                self._nodes_cache[node.candidate_id] = node
        except Exception as exc:
            logger.debug("load candidate nodes cache failed: %s", exc)
        try:
            edges_table = self.db.open_table(self.EDGES_TABLE)
            for row in edges_table.search().to_list():
                edge = self._row_to_edge(row)
                self._edges_cache[edge.candidate_id] = edge
        except Exception as exc:
            logger.debug("load candidate edges cache failed: %s", exc)

    def add_node(self, node: CandidateNode) -> CandidateNode:
        """添加候选节点。状态不能为 ACTIVE/TRUSTED。"""
        if node.status in (CandidateStatus.ACTIVE.value, CandidateStatus.TRUSTED.value):
            raise ValueError(
                f"CandidateStore 不允许直接写入 {node.status} 状态; "
                "必须经 EvidenceActivator 升级或通过 promote_to_main_graph 提升"
            )
        if not node.first_discovered_by:
            node.first_discovered_by = "unknown"
        node.updated_at = utc_now()
        self._nodes_cache[node.candidate_id] = node
        self._persist_node(node)
        return node

    def add_edge(self, edge: CandidateEdge) -> CandidateEdge:
        """添加候选边。状态不能为 ACTIVE/TRUSTED。"""
        if edge.status in (CandidateStatus.ACTIVE.value, CandidateStatus.TRUSTED.value):
            raise ValueError(
                f"CandidateStore 不允许直接写入 {edge.status} 状态; "
                "必须经 EvidenceActivator 升级"
            )
        if not edge.first_discovered_by:
            edge.first_discovered_by = "unknown"
        edge.updated_at = utc_now()
        self._edges_cache[edge.candidate_id] = edge
        self._persist_edge(edge)
        return edge

    def get_node(self, candidate_id: str) -> CandidateNode | None:
        return self._nodes_cache.get(candidate_id)

    def get_edge(self, candidate_id: str) -> CandidateEdge | None:
        return self._edges_cache.get(candidate_id)

    def update_node(self, candidate_id: str, **updates: Any) -> CandidateNode | None:
        """更新候选节点字段(增量)。"""
        node = self._nodes_cache.get(candidate_id)
        if node is None:
            return None
        for key, value in updates.items():
            if hasattr(node, key):
                setattr(node, key, value)
        node.updated_at = utc_now()
        self._persist_node(node)
        return node

    def update_edge(self, candidate_id: str, **updates: Any) -> CandidateEdge | None:
        edge = self._edges_cache.get(candidate_id)
        if edge is None:
            return None
        for key, value in updates.items():
            if hasattr(edge, key):
                setattr(edge, key, value)
        edge.updated_at = utc_now()
        self._persist_edge(edge)
        return edge

    def update_nodes_batch(
        self,
        updates: list[tuple[str, dict[str, Any]]],
    ) -> int:
        """批量更新候选节点。

        Args:
            updates: [(candidate_id, {field: value}), ...]

        Returns:
            成功更新的节点数量。
        """
        if not updates:
            return 0

        now = utc_now()
        changed_nodes: list[CandidateNode] = []
        for candidate_id, field_updates in updates:
            node = self._nodes_cache.get(candidate_id)
            if node is None:
                continue
            for key, value in field_updates.items():
                if hasattr(node, key):
                    setattr(node, key, value)
            node.updated_at = now
            changed_nodes.append(node)

        if not changed_nodes:
            return 0

        self._persist_nodes_batch(changed_nodes)
        return len(changed_nodes)

    def update_edges_batch(
        self,
        updates: list[tuple[str, dict[str, Any]]],
    ) -> int:
        """批量更新候选边。

        Args:
            updates: [(candidate_id, {field: value}), ...]

        Returns:
            成功更新的边数量。
        """
        if not updates:
            return 0

        now = utc_now()
        changed_edges: list[CandidateEdge] = []
        for candidate_id, field_updates in updates:
            edge = self._edges_cache.get(candidate_id)
            if edge is None:
                continue
            for key, value in field_updates.items():
                if hasattr(edge, key):
                    setattr(edge, key, value)
            edge.updated_at = now
            changed_edges.append(edge)

        if not changed_edges:
            return 0

        self._persist_edges_batch(changed_edges)
        return len(changed_edges)

    def list_candidates(
        self,
        *,
        view: str = "exploration",
        node_type: str = "",
        relation: str = "",
    ) -> list[CandidateNode]:
        """列出候选节点。

        Args:
            view: "research" 仅返回 ACTIVE/TRUSTED; "exploration" 返回除 PRUNED/REJECTED 外所有。
            node_type: 可选过滤节点类型。
            relation: 可选过滤相关关系类型(返回包含该关系的边连接的节点)。

        Returns:
            符合条件的 CandidateNode 列表。
        """
        visible_check = is_research_visible if view == "research" else is_exploration_visible
        result: list[CandidateNode] = []
        for node in self._nodes_cache.values():
            if not visible_check(node.status):
                continue
            if node_type and node.node_type != node_type:
                continue
            result.append(node)
        if relation:
            related_ids = {
                e.subject_id for e in self._edges_cache.values()
                if e.relation == relation and visible_check(e.status)
            } | {
                e.object_id for e in self._edges_cache.values()
                if e.relation == relation and visible_check(e.status)
            }
            result = [n for n in result if n.candidate_id in related_ids]
        result.sort(key=lambda n: (-n.activation_score, n.candidate_id))
        return result

    def list_edges(
        self,
        *,
        view: str = "exploration",
        subject_id: str = "",
        object_id: str = "",
        relation: str = "",
    ) -> list[CandidateEdge]:
        """列出候选边。"""
        visible_check = is_research_visible if view == "research" else is_exploration_visible
        result: list[CandidateEdge] = []
        for edge in self._edges_cache.values():
            if not visible_check(edge.status):
                continue
            if subject_id and edge.subject_id != subject_id:
                continue
            if object_id and edge.object_id != object_id:
                continue
            if relation and edge.relation != relation:
                continue
            result.append(edge)
        result.sort(key=lambda e: (-e.activation_score, e.candidate_id))
        return result

    def find_by_name(self, name: str, *, node_type: str = "") -> list[CandidateNode]:
        """按名称/别名模糊查找候选节点。"""
        name_lower = name.lower().strip()
        results: list[CandidateNode] = []
        for node in self._nodes_cache.values():
            if not is_exploration_visible(node.status):
                continue
            if node_type and node.node_type != node_type:
                continue
            names = [node.name.lower(), *[a.lower() for a in node.aliases]]
            if any(name_lower in n or n in name_lower for n in names if n):
                results.append(node)
        return results

    def neighbors(self, node_id: str, *, view: str = "exploration") -> list[tuple[CandidateEdge, CandidateNode | None]]:
        """返回节点的邻接边和另一端节点。邻接包括主图和候选图节点。"""
        visible_check = is_research_visible if view == "research" else is_exploration_visible
        results: list[tuple[CandidateEdge, CandidateNode | None]] = []
        for edge in self._edges_cache.values():
            if not visible_check(edge.status):
                continue
            other_id: str | None = None
            if edge.subject_id == node_id:
                other_id = edge.object_id
            elif edge.object_id == node_id:
                other_id = edge.subject_id
            if other_id is not None:
                other_node = self._nodes_cache.get(other_id)
                results.append((edge, other_node))
        return results

    def promote_candidate(
        self,
        candidate_id: str,
        *,
        to_main_graph: bool = False,
        claim_id: str = "",
        transition_id: str = "",
    ) -> dict[str, Any]:
        """将候选节点升级为 ACTIVE,可选提升到主图。

        Returns:
            {"promoted": bool, "status": str, "main_graph_payload": dict | None}
        """
        node = self._nodes_cache.get(candidate_id)
        if node is None:
            return {"promoted": False, "status": "not_found", "main_graph_payload": None}
        if not claim_id or not transition_id:
            return {
                "promoted": False,
                "status": "claim_policy_required",
                "main_graph_payload": None,
            }
        node.status = CandidateStatus.ACTIVE.value
        node.activation_score = max(node.activation_score, 1.0)
        node.attributes = dict(node.attributes or {})
        node.attributes.update({
            "claim_id": claim_id,
            "state_transition_id": transition_id,
            "terminal_status_source": "claim_policy",
        })
        node.updated_at = utc_now()
        self._persist_node(node)
        payload = None
        if to_main_graph:
            payload = {
                "node_id": node.candidate_id,
                "type": node.node_type,
                "name": node.name,
                "aliases": list(node.aliases),
                "description": node.description,
                "attributes": dict(node.attributes),
                "source": "candidate_promotion",
            }
        return {"promoted": True, "status": node.status, "main_graph_payload": payload}

    def _persist_node(self, node: CandidateNode) -> None:
        self._persist_nodes_batch([node])

    def _persist_edge(self, edge: CandidateEdge) -> None:
        self._persist_edges_batch([edge])

    def _persist_nodes_batch(self, nodes: list[CandidateNode]) -> None:
        """批量持久化候选节点:先批量删除,再批量添加。"""
        if not nodes:
            return
        try:
            table = self.db.open_table(self.NODES_TABLE)
            ids = [_escape_sql(n.candidate_id) for n in nodes]
            # LanceDB delete 支持 IN 表达式;单条时回退到等号
            if len(ids) == 1:
                table.delete(f"candidate_id = '{ids[0]}'")
            else:
                id_list = ", ".join(f"'{i}'" for i in ids)
                table.delete(f"candidate_id IN ({id_list})")
            records = _merge_records(nodes, self._node_to_record)
            arrow_table = pa.table(records, schema=_candidate_node_schema())
            table.add(arrow_table)
        except Exception as exc:
            logger.warning("batch persist candidate nodes failed: %s", exc)

    def _persist_edges_batch(self, edges: list[CandidateEdge]) -> None:
        """批量持久化候选边:先批量删除,再批量添加。"""
        if not edges:
            return
        try:
            table = self.db.open_table(self.EDGES_TABLE)
            ids = [_escape_sql(e.candidate_id) for e in edges]
            if len(ids) == 1:
                table.delete(f"candidate_id = '{ids[0]}'")
            else:
                id_list = ", ".join(f"'{i}'" for i in ids)
                table.delete(f"candidate_id IN ({id_list})")
            records = _merge_records(edges, self._edge_to_record)
            arrow_table = pa.table(records, schema=_candidate_edge_schema())
            table.add(arrow_table)
        except Exception as exc:
            logger.warning("batch persist candidate edges failed: %s", exc)

    @staticmethod
    def _node_to_record(node: CandidateNode) -> dict[str, list]:
        return {
            "candidate_id": [node.candidate_id],
            "name": [node.name],
            "node_type": [node.node_type],
            "status": [node.status],
            "confidence": [float(node.confidence)],
            "activation_score": [float(node.activation_score)],
            "evidence_slots": [json.dumps(node.evidence_slots, ensure_ascii=False)],
            "source_trace": [json.dumps(node.source_trace, ensure_ascii=False)],
            "first_discovered_by": [node.first_discovered_by],
            "last_seen_at": [node.last_seen_at],
            "aliases": [json.dumps(node.aliases, ensure_ascii=False)],
            "description": [node.description],
            "attributes": [json.dumps(node.attributes, ensure_ascii=False)],
            "created_at": [node.created_at],
            "updated_at": [node.updated_at],
        }

    @staticmethod
    def _edge_to_record(edge: CandidateEdge) -> dict[str, list]:
        return {
            "candidate_id": [edge.candidate_id],
            "subject_id": [edge.subject_id],
            "object_id": [edge.object_id],
            "relation": [edge.relation],
            "status": [edge.status],
            "confidence": [float(edge.confidence)],
            "activation_score": [float(edge.activation_score)],
            "evidence_slots": [json.dumps(edge.evidence_slots, ensure_ascii=False)],
            "source_trace": [json.dumps(edge.source_trace, ensure_ascii=False)],
            "first_discovered_by": [edge.first_discovered_by],
            "last_seen_at": [edge.last_seen_at],
            "qualifiers": [json.dumps(edge.qualifiers, ensure_ascii=False)],
            "created_at": [edge.created_at],
            "updated_at": [edge.updated_at],
        }

    @staticmethod
    def _row_to_node(row: dict[str, Any]) -> CandidateNode:
        return CandidateNode(
            candidate_id=str(row.get("candidate_id", "")),
            name=str(row.get("name", "")),
            node_type=str(row.get("node_type", "")),
            status=str(row.get("status", CandidateStatus.DISCOVERED.value)),
            confidence=float(row.get("confidence", 0.3) or 0.3),
            activation_score=float(row.get("activation_score", 0.0) or 0.0),
            evidence_slots=_safe_json_loads(row.get("evidence_slots", "[]"), []),
            source_trace=_safe_json_loads(row.get("source_trace", "[]"), []),
            first_discovered_by=str(row.get("first_discovered_by", "")),
            last_seen_at=str(row.get("last_seen_at", "")),
            aliases=_safe_json_loads(row.get("aliases", "[]"), []),
            description=str(row.get("description", "")),
            attributes=_safe_json_loads(row.get("attributes", "{}"), {}),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )

    @staticmethod
    def _row_to_edge(row: dict[str, Any]) -> CandidateEdge:
        return CandidateEdge(
            candidate_id=str(row.get("candidate_id", "")),
            subject_id=str(row.get("subject_id", "")),
            object_id=str(row.get("object_id", "")),
            relation=str(row.get("relation", "")),
            status=str(row.get("status", CandidateStatus.DISCOVERED.value)),
            confidence=float(row.get("confidence", 0.3) or 0.3),
            activation_score=float(row.get("activation_score", 0.0) or 0.0),
            evidence_slots=_safe_json_loads(row.get("evidence_slots", "[]"), []),
            source_trace=_safe_json_loads(row.get("source_trace", "[]"), []),
            first_discovered_by=str(row.get("first_discovered_by", "")),
            last_seen_at=str(row.get("last_seen_at", "")),
            qualifiers=_safe_json_loads(row.get("qualifiers", "{}"), {}),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _merge_records(items: list[Any], to_record_fn: Any) -> dict[str, list]:
    """将多个单记录字典合并为一个批量 Arrow Table 记录字典。"""
    if not items:
        return {}
    merged: dict[str, list] | None = None
    for item in items:
        record = to_record_fn(item)
        if merged is None:
            merged = {k: list(v) for k, v in record.items()}
        else:
            for k, v in record.items():
                merged[k].extend(v)
    return merged or {}
