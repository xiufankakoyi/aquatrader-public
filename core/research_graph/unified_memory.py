"""统一研究记忆层：跨主题共享的持续演化世界模型。

整合三组件:
    1. GraphStore — 主图，持有 HyperNode / HyperEdge / Evidence
    2. NeuronStateStore — 神经元状态（LanceDB 持久化）
    3. DocumentChunkStore — 文档向量记忆（LanceDB 持久化）

核心设计:
    - 单例：整个进程共享一个 UnifiedResearchMemory 实例
    - 持久化：所有数据落盘到统一 LanceDB 目录（nodes/edges/evidence/documents 表）
    - 跨主题：节点/边/证据全局共享，不再每主题独立存储
    - 缓存：W-TinyLFU 缓存热节点、热邻接、热查询结果
    - 合并：新证据到达时更新同一节点，而非创建新 store
    - 降级：LanceDB 不可用时回退到 JSON 文件持久化（graph_store.json）

接入 ResearchGraphEnvironment:
    - UnifiedResearchMemory 创建并持有 environment 实例
    - environment 的 event_loop_tick 驱动神经元网络
    - 新证据通过 environment.route_evidence 注入
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.research_graph.wtiny_lfu_cache import WTinyLFUCache

logger = logging.getLogger(__name__)


def _safe_json_dumps(value: Any) -> str:
    """安全序列化为 JSON 字符串，失败时返回空 JSON。"""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def _safe_json_loads(raw: Any, default: Any) -> Any:
    """安全反序列化 JSON 字符串，失败时返回默认值。"""
    if not raw:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _escape_sql(value: str) -> str:
    """转义 LanceDB SQL 字符串中的单引号。"""
    return value.replace("'", "''")


class _LanceGraphPersistence:
    """GraphStore 的 LanceDB 持久化适配器。

    将 HyperNode/HyperEdge/Evidence 序列化为 Arrow 表行存储，
    支持按主键 upsert（先 delete 再 add）。LanceDB 不可用时抛出异常，
    由上层调用方降级到 JSON 文件持久化。

    表结构（表名遵循约束 nodes/edges/evidence/documents）:
        - nodes: node_id 主键
        - edges: edge_id 主键
        - evidence: evidence_id 主键

    Args:
        db_path: LanceDB 数据库目录路径。
    """

    NODES_TABLE = "nodes"
    EDGES_TABLE = "edges"
    EVIDENCE_TABLE = "evidence"

    def __init__(self, db_path: str | Path) -> None:
        import lancedb
        import pyarrow as pa

        self._pa = pa
        self._db_path = Path(db_path)
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self._db_path))
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """确保所需表存在，缺失则按空 schema 创建。"""
        pa = self._pa
        existing = set(self._db.list_tables().tables)

        if self.NODES_TABLE not in existing:
            empty = pa.table({f.name: [] for f in self._nodes_schema()}, schema=self._nodes_schema())
            self._db.create_table(self.NODES_TABLE, empty)
        if self.EDGES_TABLE not in existing:
            empty = pa.table({f.name: [] for f in self._edges_schema()}, schema=self._edges_schema())
            self._db.create_table(self.EDGES_TABLE, empty)
        if self.EVIDENCE_TABLE not in existing:
            empty = pa.table({f.name: [] for f in self._evidence_schema()}, schema=self._evidence_schema())
            self._db.create_table(self.EVIDENCE_TABLE, empty)

    def _nodes_schema(self) -> Any:
        """节点表 schema。"""
        pa = self._pa
        return pa.schema([
            pa.field("node_id", pa.string()),
            pa.field("type", pa.string()),
            pa.field("name", pa.string()),
            pa.field("aliases", pa.string()),
            pa.field("description", pa.string()),
            pa.field("attributes", pa.string()),
            pa.field("status", pa.string()),
            pa.field("source", pa.string()),
            pa.field("created_at", pa.string()),
            pa.field("updated_at", pa.string()),
        ])

    def _edges_schema(self) -> Any:
        """边表 schema。"""
        pa = self._pa
        return pa.schema([
            pa.field("edge_id", pa.string()),
            pa.field("relation", pa.string()),
            pa.field("subjects", pa.string()),
            pa.field("objects", pa.string()),
            pa.field("qualifiers", pa.string()),
            pa.field("dimensions", pa.string()),
            pa.field("primary_dimension", pa.string()),
            pa.field("route_layer", pa.string()),
            pa.field("time_stage", pa.string()),
            pa.field("confidence", pa.string()),
            pa.field("status", pa.string()),
            pa.field("evidence_ids", pa.string()),
            pa.field("counter_evidence_ids", pa.string()),
            pa.field("source", pa.string()),
            pa.field("created_at", pa.string()),
            pa.field("updated_at", pa.string()),
        ])

    def _evidence_schema(self) -> Any:
        """证据表 schema。"""
        pa = self._pa
        return pa.schema([
            pa.field("evidence_id", pa.string()),
            pa.field("source_type", pa.string()),
            pa.field("source_title", pa.string()),
            pa.field("source_id", pa.string()),
            pa.field("document_id", pa.string()),
            pa.field("source_url", pa.string()),
            pa.field("publish_date", pa.string()),
            pa.field("quote", pa.string()),
            pa.field("summary", pa.string()),
            pa.field("evidence_level", pa.string()),
            pa.field("supports", pa.string()),
            pa.field("contradicts", pa.string()),
            pa.field("freshness", pa.string()),
            pa.field("extracted_by", pa.string()),
            pa.field("verified_by", pa.string()),
            pa.field("metadata", pa.string()),
            pa.field("event_date", pa.string()),
            pa.field("causal_status", pa.string()),
        ])

    def load_all(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """从 LanceDB 加载全部节点/边/证据并返回反序列化字典。

        Returns:
            (nodes_dict, edges_dict, evidence_dict)，每个字典的值为对应的 dataclass 实例。
        """
        from core.research_graph.models import Evidence, HyperEdge, HyperNode

        nodes: dict[str, HyperNode] = {}
        edges: dict[str, HyperEdge] = {}
        evidence: dict[str, Evidence] = {}

        try:
            table = self._db.open_table(self.NODES_TABLE)
            for row in table.search().to_list():
                node = HyperNode(
                    node_id=str(row.get("node_id", "")),
                    type=str(row.get("type", "")),
                    name=str(row.get("name", "")),
                    aliases=_safe_json_loads(row.get("aliases", "[]"), []),
                    description=str(row.get("description", "")),
                    attributes=_safe_json_loads(row.get("attributes", "{}"), {}),
                    status=str(row.get("status", "ACTIVE")),
                    source=str(row.get("source", "manual")),
                    created_at=str(row.get("created_at", "")),
                    updated_at=str(row.get("updated_at", "")),
                )
                nodes[node.node_id] = node
        except Exception as exc:
            logger.warning("从 LanceDB 加载 nodes 失败: %s", exc)

        try:
            table = self._db.open_table(self.EDGES_TABLE)
            for row in table.search().to_list():
                edge = HyperEdge(
                    edge_id=str(row.get("edge_id", "")),
                    relation=str(row.get("relation", "")),
                    subjects=_safe_json_loads(row.get("subjects", "[]"), []),
                    objects=_safe_json_loads(row.get("objects", "[]"), []),
                    qualifiers=_safe_json_loads(row.get("qualifiers", "{}"), {}),
                    dimensions=_safe_json_loads(row.get("dimensions", "[]"), []),
                    primary_dimension=str(row.get("primary_dimension", "")),
                    route_layer=str(row.get("route_layer", "")),
                    time_stage=str(row.get("time_stage", "")),
                    confidence=str(row.get("confidence", "SPECULATIVE")),
                    status=str(row.get("status", "CANDIDATE")),
                    evidence_ids=_safe_json_loads(row.get("evidence_ids", "[]"), []),
                    counter_evidence_ids=_safe_json_loads(row.get("counter_evidence_ids", "[]"), []),
                    source=str(row.get("source", "manual")),
                    created_at=str(row.get("created_at", "")),
                    updated_at=str(row.get("updated_at", "")),
                )
                edges[edge.edge_id] = edge
        except Exception as exc:
            logger.warning("从 LanceDB 加载 edges 失败: %s", exc)

        try:
            table = self._db.open_table(self.EVIDENCE_TABLE)
            for row in table.search().to_list():
                ev = Evidence(
                    evidence_id=str(row.get("evidence_id", "")),
                    source_type=str(row.get("source_type", "")),
                    source_title=str(row.get("source_title", "")),
                    source_id=str(row.get("source_id", "")),
                    document_id=str(row.get("document_id", "")),
                    source_url=str(row.get("source_url", "")),
                    publish_date=str(row.get("publish_date", "")),
                    quote=str(row.get("quote", "")),
                    summary=str(row.get("summary", "")),
                    evidence_level=str(row.get("evidence_level", "SPECULATIVE")),
                    supports=_safe_json_loads(row.get("supports", "[]"), []),
                    contradicts=_safe_json_loads(row.get("contradicts", "[]"), []),
                    freshness=str(row.get("freshness", "current")),
                    extracted_by=str(row.get("extracted_by", "human")),
                    verified_by=str(row.get("verified_by", "rule_verifier")),
                    metadata=_safe_json_loads(row.get("metadata", "{}"), {}),
                    event_date=str(row.get("event_date", "")),
                    causal_status=str(row.get("causal_status", "")),
                )
                evidence[ev.evidence_id] = ev
        except Exception as exc:
            logger.warning("从 LanceDB 加载 evidence 失败: %s", exc)

        return nodes, edges, evidence

    def upsert_node(self, node: Any) -> None:
        """upsert 单个节点：先按 node_id 删除再写入。"""
        record = {
            "node_id": [node.node_id],
            "type": [node.type],
            "name": [node.name],
            "aliases": [_safe_json_dumps(node.aliases)],
            "description": [node.description],
            "attributes": [_safe_json_dumps(node.attributes)],
            "status": [node.status],
            "source": [node.source],
            "created_at": [node.created_at],
            "updated_at": [node.updated_at],
        }
        self._upsert_rows(self.NODES_TABLE, "node_id", node.node_id, record, self._nodes_schema())

    def upsert_edge(self, edge: Any) -> None:
        """upsert 单个边：先按 edge_id 删除再写入。"""
        record = {
            "edge_id": [edge.edge_id],
            "relation": [edge.relation],
            "subjects": [_safe_json_dumps(edge.subjects)],
            "objects": [_safe_json_dumps(edge.objects)],
            "qualifiers": [_safe_json_dumps(edge.qualifiers)],
            "dimensions": [_safe_json_dumps(edge.dimensions)],
            "primary_dimension": [edge.primary_dimension],
            "route_layer": [edge.route_layer],
            "time_stage": [edge.time_stage],
            "confidence": [edge.confidence],
            "status": [edge.status],
            "evidence_ids": [_safe_json_dumps(edge.evidence_ids)],
            "counter_evidence_ids": [_safe_json_dumps(edge.counter_evidence_ids)],
            "source": [edge.source],
            "created_at": [edge.created_at],
            "updated_at": [edge.updated_at],
        }
        self._upsert_rows(self.EDGES_TABLE, "edge_id", edge.edge_id, record, self._edges_schema())

    def upsert_evidence(self, evidence: Any) -> None:
        """upsert 单个证据：先按 evidence_id 删除再写入。"""
        record = {
            "evidence_id": [evidence.evidence_id],
            "source_type": [evidence.source_type],
            "source_title": [evidence.source_title],
            "source_id": [evidence.source_id],
            "document_id": [evidence.document_id],
            "source_url": [evidence.source_url],
            "publish_date": [evidence.publish_date],
            "quote": [evidence.quote],
            "summary": [evidence.summary],
            "evidence_level": [evidence.evidence_level],
            "supports": [_safe_json_dumps(evidence.supports)],
            "contradicts": [_safe_json_dumps(evidence.contradicts)],
            "freshness": [evidence.freshness],
            "extracted_by": [evidence.extracted_by],
            "verified_by": [evidence.verified_by],
            "metadata": [_safe_json_dumps(evidence.metadata)],
            "event_date": [evidence.event_date],
            "causal_status": [evidence.causal_status],
        }
        self._upsert_rows(
            self.EVIDENCE_TABLE, "evidence_id", evidence.evidence_id,
            record, self._evidence_schema(),
        )

    def _upsert_rows(
        self,
        table_name: str,
        key_field: str,
        key_value: str,
        record: dict[str, list],
        schema: Any,
    ) -> None:
        """执行 upsert：先 delete 主键，再 add 新行。

        Args:
            table_name: LanceDB 表名。
            key_field: 主键字段名。
            key_value: 主键值。
            record: 待写入的记录字典（值为 list 形式）。
            schema: Arrow schema。
        """
        try:
            table = self._db.open_table(table_name)
            table.delete(f"{key_field} = '{_escape_sql(key_value)}'")
            arrow_table = self._pa.table(record, schema=schema)
            table.add(arrow_table)
        except Exception as exc:
            logger.warning("upsert %s 失败 key=%s: %s", table_name, key_value, exc)


class _JsonGraphPersistence:
    """GraphStore 的 JSON 文件持久化降级方案。

    LanceDB 不可用时使用，将整个 GraphStore 序列化为单一 JSON 文件。
    每次写入时全量覆盖，适合小规模数据。

    Args:
        json_path: JSON 文件路径。
    """

    def __init__(self, json_path: str | Path) -> None:
        self._json_path = Path(json_path)
        self._json_path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """从 JSON 文件加载全部数据。"""
        from core.research_graph.models import Evidence, HyperEdge, HyperNode

        if not self._json_path.exists():
            return {}, {}, {}
        try:
            payload = json.loads(self._json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("加载 JSON 持久化文件失败: %s", exc)
            return {}, {}, {}

        nodes = {
            key: HyperNode.from_dict(value)
            for key, value in payload.get("nodes", {}).items()
        }
        edges = {
            key: HyperEdge.from_dict(value)
            for key, value in payload.get("edges", {}).items()
        }
        evidence = {
            key: Evidence.from_dict(value)
            for key, value in payload.get("evidence", {}).items()
        }
        return nodes, edges, evidence

    def upsert_node(self, node: Any) -> None:
        """JSON 持久化忽略单条 upsert，由 save_full 统一保存。"""
        # no-op：JSON 方案采用全量保存，单条 upsert 不触发 IO
        return None

    def upsert_edge(self, edge: Any) -> None:
        """JSON 持久化忽略单条 upsert，由 save_full 统一保存。"""
        return None

    def upsert_evidence(self, evidence: Any) -> None:
        """JSON 持久化忽略单条 upsert，由 save_full 统一保存。"""
        return None

    def save_full(self, store: Any) -> None:
        """全量保存 GraphStore 到 JSON 文件。

        Args:
            store: GraphStore 实例。
        """
        try:
            payload = store.to_dict()
            self._json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("保存 JSON 持久化文件失败: %s", exc)


class UnifiedResearchMemory:
    """统一研究记忆层：跨主题共享的持续演化世界模型。

    在同一进程中持有 GraphStore + NeuronStateStore + DocumentChunkStore，
    所有主题共享同一实例，消除记忆孤岛。

    Args:
        data_root: 数据根目录，LanceDB 文件存放于此。
        cache_max_size: W-TinyLFU 缓存最大条目数。
    """

    def __init__(
        self,
        data_root: str | Path = "data/research_memory",
        cache_max_size: int = 50000,
    ) -> None:
        self._data_root = Path(data_root)
        self._data_root.mkdir(parents=True, exist_ok=True)

        # W-TinyLFU 缓存层
        self._node_cache: WTinyLFUCache[str, Any] = WTinyLFUCache(
            max_size=cache_max_size, ttl_seconds=300,
        )
        self._adjacency_cache: WTinyLFUCache[str, list[str]] = WTinyLFUCache(
            max_size=cache_max_size // 2, ttl_seconds=60,
        )
        self._query_cache: WTinyLFUCache[str, list[Any]] = WTinyLFUCache(
            max_size=cache_max_size // 4, ttl_seconds=120,
        )

        # 惰性初始化的组件（按需加载，避免导入失败）
        self._graph_store: Any = None
        self._state_store: Any = None
        self._chunk_store: Any = None
        self._evidence_ledger: Any = None
        self._environment: Any = None
        self._entity_resolver: Any = None
        # GraphStore 持久化适配器（LanceDB 或 JSON 降级）
        self._graph_persistence: Any = None
        # 持久化模式："lance" | "json"
        self._persistence_mode: str = "lance"
        # 修复 3：environment 惰性初始化前的 pending 状态。
        # EnginePersistence.load_environment_state 返回的 dict 在 environment
        # 尚未创建时无法直接注入，暂存于此，由 _ensure_environment 在
        # cold_start 后消费。消费后置 None，避免重复注入。
        self._pending_environment_state: dict[str, Any] | None = None

        # 统计
        self._node_count = 0
        self._edge_count = 0
        self._evidence_count = 0
        self._topic_nodes: dict[str, set[str]] = {}  # topic → node_ids

    # ------------------------------------------------------------------
    # 组件初始化（惰性加载）
    # ------------------------------------------------------------------

    def _ensure_graph_store(self) -> Any:
        """惰性初始化 GraphStore 并从 LanceDB 加载已有数据。

        LanceDB 不可用时降级为 JSON 文件持久化（graph_store.json）。
        """
        if self._graph_store is None:
            from core.research_graph.store import GraphStore
            self._graph_store = GraphStore()
            self._init_graph_persistence()
            self._load_from_persistence()
            logger.info(
                "GraphStore 已初始化 (mode=%s, nodes=%d, edges=%d, evidence=%d)",
                self._persistence_mode,
                len(self._graph_store.nodes),
                len(self._graph_store.edges),
                len(self._graph_store.evidence),
            )
        return self._graph_store

    def _init_graph_persistence(self) -> None:
        """初始化 GraphStore 持久化适配器，优先 LanceDB，失败降级 JSON。"""
        lance_db_path = self._data_root / "graph_store.lance"
        try:
            self._graph_persistence = _LanceGraphPersistence(lance_db_path)
            self._persistence_mode = "lance"
            logger.debug("GraphStore 持久化使用 LanceDB: %s", lance_db_path)
        except Exception as exc:
            logger.warning(
                "LanceDB 不可用，降级到 JSON 持久化: %s", exc,
            )
            json_path = self._data_root / "graph_store.json"
            self._graph_persistence = _JsonGraphPersistence(json_path)
            self._persistence_mode = "json"

    def _load_from_persistence(self) -> None:
        """从持久化层加载已有节点/边/证据到内存 GraphStore。"""
        if self._graph_persistence is None or self._graph_store is None:
            return
        try:
            nodes, edges, evidence = self._graph_persistence.load_all()
        except Exception as exc:
            logger.warning("从持久化层加载 GraphStore 失败: %s", exc)
            return
        # 直接覆盖 GraphStore 的内存字典（保留原 mutations/verifier）
        self._graph_store.nodes.update(nodes)
        self._graph_store.edges.update(edges)
        self._graph_store.evidence.update(evidence)
        # 同步统计
        self._node_count = len(self._graph_store.nodes)
        self._edge_count = len(self._graph_store.edges)
        self._evidence_count = len(self._graph_store.evidence)
        self._topic_nodes = {}
        for node_id, node in self._graph_store.nodes.items():
            topic = str((node.attributes or {}).get("topic", ""))
            if topic:
                self._topic_nodes.setdefault(topic, set()).add(node_id)

    # ------------------------------------------------------------------
    # 持久化写入（upsert 到 LanceDB/JSON）
    # ------------------------------------------------------------------

    def _persist_node(self, node: Any) -> None:
        """将单个节点 upsert 到持久化层。

        Args:
            node: HyperNode 实例。
        """
        if self._graph_persistence is None:
            return
        try:
            self._graph_persistence.upsert_node(node)
        except Exception as exc:
            logger.warning("持久化节点失败 node_id=%s: %s", getattr(node, "node_id", "?"), exc)
        # JSON 模式下需要全量保存
        if self._persistence_mode == "json" and self._graph_store is not None:
            self._graph_persistence.save_full(self._graph_store)

    def _persist_edge(self, edge: Any) -> None:
        """将单个边 upsert 到持久化层。

        Args:
            edge: HyperEdge 实例。
        """
        if self._graph_persistence is None:
            return
        try:
            self._graph_persistence.upsert_edge(edge)
        except Exception as exc:
            logger.warning("持久化边失败 edge_id=%s: %s", getattr(edge, "edge_id", "?"), exc)
        if self._persistence_mode == "json" and self._graph_store is not None:
            self._graph_persistence.save_full(self._graph_store)

    def _persist_evidence(self, evidence: Any) -> None:
        """将单个证据 upsert 到持久化层。

        Args:
            evidence: Evidence 实例。
        """
        if self._graph_persistence is None:
            return
        try:
            self._graph_persistence.upsert_evidence(evidence)
        except Exception as exc:
            logger.warning(
                "持久化证据失败 evidence_id=%s: %s",
                getattr(evidence, "evidence_id", "?"), exc,
            )
        if self._persistence_mode == "json" and self._graph_store is not None:
            self._graph_persistence.save_full(self._graph_store)

    def _ensure_state_store(self) -> Any:
        """惰性初始化 NeuronStateStore。"""
        if self._state_store is None:
            from core.research_graph.neuron.state_store import NeuronStateStore
            db_path = str(self._data_root / "neuron_state.lance")
            self._state_store = NeuronStateStore(db_path=db_path)
            logger.info("NeuronStateStore 已初始化: %s", db_path)
        return self._state_store

    def _ensure_chunk_store(self) -> Any:
        """惰性初始化 DocumentChunkStore。"""
        if self._chunk_store is None:
            from core.research_graph.vector_memory import (
                DocumentChunkStore,
                LocalHashEmbeddingProvider,
            )
            db_path = str(self._data_root / "research_chunks.lance")
            self._chunk_store = DocumentChunkStore(
                db_path=db_path,
                embedding_provider=LocalHashEmbeddingProvider(dimension=128),
            )
            logger.info("DocumentChunkStore 已初始化: %s", db_path)
        return self._chunk_store

    def _ensure_evidence_ledger(self) -> Any:
        """惰性初始化 EvidenceLedger。"""
        if self._evidence_ledger is None:
            from core.research_graph.evidence_ledger import EvidenceLedger
            store = self._ensure_graph_store()
            self._evidence_ledger = EvidenceLedger(store=store)
            logger.info("EvidenceLedger 已初始化")
        return self._evidence_ledger

    def _sync_graph_edges_to_neurons(self) -> int:
        """将 GraphStore 中的有向边镜像为神经元突触。

        GraphStore 与 NeuronStateStore 是两套持久化表；如果只写前者，tick
        只能处理孤立神经元，无法沿研究图传播。该同步是幂等的，未知关系
        保留为空 relation，不会伪造合法的神经元关系。
        """
        if self._environment is None:
            return 0
        from core.research_graph.neuron.enums import SynapseTargetKind
        from core.research_graph.neuron.models import ResearchSynapse

        store = self._ensure_graph_store()
        state_store = self._environment.state_store
        existing = {
            syn.synapse_id
            for syn in state_store.get_all_synapses()
            if getattr(syn, "synapse_id", "")
        }
        created = 0
        for edge in store.edges.values():
            relation = str(edge.relation or "")
            synapse_id = f"graph_edge:{edge.edge_id}"
            if synapse_id in existing:
                continue
            for subject_id in edge.subjects:
                for object_id in edge.objects:
                    if not subject_id or not object_id:
                        continue
                    if state_store.get_neuron(subject_id) is None:
                        continue
                    if state_store.get_neuron(object_id) is None:
                        continue
                    synapse = ResearchSynapse(
                        synapse_id=synapse_id,
                        source_neuron_id=subject_id,
                        target_kind=SynapseTargetKind.EXISTING_NEURON.value,
                        target_neuron_id=object_id,
                        dimension=str(edge.primary_dimension or ""),
                        relation=relation,
                        confidence=0.0,
                    )
                    try:
                        state_store.save_synapse(synapse)
                    except ValueError:
                        logger.warning("无法同步图边为突触: edge_id=%s", edge.edge_id)
                        continue
                    created += 1
                    existing.add(synapse_id)
        return created

    def _ensure_environment(self) -> Any:
        """惰性初始化 ResearchGraphEnvironment。

        初始化后：
        1. 注入 no-op growth_mining_engine_factory，避免 tick 时脉冲处理因无 factory 失败
           （返回空 GrowthMiningResult，表示不执行网络搜索，仅本地推理）
        2. 调用 cold_start() 为已有图节点创建神经元，确保 tick 能处理神经元 > 0
        3. 修复 3：若存在 pending environment state（从 checkpoint 恢复），
           在 cold_start 后注入，重建 _evidence_pool / _evidence_stance /
           _neuron_evidence_map，避免重启后 EvidenceLedgerReaderAdapter 查询为空。
        """
        if self._environment is None:
            from core.research_graph.neuron.environment import (
                EnvironmentBudgetConfig,
                ResearchGraphEnvironment,
            )
            state_store = self._ensure_state_store()
            graph_store = self._ensure_graph_store()
            evidence_ledger = self._ensure_evidence_ledger()
            self._environment = ResearchGraphEnvironment(
                state_store=state_store,
                budget_config=EnvironmentBudgetConfig(),
                graph_store=graph_store,
                evidence_ledger=evidence_ledger,
            )
            # 注入 no-op factory：不执行网络搜索，仅本地推理
            self._environment.growth_mining_engine_factory = (
                self._create_noop_growth_mining_factory()
            )
            logger.info("ResearchGraphEnvironment 已初始化")
            # 冷启动：为已有图节点创建神经元，确保 tick 时处理神经元数 > 0
            try:
                cold_start_result = self._environment.cold_start()
                synced_synapses = self._sync_graph_edges_to_neurons()
                logger.info(
                    "ResearchGraphEnvironment 冷启动: neurons_created=%d, neurons_activated=%d, synapses_synced=%d",
                    cold_start_result.get("neurons_created", 0),
                    cold_start_result.get("neurons_activated", 0),
                    synced_synapses,
                )
            except Exception as exc:
                logger.warning("Environment cold_start 失败: %s", exc)
            # 修复 3：消费 pending environment state
            if self._pending_environment_state is not None:
                try:
                    restore_stats = (
                        self._environment.import_evidence_routing_state(
                            self._pending_environment_state,
                            graph_store=graph_store,
                        )
                    )
                    logger.info(
                        "Environment checkpoint 恢复: evidence=%d, stance=%d, "
                        "neuron_map=%d, active_neurons=%d",
                        restore_stats["evidence_restored"],
                        restore_stats["stance_restored"],
                        restore_stats["neuron_map_restored"],
                        restore_stats["active_neurons_merged"],
                    )
                except Exception as exc:
                    logger.warning(
                        "Environment checkpoint 恢复失败: %s", exc,
                    )
                self._pending_environment_state = None
        return self._environment

    def set_pending_environment_state(self, state: dict[str, Any] | None) -> None:
        """暂存 environment checkpoint 状态，在 _ensure_environment 后注入。

        修复 3：EnginePersistence.load_environment_state 返回的 dict 在
        environment 尚未创建时无法直接注入（environment 是惰性初始化）。
        调用方在 resume 时把 state 暂存于此，_ensure_environment 在
        cold_start 后消费并置 None，避免重复注入。

        Args:
            state: export_evidence_routing_state 返回的 dict；None 清空暂存。
        """
        self._pending_environment_state = state

    @staticmethod
    def _create_noop_growth_mining_factory() -> Any:
        """创建 no-op growth_mining_engine_factory。

        返回的 factory 接受 GrowthMiningRequest 但不执行网络搜索，
        返回空 GrowthMiningResult（support_evidence_count=0），
        表示仅本地推理，不产生新证据。

        Returns:
            可调用对象 (request) -> GrowthMiningResult。
        """
        from core.research_graph.growth_mining import GrowthMiningResult

        def factory(request: Any) -> Any:
            """no-op 工厂函数：返回空结果，不执行网络搜索。"""
            return GrowthMiningResult(
                run_dir=Path(""),
                validation_summary={},
                run_status="NOOP",
                support_evidence_count=0,
            )

        return factory

    def _register_node_as_neuron(self, node: Any) -> None:
        """为单个图节点创建神经元（若不存在）。

        在 environment 已初始化后，新增图节点时调用此方法，
        确保 tick 能处理新节点对应的神经元。

        Args:
            node: HyperNode 实例。
        """
        if self._environment is None:
            return
        try:
            existing = self._environment.state_store.get_neuron(node.node_id)
            if existing is not None:
                return  # 已存在，不重复创建
            from core.research_graph.neuron.enums import VerificationStatus
            from core.research_graph.neuron.models import ResearchNeuron

            neuron = ResearchNeuron(
                neuron_id=node.node_id,
                node_id=node.node_id,
                node_type=node.type,
                canonical_name=node.name,
                aliases=list(node.aliases),
                verification_status=(
                    VerificationStatus.UNVERIFIED_SEED.value
                    if node.source == "seed_graph"
                    else VerificationStatus.CANDIDATE.value
                ),
            )
            self._environment.state_store.save_neuron(neuron)
            self._environment.evidence_index.index_neuron(neuron)
            self._environment._activate_neuron(neuron.neuron_id)
            logger.debug("为节点创建神经元: %s (node_id=%s)", node.name, node.node_id)
        except Exception as exc:
            logger.warning(
                "为节点创建神经元失败 node_id=%s: %s",
                getattr(node, "node_id", "?"), exc,
            )

    # ------------------------------------------------------------------
    # 统一 API：节点操作
    # ------------------------------------------------------------------

    def add_node(
        self,
        name: str,
        node_type: str = "CONCEPT",
        topic: str = "",
        aliases: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
        source: str = "seed_graph",
    ) -> str:
        """添加或更新节点到统一记忆。跨主题共享同一节点。

        Args:
            name: 节点名称（canonical）。
            node_type: 节点类型。
            topic: 发现该节点的主题（用于溯源，不隔离）。
            aliases: 别名列表。
            attributes: 额外属性。

        Returns:
            节点 ID。
        """
        import hashlib
        node_id = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]

        store = self._ensure_graph_store()
        from core.research_graph.models import HyperNode
        from core.research_graph.schema import NodeSource

        # 检查是否已存在
        if node_id in store.nodes:
            # 已存在：更新 topic 归属，不重复创建
            if topic:
                self._topic_nodes.setdefault(topic, set()).add(node_id)
            self._node_cache.put(node_id, store.nodes[node_id])
            return node_id

        # 新建节点：status 由 GraphStore.add_node 根据 source 自动设置
        # （SEED_GRAPH → UNVERIFIED_SEED, LLM → CANDIDATE, MANUAL → ACTIVE）
        node_attributes = dict(attributes or {})
        if topic:
            node_attributes.setdefault("topic", topic)
        node = HyperNode(
            node_id=node_id,
            type=node_type,
            name=name,
            source=source or NodeSource.SEED_GRAPH.value,
            aliases=aliases or [],
            attributes=node_attributes,
        )
        store.add_node(node)
        self._node_count += 1
        if topic:
            self._topic_nodes.setdefault(topic, set()).add(node_id)

        # 同步持久化到 LanceDB/JSON
        self._persist_node(node)

        # 更新缓存
        self._node_cache.put(node_id, node)
        self._adjacency_cache.invalidate(node_id)

        # 若 environment 已初始化，为新节点创建神经元，避免 tick 空转
        self._register_node_as_neuron(node)

        logger.debug("添加节点: %s (topic=%s)", name, topic)
        return node_id

    def get_node(self, node_id: str) -> Any | None:
        """查询节点。优先从缓存读取。"""
        cached = self._node_cache.get(node_id)
        if cached is not None:
            return cached

        store = self._ensure_graph_store()
        node = store.nodes.get(node_id)
        if node is not None:
            self._node_cache.put(node_id, node)
        return node

    def find_node_by_name(self, name: str) -> Any | None:
        """按名称查找节点（全局，跨主题）。"""
        store = self._ensure_graph_store()
        for node in store.nodes.values():
            if node.name == name or name in (node.aliases or []):
                self._node_cache.put(node.node_id, node)
                return node
        return None

    # ------------------------------------------------------------------
    # 统一 API：边操作
    # ------------------------------------------------------------------

    def add_edge(
        self,
        subject_id: str,
        object_id: str,
        relation: str,
        qualifiers: dict[str, Any] | None = None,
        topic: str = "",
        source: str = "manual",
    ) -> str:
        """添加超边到统一记忆。

        Args:
            subject_id: 主体节点 ID。
            object_id: 客体节点 ID。
            relation: 关系类型。
            qualifiers: 限定条件（时间、地区、约束等）。
            topic: 发现该边的主题。

        Returns:
            边 ID。
        """
        import hashlib
        edge_id = hashlib.sha1(
            f"{subject_id}|{relation}|{object_id}".encode("utf-8")
        ).hexdigest()[:16]

        store = self._ensure_graph_store()
        from core.research_graph.models import HyperEdge

        if edge_id in store.edges:
            return edge_id  # 已存在不重复创建

        # status 由 GraphStore.add_edge 通过 verifier 自动设置
        edge_qualifiers = dict(qualifiers or {})
        if topic:
            edge_qualifiers.setdefault("source_topic", topic)
        edge = HyperEdge(
            edge_id=edge_id,
            relation=relation,
            subjects=[subject_id],
            objects=[object_id],
            qualifiers=edge_qualifiers,
            source=source or "manual",
        )
        edge, _verification_result = store.add_edge(edge)
        self._edge_count += 1

        # 若神经元环境已经启动，立即镜像该边；否则在 cold_start 后统一补齐。
        if self._environment is not None:
            self._sync_graph_edges_to_neurons()

        # 同步持久化到 LanceDB/JSON
        self._persist_edge(edge)

        # 失效邻接缓存
        self._adjacency_cache.invalidate(subject_id)
        self._adjacency_cache.invalidate(object_id)

        logger.debug("添加边: %s --%s--> %s (topic=%s)", subject_id, relation, object_id, topic)
        return edge_id

    def get_neighbors(self, node_id: str, direction: str = "both") -> list[str]:
        """查询节点邻居（带缓存）。"""
        cache_key = f"{node_id}:{direction}"
        cached = self._adjacency_cache.get(cache_key)
        if cached is not None:
            return cached

        store = self._ensure_graph_store()
        neighbors: list[str] = []
        for edge in store.edges.values():
            if direction in ("out", "both") and node_id in edge.subjects:
                neighbors.extend(edge.objects)
            if direction in ("in", "both") and node_id in edge.objects:
                neighbors.extend(edge.subjects)

        result = list(set(neighbors))
        self._adjacency_cache.put(cache_key, result)
        return result

    def recall_graph_context(
        self,
        node_name: str,
        hops: int = 2,
    ) -> dict[str, Any]:
        """从指定节点出发，BFS 遍历 hops 跳内的图上下文。

        用于多跳联想扩散检索：给定起始节点名称，返回其 hops 跳邻域内的
        所有节点和连接边，供下游 LLM/检索器构造扩展上下文。

        Args:
            node_name: 起始节点名称（canonical 或 alias）。
            hops: BFS 最大跳数，默认 2 跳。

        Returns:
            包含 start_node、nodes、edges 的字典：
            - start_node: 起始节点 HyperNode 或 None（未找到时）
            - nodes: list[HyperNode]，邻域内所有节点（含起点）
            - edges: list[HyperEdge]，邻域内所有连接边
        """
        store = self._ensure_graph_store()
        start_node = self.find_node_by_name(node_name)
        if start_node is None:
            return {"start_node": None, "nodes": [], "edges": []}

        # BFS 遍历
        visited_node_ids: set[str] = {start_node.node_id}
        visited_edge_ids: set[str] = set()
        current_frontier: set[str] = {start_node.node_id}

        for _hop in range(max(1, hops)):
            next_frontier: set[str] = set()
            # 遍历所有边，找到连接当前 frontier 节点的边
            for edge in store.edges.values():
                # 跳过已收集的边
                if edge.edge_id in visited_edge_ids:
                    continue
                # 检查边是否与当前 frontier 相连
                frontier_subjects = set(edge.subjects) & current_frontier
                frontier_objects = set(edge.objects) & current_frontier
                if not frontier_subjects and not frontier_objects:
                    continue
                # 收集这条边及其两端节点
                visited_edge_ids.add(edge.edge_id)
                for nid in edge.subjects + edge.objects:
                    if nid not in visited_node_ids:
                        visited_node_ids.add(nid)
                        next_frontier.add(nid)
            if not next_frontier:
                break
            current_frontier = next_frontier

        # 收集结果
        nodes = [
            store.nodes[nid]
            for nid in visited_node_ids
            if nid in store.nodes
        ]
        edges = [
            store.edges[eid]
            for eid in visited_edge_ids
            if eid in store.edges
        ]
        return {"start_node": start_node, "nodes": nodes, "edges": edges}

    # ------------------------------------------------------------------
    # 统一 API：证据操作
    # ------------------------------------------------------------------

    def add_evidence(self, evidence: Any) -> Any:
        """添加证据到统一记忆。

        Args:
            evidence: Evidence 对象。

        Returns:
            已添加的 Evidence 对象。
        """
        ledger = self._ensure_evidence_ledger()
        result = ledger.add(evidence)
        self._evidence_count += 1
        # 同步持久化证据到 LanceDB/JSON
        self._persist_evidence(result)
        # 证据到达后失效相关缓存
        self._node_cache.invalidate_all()
        self._adjacency_cache.invalidate_all()
        return result

    def route_evidence(self, evidence: Any, topic: str = "") -> None:
        """将新证据路由给相关神经元。

        Args:
            evidence: Evidence 对象。
            topic: 证据来源主题。
        """
        env = self._ensure_environment()
        env.route_evidence(evidence)

    def attach_evidence_to_edge(
        self,
        edge_id: str,
        evidence_id: str,
        *,
        as_counter: bool = False,
        operator: str = "rule_verifier",
    ) -> bool:
        """将证据绑定到指定边（追加到 edge.evidence_ids）。

        通过 GraphStore.attach_evidence 实现，同时触发 verifier 重新评估
        边的 confidence/status。绑定后同步持久化边到 LanceDB/JSON。

        Args:
            edge_id: 目标边 ID。
            evidence_id: 待绑定的证据 ID（需已通过 add_evidence 写入）。
            as_counter: True 时作为反证绑定到 counter_evidence_ids。
            operator: 操作者标识，用于 mutation log。

        Returns:
            True 表示绑定成功；False 表示边或证据不存在。
        """
        store = self._ensure_graph_store()
        edge = store.edges.get(edge_id)
        if edge is None:
            logger.warning("绑定证据失败：边不存在 edge_id=%s", edge_id)
            return False
        if evidence_id not in store.evidence:
            logger.warning(
                "绑定证据失败：证据不存在 evidence_id=%s", evidence_id,
            )
            return False

        try:
            store.attach_evidence(
                edge_id, evidence_id, as_counter=as_counter, operator=operator,
            )
            # 同步持久化边（evidence_ids 已变更）
            self._persist_edge(store.edges[edge_id])
            # 失效邻接缓存（边的 confidence/status 可能变化）
            self._adjacency_cache.invalidate(edge.subjects[0] if edge.subjects else "")
            self._adjacency_cache.invalidate(edge.objects[0] if edge.objects else "")
            logger.debug(
                "证据绑定到边: edge_id=%s, evidence_id=%s, as_counter=%s",
                edge_id, evidence_id, as_counter,
            )
            return True
        except Exception as exc:
            logger.warning(
                "绑定证据到边失败 edge_id=%s evidence_id=%s: %s",
                edge_id, evidence_id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # 统一 API：文档记忆
    # ------------------------------------------------------------------

    def upsert_document(
        self,
        doc_id: str,
        content: str,
        source_type: str = "unknown",
        title: str = "",
        url: str = "",
        topic: str = "",
    ) -> None:
        """将文档写入向量记忆，支持跨主题召回。

        Args:
            doc_id: 文档唯一 ID。
            content: 文档正文。
            source_type: 来源类型。
            title: 标题。
            url: 原始 URL。
            topic: 主题。
        """
        from core.research_graph.vector_memory import DocumentChunk

        chunk_store = self._ensure_chunk_store()
        # DocumentChunk 没有 title/url 字段，放入 metadata
        metadata: dict[str, Any] = {}
        if title:
            metadata["title"] = title
        if url:
            metadata["url"] = url
        chunk = DocumentChunk(
            chunk_id=doc_id,
            document_id=doc_id,
            text=content,
            source_type=source_type,
            topic=topic,
            metadata=metadata,
        )
        chunk_store.upsert_chunks([chunk])
        # 新文档必须立即使召回缓存失效，否则同一进程内最多 120 秒看不到新增材料。
        self._query_cache.invalidate_all()

    def recall_documents(
        self,
        query: str,
        limit: int = 10,
        topic: str = "",
    ) -> list[Any]:
        """从向量记忆召回文档（跨主题）。

        Args:
            query: 查询文本。
            limit: 最大返回数。
            topic: 若指定，优先返回该主题的文档。

        Returns:
            RetrievedChunk 列表。
        """
        chunk_store = self._ensure_chunk_store()

        # 查询缓存
        cache_key = f"recall:{query}:{limit}:{topic}"
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            return cached

        results = chunk_store.search(query, limit=limit)

        # 如果指定了 topic，优先排序（RetrievedChunk.chunk.topic）
        if topic:
            results.sort(key=lambda r: (r.chunk.topic != topic, -r.vector_score))

        self._query_cache.put(cache_key, results)
        return results

    # ------------------------------------------------------------------
    # 统一 API：缺口分析
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 缺口识别辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date_string(date_str: str) -> datetime | None:
        """解析日期字符串为带时区的 datetime。

        支持 ISO 8601 格式，无时区时按 UTC 处理，解析失败返回 None。

        Args:
            date_str: 日期字符串（如 event_date 或 publish_date）。

        Returns:
            带时区的 datetime 对象，或 None。
        """
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _collect_node_evidence(node_id: str, store: Any) -> list[Any]:
        """收集与节点关联的所有 Evidence。

        通过节点的边收集证据：找到节点参与的所有边，
        再收集边上的 evidence_ids 和 counter_evidence_ids 对应的 Evidence。

        Args:
            node_id: 节点 ID。
            store: GraphStore 实例。

        Returns:
            Evidence 对象列表（去重）。
        """
        evidence_list: list[Any] = []
        seen_ids: set[str] = set()
        for edge in store.edges.values():
            if node_id not in edge.subjects and node_id not in edge.objects:
                continue
            for eid in edge.evidence_ids + edge.counter_evidence_ids:
                if eid and eid not in seen_ids and eid in store.evidence:
                    seen_ids.add(eid)
                    evidence_list.append(store.evidence[eid])
        return evidence_list

    @staticmethod
    def _latest_evidence_date(evidence_list: list[Any]) -> datetime | None:
        """从证据列表中提取最新日期（优先 event_date，回退 publish_date）。

        Args:
            evidence_list: Evidence 对象列表。

        Returns:
            最新的 datetime，或 None（无可用日期时）。
        """
        latest: datetime | None = None
        for ev in evidence_list:
            ev_date = UnifiedResearchMemory._parse_date_string(ev.event_date)
            if ev_date is None:
                ev_date = UnifiedResearchMemory._parse_date_string(ev.publish_date)
            if ev_date is not None and (latest is None or ev_date > latest):
                latest = ev_date
        return latest

    # ------------------------------------------------------------------
    # 5 种扩展缺口检测
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_causal_gaps(store: Any) -> list[dict[str, Any]]:
        """检测因果链缺环：有共同邻居但无直接连接的节点对。

        若节点 A 与 C 有共同邻居 B（A-B、B-C 存在）但 A-C 无直接边，
        则标记为因果链缺环，提示补充中间机制或直接因果证据。

        Args:
            store: GraphStore 实例。

        Returns:
            causal_gap 缺口列表。
        """
        gaps: list[dict[str, Any]] = []
        node_ids = list(store.nodes.keys())
        # 构建无向邻接表
        adjacency: dict[str, set[str]] = {nid: set() for nid in node_ids}
        for edge in store.edges.values():
            for s in edge.subjects:
                for o in edge.objects:
                    if s in adjacency and o in adjacency:
                        adjacency[s].add(o)
                        adjacency[o].add(s)

        seen_pairs: set[tuple[str, str]] = set()
        for a_id in node_ids:
            neighbors_a = adjacency[a_id]
            for b_id in neighbors_a:
                for c_id in adjacency[b_id]:
                    if c_id == a_id or c_id in neighbors_a:
                        continue
                    pair = (a_id, c_id) if a_id < c_id else (c_id, a_id)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    common = neighbors_a & adjacency[c_id]
                    if not common:
                        continue
                    node_a = store.nodes.get(a_id)
                    node_c = store.nodes.get(c_id)
                    if not node_a or not node_c:
                        continue
                    common_count = len(common)
                    severity = min(0.8, 0.3 + common_count * 0.1)
                    gaps.append({
                        "type": "causal_gap",
                        "node_id": a_id,
                        "node_name": node_a.name,
                        "related_node_id": c_id,
                        "related_node_name": node_c.name,
                        "common_neighbor_count": common_count,
                        "severity": round(severity, 2),
                        "description": (
                            f"节点 {node_a.name} 与 {node_c.name} 有 "
                            f"{common_count} 个共同邻居但无直接连接，"
                            f"可能缺失因果链中间环节"
                        ),
                    })
        return gaps

    @staticmethod
    def _detect_temporal_gaps(store: Any) -> list[dict[str, Any]]:
        """检测时间断层：节点最新证据超过 90 天。

        仅检测有证据的节点，无证据节点由其他缺口类型覆盖。

        Args:
            store: GraphStore 实例。

        Returns:
            temporal_gap 缺口列表。
        """
        gaps: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for node_id, node in store.nodes.items():
            evidence_list = UnifiedResearchMemory._collect_node_evidence(node_id, store)
            if not evidence_list:
                continue
            latest = UnifiedResearchMemory._latest_evidence_date(evidence_list)
            if latest is None:
                continue
            days_since = (now - latest).days
            if days_since > 90:
                severity = min(0.9, 0.4 + (days_since - 90) / 365 * 0.5)
                gaps.append({
                    "type": "temporal_gap",
                    "node_id": node_id,
                    "node_name": node.name,
                    "days_since_latest": days_since,
                    "severity": round(severity, 2),
                    "description": (
                        f"节点 {node.name} 最新证据距今 {days_since} 天，"
                        f"超过 90 天阈值，存在时间断层"
                    ),
                })
        return gaps

    @staticmethod
    def _detect_stale_verifications(store: Any) -> list[dict[str, Any]]:
        """检测过期重验：ACTIVE/TRUSTED 节点超过 180 天无新证据。

        Args:
            store: GraphStore 实例。

        Returns:
            stale_verification 缺口列表。
        """
        gaps: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        active_statuses = {"ACTIVE", "TRUSTED"}
        for node_id, node in store.nodes.items():
            if node.status not in active_statuses:
                continue
            evidence_list = UnifiedResearchMemory._collect_node_evidence(node_id, store)
            if not evidence_list:
                gaps.append({
                    "type": "stale_verification",
                    "node_id": node_id,
                    "node_name": node.name,
                    "days_since_latest": None,
                    "severity": 0.9,
                    "description": (
                        f"节点 {node.name} 状态为 {node.status} "
                        f"但完全无证据支撑，需立即重验"
                    ),
                })
                continue
            latest = UnifiedResearchMemory._latest_evidence_date(evidence_list)
            if latest is None:
                continue
            days_since = (now - latest).days
            if days_since > 180:
                severity = min(0.9, 0.5 + (days_since - 180) / 365 * 0.4)
                gaps.append({
                    "type": "stale_verification",
                    "node_id": node_id,
                    "node_name": node.name,
                    "days_since_latest": days_since,
                    "severity": round(severity, 2),
                    "description": (
                        f"节点 {node.name} 状态为 {node.status}，"
                        f"但最新证据距今 {days_since} 天，超过 180 天需重验"
                    ),
                })
        return gaps

    @staticmethod
    def _detect_company_mapping_gaps(store: Any) -> list[dict[str, Any]]:
        """检测公司映射缺口：MATERIAL/PRODUCT 节点无 COMPANY 供应关系。

        检查 MATERIAL/PRODUCT 类型节点是否有 SUPPLIES_TO 或 PRODUCES
        关系连接到 COMPANY 类型节点。

        Args:
            store: GraphStore 实例。

        Returns:
            company_mapping_gap 缺口列表。
        """
        gaps: list[dict[str, Any]] = []
        company_relations = {"SUPPLIES_TO", "PRODUCES"}
        for node_id, node in store.nodes.items():
            if node.type not in ("MATERIAL", "PRODUCT"):
                continue
            has_company_link = False
            for edge in store.edges.values():
                if edge.relation not in company_relations:
                    continue
                if node_id not in edge.subjects and node_id not in edge.objects:
                    continue
                other_ids = (
                    edge.objects if node_id in edge.subjects else edge.subjects
                )
                for other_id in other_ids:
                    other_node = store.nodes.get(other_id)
                    if other_node and other_node.type == "COMPANY":
                        has_company_link = True
                        break
                if has_company_link:
                    break
            if not has_company_link:
                gaps.append({
                    "type": "company_mapping_gap",
                    "node_id": node_id,
                    "node_name": node.name,
                    "severity": 0.6,
                    "description": (
                        f"{node.type} 节点 {node.name} 缺少到 COMPANY 的"
                        f"供应关系(SUPPLIES_TO/PRODUCES)"
                    ),
                })
        return gaps

    @staticmethod
    def _detect_source_lineage_gaps(store: Any) -> list[dict[str, Any]]:
        """检测来源血缘不足：Evidence 数量 < 2 或所有 Evidence 来自同一 source_type。

        Args:
            store: GraphStore 实例。

        Returns:
            source_lineage_gap 缺口列表。
        """
        gaps: list[dict[str, Any]] = []
        for node_id, node in store.nodes.items():
            evidence_list = UnifiedResearchMemory._collect_node_evidence(node_id, store)
            if not evidence_list:
                continue
            source_types = {
                ev.source_type for ev in evidence_list if ev.source_type
            }
            if len(evidence_list) < 2:
                severity = 0.7
                description = (
                    f"节点 {node.name} 仅有 {len(evidence_list)} 条证据，"
                    f"血缘不足，建议补充独立来源"
                )
            elif len(source_types) < 2:
                severity = 0.5
                description = (
                    f"节点 {node.name} 的 {len(evidence_list)} 条证据"
                    f"全来自同一来源类型 {source_types}，缺乏来源多样性"
                )
            else:
                continue
            gaps.append({
                "type": "source_lineage_gap",
                "node_id": node_id,
                "node_name": node.name,
                "evidence_count": len(evidence_list),
                "source_type_count": len(source_types),
                "severity": severity,
                "description": description,
            })
        return gaps

    def identify_gaps(self, max_gaps: int = 50) -> list[dict[str, Any]]:
        """识别统一记忆图中的认知缺口。

        缺口类型:
            - island: 孤岛节点（无任何边连接）
            - weak_edge: 弱边（无证据支撑）
            - conflict: 冲突边（有反证）
            - causal_gap: 因果链缺环（有共同邻居但无直接连接）
            - temporal_gap: 时间断层（最新证据超过 90 天）
            - stale_verification: 过期重验（ACTIVE/TRUSTED 节点超 180 天无新证据）
            - company_mapping_gap: 公司映射缺口（MATERIAL/PRODUCT 无 COMPANY 供应关系）
            - source_lineage_gap: 来源血缘不足（证据 < 2 或来源单一）

        Args:
            max_gaps: 最大返回缺口数，按 severity 降序截断。

        Returns:
            缺口列表，每项含 type, severity 及关联字段（node_id/edge_id 等）。
        """
        store = self._ensure_graph_store()
        gaps: list[dict[str, Any]] = []

        # 1. 孤岛节点
        node_edge_count: dict[str, int] = {}
        for edge in store.edges.values():
            for nid in edge.subjects + edge.objects:
                node_edge_count[nid] = node_edge_count.get(nid, 0) + 1

        for node_id, node in store.nodes.items():
            if node_edge_count.get(node_id, 0) == 0:
                gaps.append({
                    "type": "island",
                    "node_id": node_id,
                    "node_name": node.name,
                    "severity": 0.3,
                })

        # 2. 弱边（无证据支撑）
        for edge_id, edge in store.edges.items():
            if not edge.evidence_ids:
                gaps.append({
                    "type": "weak_edge",
                    "edge_id": edge_id,
                    "relation": edge.relation,
                    "severity": 0.5,
                })

        # 3. 冲突边（有反证）
        for edge_id, edge in store.edges.items():
            if edge.counter_evidence_ids:
                gaps.append({
                    "type": "conflict",
                    "edge_id": edge_id,
                    "relation": edge.relation,
                    "counter_count": len(edge.counter_evidence_ids),
                    "severity": 0.8,
                })

        # 4. 因果链缺环
        gaps.extend(self._detect_causal_gaps(store))
        # 5. 时间断层
        gaps.extend(self._detect_temporal_gaps(store))
        # 6. 过期重验
        gaps.extend(self._detect_stale_verifications(store))
        # 7. 公司映射缺口
        gaps.extend(self._detect_company_mapping_gaps(store))
        # 8. 来源血缘不足
        gaps.extend(self._detect_source_lineage_gaps(store))

        # 按严重度排序并截断
        gaps.sort(key=lambda g: g.get("severity", 0), reverse=True)
        return gaps[:max_gaps]

    # ------------------------------------------------------------------
    # 统一 API：环境 tick
    # ------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """执行一次神经元环境 tick，驱动自主学习。

        Returns:
            tick 结果摘要。
        """
        env = self._ensure_environment()
        return env.event_loop_tick()

    def cold_start_from_topic(self, topic: str) -> None:
        """从主题冷启动神经元网络。

        Args:
            topic: 种子主题。
        """
        env = self._ensure_environment()
        env.cold_start_from_topic(topic)

    # ------------------------------------------------------------------
    # 统计与快照
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """返回记忆层统计快照。"""
        store = self._graph_store
        return {
            "nodes": len(store.nodes) if store else 0,
            "edges": len(store.edges) if store else 0,
            "evidence": len(store.evidence) if store else 0,
            "topics": len(self._topic_nodes),
            "topic_nodes": {t: len(ns) for t, ns in self._topic_nodes.items()},
            "node_cache": self._node_cache.snapshot(),
            "adjacency_cache": self._adjacency_cache.snapshot(),
            "query_cache": self._query_cache.snapshot(),
        }

    @property
    def graph_store(self) -> Any:
        """暴露 GraphStore 供外部组件使用。"""
        return self._ensure_graph_store()

    @property
    def environment(self) -> Any:
        """暴露 ResearchGraphEnvironment 供外部组件使用。"""
        return self._ensure_environment()

    @property
    def chunk_store(self) -> Any:
        """暴露 DocumentChunkStore 供外部组件使用。"""
        return self._ensure_chunk_store()

    @property
    def entity_resolver(self) -> Any:
        """暴露 EntityResolver（惰性初始化）。"""
        if self._entity_resolver is None:
            try:
                from core.research_graph.entity_resolver import EntityResolver
                self._entity_resolver = EntityResolver()
            except Exception as exc:
                logger.warning("EntityResolver 初始化失败: %s", exc)
        return self._entity_resolver
