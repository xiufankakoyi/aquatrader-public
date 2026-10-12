"""DeepExpansionEngine:知识超图深度扩展引擎。

与 GrowthMiningEngine 的区别:
- GrowthMining:给定 topic+node+company,验证一个具体映射(收敛式)
- DeepExpansion:从 seed 出发,主动发现新节点/边/路径(发散式)

支持的 seed_type:
  - node:     从已有图节点出发,沿关系邻接扩展
  - topic:    从领域主题关键词出发,构建该领域产业链骨架
  - gap_fill: 扫描 GapAnalyzer 的缺口,对高缺口区域补充候选节点

扩展策略(BFS 受控扩展):
  - max_hops 控制深度(默认1)
  - 节点类型白名单允许的扩展方向
  - 不允许扩展到 ACTIVE/TRUSTED 已确认节点(避免重复)
  - 共享枢纽节点只保留回到 seed 的链路

输出:
  - 新节点/边默认 DISCOVERED 状态写入 CandidateStore
  - 每个候选携带 source_trace/first_discovered_by/confidence
  - write_policy 默认 persist_candidate_and_tasks

检索假设:
  - 扩展完成时为新节点生成轻量 QueryPrototype
  - 明确标记不可进入 Evidence Slot、不可参与 posterior
  - 不调用 LLM，不构建 BGE/FAISS 预热索引
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.research_graph.candidate_graph import (
    CandidateEdge,
    CandidateNode,
    CandidateStore,
)
from core.research_graph.candidate_schema import CandidateStatus
from core.research_graph.models import utc_now

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 产业链邻接关系模板:产品→材料/工艺/设备/公司 的常见关联方向
# 用于 topic 模式构建骨架,以及 node 模式推断邻接节点类型
# ---------------------------------------------------------------------------

INDUSTRY_CHAIN_TEMPLATES: dict[str, list[dict[str, str]]] = {
    "PRODUCT": [
        {"relation": "REQUIRES", "node_type": "MATERIAL", "name_pattern": "{name}关键材料", "confidence": 0.3},
        {"relation": "REQUIRES", "node_type": "PROCESS", "name_pattern": "{name}制造工艺", "confidence": 0.3},
        {"relation": "REQUIRES", "node_type": "EQUIPMENT", "name_pattern": "{name}生产设备", "confidence": 0.3},
        {"relation": "SUPPLY_CHAIN_OF", "node_type": "APPLICATION", "name_pattern": "{name}应用场景", "confidence": 0.25},
    ],
    "MATERIAL": [
        {"relation": "UPSTREAM_OF", "node_type": "PRODUCT", "name_pattern": "{name}下游产品", "confidence": 0.3},
        {"relation": "SUBSTITUTE_WITH", "node_type": "MATERIAL", "name_pattern": "{name}替代材料", "confidence": 0.2},
    ],
    "PROCESS": [
        {"relation": "REQUIRES", "node_type": "EQUIPMENT", "name_pattern": "{name}设备", "confidence": 0.3},
        {"relation": "UPSTREAM_OF", "node_type": "PRODUCT", "name_pattern": "{name}产物", "confidence": 0.25},
    ],
    "EQUIPMENT": [
        {"relation": "PRODUCES", "node_type": "PROCESS", "name_pattern": "{name}工艺", "confidence": 0.25},
    ],
    "APPLICATION": [
        {"relation": "DRIVES", "node_type": "PRODUCT", "name_pattern": "{name}上游产品", "confidence": 0.25},
    ],
}

# 主题→产业链骨架的初始映射(可扩展)
TOPIC_SEED_SKELETONS: dict[str, list[dict[str, Any]]] = {
    "HBM": [
        {"name": "HBM", "node_type": "PRODUCT", "central": True},
        {"name": "TSV硅通孔", "node_type": "PROCESS"},
        {"name": "CoWoS封装", "node_type": "PROCESS"},
        {"name": "DRAM晶圆", "node_type": "MATERIAL"},
        {"name": "键合设备", "node_type": "EQUIPMENT"},
        {"name": "光刻机", "node_type": "EQUIPMENT"},
        {"name": "HBM3E", "node_type": "PRODUCT"},
        {"name": "AI加速卡", "node_type": "APPLICATION"},
    ],
    "AI算力": [
        {"name": "AI算力", "node_type": "APPLICATION", "central": True},
        {"name": "GPU", "node_type": "PRODUCT"},
        {"name": "HBM", "node_type": "PRODUCT"},
        {"name": "先进封装", "node_type": "PROCESS"},
        {"name": "光模块", "node_type": "PRODUCT"},
        {"name": "液冷", "node_type": "PROCESS"},
    ],
    "固态电池": [
        {"name": "固态电池", "node_type": "PRODUCT", "central": True},
        {"name": "固态电解质", "node_type": "MATERIAL"},
        {"name": "锂金属负极", "node_type": "MATERIAL"},
        {"name": "硫化物路线", "node_type": "PROCESS"},
        {"name": "氧化物路线", "node_type": "PROCESS"},
        {"name": "等静压设备", "node_type": "EQUIPMENT"},
    ],
    "光通信": [
        {"name": "光通信", "node_type": "APPLICATION", "central": True},
        {"name": "光模块", "node_type": "PRODUCT"},
        {"name": "磷化铟", "node_type": "MATERIAL"},
        {"name": "激光芯片", "node_type": "PRODUCT"},
        {"name": "硅光技术", "node_type": "PROCESS"},
    ],
}


@dataclass
class ExpansionSeed:
    """扩展种子。"""

    seed_type: str  # node / topic / gap_fill
    seed_id: str = ""
    seed_name: str = ""
    node_type: str = "PRODUCT"
    max_hops: int = 1
    allowed_node_types: list[str] = field(default_factory=list)
    max_new_nodes: int = 30
    min_confidence: float = 0.2
    write_policy: str = "persist_candidate_and_tasks"


@dataclass
class ExpansionResult:
    """扩展结果摘要。"""

    seed_type: str
    seed_id: str
    new_nodes_count: int = 0
    new_edges_count: int = 0
    skipped_existing: int = 0
    write_policy: str = "persist_candidate_and_tasks"
    discovered_node_ids: list[str] = field(default_factory=list)
    discovered_edge_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retrieval_hypotheses_generated: int = 0
    # 分层搜索验证统计
    search_attempted_nodes: int = 0
    search_upgraded_nodes: int = 0
    search_documents_activated: int = 0
    search_tiers_executed: list[str] = field(default_factory=list)
    search_warnings: list[str] = field(default_factory=list)


def _make_candidate_id(prefix: str, *parts: str) -> str:
    """生成确定性候选 ID。"""
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    return f"cn_{prefix}_{digest}"


def _cname_id(name: str) -> str:
    """从名称生成简化 ID(中文+英文)。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", name.lower()).strip("_")
    return cleaned or "unknown"


class DeepExpansionEngine:
    """知识超图深度扩展引擎。

    从 seed 节点/主题/缺口出发,BFS 扩展新节点/边,
    以 DISCOVERED 状态持久化到 CandidateStore。
    """

    ENGINE_ID = "deep_expansion_v1"

    def __init__(
        self,
        candidate_store: CandidateStore,
        *,
        main_store: Any | None = None,
        chain_templates: dict[str, list[dict[str, str]]] | None = None,
        topic_skeletons: dict[str, list[dict[str, Any]]] | None = None,
        enable_query_prototypes: bool = True,
        tiered_searcher: Any | None = None,
        enable_tiered_search: bool = False,
        max_search_nodes: int = 10,
        research_repository: Any | None = None,
    ) -> None:
        """初始化扩展引擎。

        Args:
            candidate_store: 候选图谱存储。
            main_store: 主图存储（可选，用于去重检查）。
            chain_templates: 产业链邻接关系模板。
            topic_skeletons: 主题→骨架节点映射。
            enable_query_prototypes: 是否生成轻量检索假设；不调用 LLM/向量模型。
            tiered_searcher: TieredEvidenceSearcher 实例（可选）。
                传入后并在 enable_tiered_search=True 时，扩展完成会对新节点执行分层搜索验证。
            enable_tiered_search: 是否启用扩展后的分层搜索验证。
            max_search_nodes: 每层扩展最多对多少个新节点执行搜索验证。
            research_repository: ResearchRepository 实例（可选）。
                传入后用于读取 knowledge policy 的 importance_overrides，覆盖默认重要程度分级。
        """
        self.candidate_store = candidate_store
        self.main_store = main_store
        self.chain_templates = chain_templates or INDUSTRY_CHAIN_TEMPLATES
        self.topic_skeletons = topic_skeletons or TOPIC_SEED_SKELETONS
        self.enable_query_prototypes = enable_query_prototypes
        self.tiered_searcher = tiered_searcher
        self.enable_tiered_search = enable_tiered_search
        self.max_search_nodes = max(max_search_nodes, 1)

        # 读取 knowledge policy 的 importance_overrides，允许校准结果覆盖默认分级。
        self._importance_overrides: dict[str, str] = {}
        try:
            if research_repository is None:
                from core.research.control_plane.repository import ResearchRepository
                research_repository = ResearchRepository()
            knowledge_policy = research_repository.get_active_policy("knowledge")
            if knowledge_policy is not None:
                overrides = knowledge_policy.parameters.get("importance_overrides", {})
                if isinstance(overrides, dict):
                    self._importance_overrides = {str(k): str(v) for k, v in overrides.items()}
        except Exception as exc:
            logger.debug("读取 importance_overrides 失败，使用默认分级: %s", exc)

    def expand(self, seed: ExpansionSeed) -> ExpansionResult:
        """执行扩展,结果持久化到 CandidateStore。"""
        if seed.seed_type == "node":
            result = self._expand_from_node(seed)
        elif seed.seed_type == "topic":
            result = self._expand_from_topic(seed)
        elif seed.seed_type == "gap_fill":
            result = self._expand_from_gaps(seed)
        else:
            return ExpansionResult(
                seed_type=seed.seed_type,
                seed_id=seed.seed_id,
                warnings=[f"unknown seed_type: {seed.seed_type}"],
            )

        if self.enable_query_prototypes and result.discovered_node_ids:
            self._batch_generate_retrieval_hypotheses(result)

        # 扩展完成后对新节点执行分层搜索验证（如果启用）
        if self.enable_tiered_search and self.tiered_searcher is not None and result.discovered_node_ids:
            self._batch_tiered_search(result)

        return result

    def _batch_tiered_search(self, result: ExpansionResult) -> None:
        """对新发现的节点批量执行分层搜索验证。

        策略：
        - 优先对 central/高置信度节点搜索（topic 模式下标记为 central 的节点）
        - 限制最大搜索节点数，避免搜索爆炸
        - 每个节点独立调用 TieredEvidenceSearcher，实时升级状态
        - 统计搜索升级情况
        """
        node_ids = result.discovered_node_ids[: self.max_search_nodes]
        for node_id in node_ids:
            node = self.candidate_store.get_node(node_id)
            if node is None:
                continue
            # 跳过已 ACTIVE/TRUSTED 的节点（通常扩展不会直接产生，但防御性处理）
            if node.status in (
                CandidateStatus.ACTIVE.value,
                CandidateStatus.TRUSTED.value,
                CandidateStatus.REJECTED.value,
                CandidateStatus.PRUNED.value,
            ):
                continue

            # 预算来自 Retrieval Outcome 的边际效用策略，不再使用 15/10/5 硬编码。
            from core.research.control_plane.repository import ResearchRepository
            from core.research.control_plane.tier_orchestrator import AdaptiveSearchBudgetPolicy
            information_gain = float(
                (node.attributes or {}).get("expected_information_gain", 0.5) or 0.5
            )
            original_budget = self.tiered_searcher.max_total_documents
            self.tiered_searcher.max_total_documents = AdaptiveSearchBudgetPolicy(
                ResearchRepository()
            ).recommended_total_budget(
                original_budget,
                expected_information_gain=information_gain,
            )

            old_status = node.status
            try:
                search_result = self.tiered_searcher.search_for_node(node)
            except Exception as exc:
                logger.warning("Tiered search failed for node %s: %s", node_id, exc)
                result.search_warnings.append(f"{node_id}: {exc}")
                continue
            finally:
                # 恢复全局预算，避免影响下一次调用
                self.tiered_searcher.max_total_documents = original_budget

            result.search_attempted_nodes += 1
            result.search_documents_activated += search_result.documents_activated
            result.search_tiers_executed.extend(search_result.tiers_executed)

            # 刷新节点状态，判断是否发生升级（必须用 old_status，因为 node 对象会被 store 同步修改）
            refreshed = self.candidate_store.get_node(node_id)
            if refreshed is not None and refreshed.status != old_status:
                result.search_upgraded_nodes += 1

    def _batch_generate_retrieval_hypotheses(self, result: ExpansionResult) -> None:
        """生成轻量查询原型；没有 LLM、embedding 或 Evidence 写入。"""
        for node_id in result.discovered_node_ids:
            node = self.candidate_store.get_node(node_id)
            if node is None:
                continue
            prototypes = [
                f"{node.name} 官方 公告 关系证据",
                f"{node.name} 否认 澄清 未供货 终止",
                f"{node.name} 产业链 上下游 最新进展",
            ]
            attributes = dict(node.attributes or {})
            attributes["retrieval_hypotheses"] = [
                {
                    "hypothesis_id": f"rh_{node.candidate_id}_{idx}",
                    "candidate_id": node.candidate_id,
                    "query_prototype": text,
                    "reason": "候选缺口检索原型",
                    "generated_by": "rule",
                    "evidence_kind": "retrieval_hypothesis",
                    "eligible_for_posterior": False,
                    "eligible_for_evidence_slot": False,
                }
                for idx, text in enumerate(prototypes)
            ]
            self.candidate_store.update_node(node_id, attributes=attributes)
            result.retrieval_hypotheses_generated += len(prototypes)

    def _expand_from_node(self, seed: ExpansionSeed) -> ExpansionResult:
        """从已有图节点出发,沿关系邻接扩展。"""
        result = ExpansionResult(
            seed_type=seed.seed_type,
            seed_id=seed.seed_id,
            write_policy=seed.write_policy,
        )

        seed_name = seed.seed_name or seed.seed_id
        seed_type = seed.node_type or "PRODUCT"

        # 获取邻接模板
        templates = self.chain_templates.get(seed_type, self.chain_templates.get("PRODUCT", []))
        if seed.allowed_node_types:
            templates = [t for t in templates if t["node_type"] in seed.allowed_node_types]

        existing_names = {n.name for n in self.candidate_store.list_candidates(view="exploration")}
        # 也检查主图已有的节点名称
        if self.main_store is not None:
            try:
                for node in getattr(self.main_store, "list_nodes", lambda: [])():
                    existing_names.add(getattr(node, "name", ""))
            except Exception:
                pass

        frontier: list[tuple[str, str, str, int]] = [(seed.seed_id, seed_name, seed_type, 0)]
        visited: set[str] = {seed.seed_id}

        while frontier and len(result.discovered_node_ids) < seed.max_new_nodes:
            current_id, current_name, current_type, depth = frontier.pop(0)
            if depth >= seed.max_hops:
                continue
            templates = self.chain_templates.get(current_type, [])
            for tmpl in templates:
                if seed.allowed_node_types and tmpl["node_type"] not in seed.allowed_node_types:
                    continue
                conf = float(tmpl.get("confidence", 0.3))
                if conf < seed.min_confidence:
                    continue
                new_name = tmpl["name_pattern"].format(name=current_name)
                new_type = tmpl["node_type"]
                new_id = _make_candidate_id(_cname_id(new_type), _cname_id(new_name))
                if new_id in visited or new_name in existing_names:
                    result.skipped_existing += 1
                    continue
                visited.add(new_id)
                existing_names.add(new_name)

                node = CandidateNode(
                    candidate_id=new_id,
                    name=new_name,
                    node_type=new_type,
                    status=CandidateStatus.DISCOVERED.value,
                    confidence=round(conf * (0.8 ** depth), 4),
                    source_trace=[{
                        "engine": self.ENGINE_ID,
                        "strategy": "adjacent_relation",
                        "seed_id": seed.seed_id,
                        "seed_name": seed_name,
                        "from_node": current_id,
                        "relation": tmpl["relation"],
                        "depth": depth + 1,
                    }],
                    first_discovered_by=self.ENGINE_ID,
                )
                self.candidate_store.add_node(node)
                result.discovered_node_ids.append(new_id)
                result.new_nodes_count += 1

                edge_id = _make_candidate_id("edge", current_id, tmpl["relation"], new_id)
                edge = CandidateEdge(
                    candidate_id=edge_id,
                    subject_id=current_id,
                    object_id=new_id,
                    relation=tmpl["relation"],
                    status=CandidateStatus.DISCOVERED.value,
                    confidence=round(conf * (0.8 ** depth), 4),
                    source_trace=[{
                        "engine": self.ENGINE_ID,
                        "strategy": "adjacent_relation",
                        "seed_id": seed.seed_id,
                        "depth": depth + 1,
                    }],
                    first_discovered_by=self.ENGINE_ID,
                )
                self.candidate_store.add_edge(edge)
                result.discovered_edge_ids.append(edge_id)
                result.new_edges_count += 1

                frontier.append((new_id, new_name, new_type, depth + 1))

        return result

    def _expand_from_topic(self, seed: ExpansionSeed) -> ExpansionResult:
        """从领域主题构建产业链骨架。"""
        result = ExpansionResult(
            seed_type=seed.seed_type,
            seed_id=seed.seed_id,
            write_policy=seed.write_policy,
        )

        topic_key = seed.seed_name or seed.seed_id
        skeleton = self.topic_skeletons.get(topic_key)
        if skeleton is None:
            # 无预定义骨架时,用通用 PRODUCT 模板生成单层骨架
            skeleton = [
                {"name": topic_key, "node_type": seed.node_type or "PRODUCT", "central": True},
            ]
            for tmpl in self.chain_templates.get("PRODUCT", []):
                skeleton.append({
                    "name": tmpl["name_pattern"].format(name=topic_key),
                    "node_type": tmpl["node_type"],
                })

        existing_names = {n.name for n in self.candidate_store.list_candidates(view="exploration")}
        if self.main_store is not None:
            try:
                for node in getattr(self.main_store, "list_nodes", lambda: [])():
                    existing_names.add(getattr(node, "name", ""))
            except Exception:
                pass

        node_id_map: dict[str, str] = {}
        central_id = ""

        for item in skeleton:
            name = item["name"]
            ntype = item.get("node_type", "PRODUCT")
            is_central = item.get("central", False)
            nid = _make_candidate_id(_cname_id(ntype), _cname_id(name))
            node_id_map[name] = nid
            if is_central:
                central_id = nid

            if name in existing_names:
                result.skipped_existing += 1
                continue

            conf = 0.35 if is_central else 0.3
            # 产业链重要程度分级：central/核心产品 > 材料工艺设备 > 其他
            importance = "high" if is_central else self._infer_importance(ntype)
            node = CandidateNode(
                candidate_id=nid,
                name=name,
                node_type=ntype,
                status=CandidateStatus.DISCOVERED.value,
                confidence=conf,
                source_trace=[{
                    "engine": self.ENGINE_ID,
                    "strategy": "topic_skeleton",
                    "seed_id": seed.seed_id,
                    "topic": topic_key,
                    "central": is_central,
                }],
                first_discovered_by=self.ENGINE_ID,
                aliases=[topic_key] if is_central else [],
                attributes={"importance": importance, "is_central": is_central},
            )
            self.candidate_store.add_node(node)
            result.discovered_node_ids.append(nid)
            result.new_nodes_count += 1

        # 为中心节点到其他节点建立边
        if central_id:
            central_name = next((k for k, v in node_id_map.items() if v == central_id), topic_key)
            for item in skeleton:
                name = item["name"]
                nid = node_id_map.get(name)
                if nid == central_id or nid is None:
                    continue
                ntype = item.get("node_type", "PRODUCT")
                relation = self._infer_relation(ntype)
                edge_id = _make_candidate_id("edge", central_id, relation, nid)
                # 检查边是否已存在
                if self.candidate_store.get_edge(edge_id) is not None:
                    continue
                edge = CandidateEdge(
                    candidate_id=edge_id,
                    subject_id=central_id,
                    object_id=nid,
                    relation=relation,
                    status=CandidateStatus.DISCOVERED.value,
                    confidence=0.3,
                    source_trace=[{
                        "engine": self.ENGINE_ID,
                        "strategy": "topic_skeleton",
                        "topic": topic_key,
                    }],
                    first_discovered_by=self.ENGINE_ID,
                )
                self.candidate_store.add_edge(edge)
                result.discovered_edge_ids.append(edge_id)
                result.new_edges_count += 1

        # max_hops > 1 时,继续从骨架节点 BFS 扩展
        if seed.max_hops > 1:
            for item in skeleton:
                name = item["name"]
                nid = node_id_map.get(name)
                ntype = item.get("node_type", "PRODUCT")
                if nid is None or name in existing_names:
                    continue
                sub_seed = ExpansionSeed(
                    seed_type="node",
                    seed_id=nid,
                    seed_name=name,
                    node_type=ntype,
                    max_hops=seed.max_hops - 1,
                    allowed_node_types=seed.allowed_node_types,
                    max_new_nodes=seed.max_new_nodes - len(result.discovered_node_ids),
                    min_confidence=seed.min_confidence,
                    write_policy=seed.write_policy,
                )
                sub_result = self._expand_from_node(sub_seed)
                result.new_nodes_count += sub_result.new_nodes_count
                result.new_edges_count += sub_result.new_edges_count
                result.skipped_existing += sub_result.skipped_existing
                result.discovered_node_ids.extend(sub_result.discovered_node_ids)
                result.discovered_edge_ids.extend(sub_result.discovered_edge_ids)

        return result

    def _expand_from_gaps(self, seed: ExpansionSeed) -> ExpansionResult:
        """从 GapAnalyzer 的缺口扩展。当前为占位实现。"""
        return ExpansionResult(
            seed_type=seed.seed_type,
            seed_id=seed.seed_id,
            write_policy=seed.write_policy,
            warnings=["gap_fill mode: placeholder, no gaps analyzed yet"],
        )

    def _infer_importance(self, node_type: str) -> str:
        """根据节点类型推断产业链重要程度。

        优先查询 knowledge policy 校准产生的 importance_overrides：
        - 命中 "downgrade" 的 node_type 直接返回 "low"
        未命中时回退到与 industry_chain_graph_builder 的 type_weight 对齐的硬编码分级：
        - high: 核心产品/应用/平台/技术路线 (product_platform / technology_route / PRODUCT / APPLICATION)
        - medium: 材料、工艺、设备、组件、上游资源 (material / component / upstream_resource / MATERIAL / PROCESS / EQUIPMENT / COMPONENT)
        - low: 主题、投资逻辑、公司、未知、其他

        Args:
            node_type: 节点类型字符串。

        Returns:
            重要程度分级："high" / "medium" / "low"。
        """
        ntype_norm = (node_type or "").lower().replace(" ", "_")
        if self._importance_overrides.get(ntype_norm) == "downgrade":
            return "low"
        high_types = {
            "product", "application", "product_platform", "technology_route",
        }
        medium_types = {
            "material", "process", "equipment", "component", "upstream_resource",
        }
        if ntype_norm in high_types:
            return "high"
        if ntype_norm in medium_types:
            return "medium"
        return "low"

    @staticmethod
    def _infer_relation(node_type: str) -> str:
        """根据节点类型推断与中心节点的关系。"""
        mapping = {
            "MATERIAL": "REQUIRES",
            "PROCESS": "REQUIRES",
            "EQUIPMENT": "REQUIRES",
            "APPLICATION": "DRIVES",
            "PRODUCT": "SUBSTITUTE_WITH",
            "COMPANY": "PRODUCES",
            "COUNTRY": "LOCATED_IN",
            "INDICATOR": "MEASURED_BY",
            "POLICY": "INFLUENCES",
            "EVENT": "IMPACTS",
        }
        return mapping.get(node_type, "ASSOCIATED_WITH")
