"""EvidenceActivator: 新文档 → 候选图谱匹配 → 证据槽填充 → 贝叶斯状态升级。

核心职责:
1. 当新文档/证据进入系统时,在 CandidateGraph 中匹配相关候选节点/边
2. 为匹配到的候选填充 evidence_slots
3. 统一调用 BayesianStatusUpgrader 进行状态迁移;
   本模块不再维护固定阈值规则
4. 反证(stance="counter")由 BayesianStatusUpgrader 统一惩罚
5. 更新 last_seen_at 和 activation_score

匹配策略:
- 候选节点名称/别名出现在证据文本中 → 视为命中
- 候选边两端节点名同时出现 → 边命中
- 用字符 bigram Jaccard 做模糊匹配,避免硬编码词典

职责边界:
- EvidenceActivator 只负责“匹配”和“填槽”
- 状态迁移决策完全交给 BayesianStatusUpgrader
- Claim 级充分性判断由 ClaimPolicy 在 orchestrator 层处理
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from core.research_graph.candidate_graph import CandidateEdge, CandidateNode, CandidateStore
from core.research_graph.bayesian_status_upgrader import BayesianStatusUpgrader
from core.research_graph.candidate_schema import (
    SOURCE_QUALITY_BOOST,
    CandidateStatus,
)
from core.research_graph.keyword_match_index import KeywordMatchIndex
from core.research_graph.models import utc_now
from core.research_graph.retrieval_hypothesis import is_blocked_evidence_kind

logger = logging.getLogger(__name__)


def _tokenize_chars(text: str) -> set[str]:
    """轻量中文字符级 tokenize,用于模糊匹配。"""
    if not text:
        return set()
    # 中文按字,英文/数字按词
    tokens = re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z0-9]+", text.lower())
    return set(tokens)


def _char_bigram_jaccard(text_a: str, text_b: str) -> float:
    """字符 bigram Jaccard 相似度。"""
    tokens_a = _tokenize_chars(text_a)
    tokens_b = _tokenize_chars(text_b)
    if not tokens_a or not tokens_b:
        return 0.0
    # bigrams
    def bigrams(ts: set[str]) -> set[str]:
        items = sorted(ts)
        if len(items) < 2:
            return set(items)
        return {f"{items[i]}{items[i+1]}" for i in range(len(items) - 1)} | ts
    bg_a = bigrams(tokens_a)
    bg_b = bigrams(tokens_b)
    inter = len(bg_a & bg_b)
    union = len(bg_a | bg_b)
    return inter / union if union else 0.0


@dataclass
class ActivationResult:
    """激活结果摘要。"""

    matched_node_ids: list[str] = field(default_factory=list)
    matched_edge_ids: list[str] = field(default_factory=list)
    upgraded_nodes: list[tuple[str, str, str]] = field(default_factory=list)  # (id, old, new)
    upgraded_edges: list[tuple[str, str, str]] = field(default_factory=list)
    matched_count: int = 0
    conflicts: list[str] = field(default_factory=list)
    timing: dict[str, float] = field(default_factory=dict)


class EvidenceActivator:
    """证据激活器:新证据匹配候选并升级状态。"""

    # 名称匹配阈值
    NAME_MATCH_THRESHOLD = 0.5
    NAME_EXACT_MATCH_BONUS = 0.3
    PSEUDO_EVIDENCE_MATCH_THRESHOLD = 0.5
    PSEUDO_EVIDENCE_TOP_K = 10
    def __init__(
        self,
        candidate_store: CandidateStore,
        *,
        # 仅为旧调用签名兼容；始终忽略。
        use_pseudo_evidence: bool = False,
        use_bayesian: bool | None = None,
        bayesian_upgrader: BayesianStatusUpgrader | None = None,
        research_repository: Any | None = None,
        on_candidate_transition: Any | None = None,
        **legacy_options: Any,
    ) -> None:
        """初始化证据激活器。

        Args:
            candidate_store: 候选图谱存储。
            use_pseudo_evidence: 已废弃；检索假设永不参与匹配。
            use_bayesian: 已废弃；统一使用贝叶斯状态升级，不再支持固定阈值。
            bayesian_upgrader: 自定义贝叶斯升级器；为 None 时使用默认参数。
            research_repository: 研究仓库，用于读取 active policy。
            on_candidate_transition: 候选状态迁移回调，签名:
                (candidate_id, promotion_type, from_status, to_status,
                 evidence_slots, policy_version) -> None
        """
        self.candidate_store = candidate_store
        # 兼容旧参数但强制关闭：检索假设不能参与 Evidence 匹配或状态升级。
        if use_pseudo_evidence:
            logger.warning(
                "use_pseudo_evidence 已废弃并强制关闭；RetrievalHypothesis 仅用于生成查询"
            )

        if use_bayesian is False:
            logger.warning(
                "use_bayesian=False 已废弃；EvidenceActivator 统一使用贝叶斯状态升级"
            )
        self.use_bayesian = True
        self._source_quality_weights = dict(SOURCE_QUALITY_BOOST)
        active_knowledge_policy = None
        try:
            if research_repository is None:
                from core.research.control_plane.repository import ResearchRepository
                research_repository = ResearchRepository()
            active_knowledge_policy = research_repository.get_active_policy("knowledge")
        except Exception as exc:  # 策略库不可用时安全回退静态默认值
            logger.debug("读取知识校准策略失败，使用默认权重: %s", exc)
        if active_knowledge_policy is not None:
            learned_weights = active_knowledge_policy.parameters.get("source_weights", {})
            for source, weight in learned_weights.items():
                if isinstance(weight, (int, float)):
                    self._source_quality_weights[str(source)] = max(0.1, min(1.5, float(weight)))

        if bayesian_upgrader is None:
            thresholds = dict(BayesianStatusUpgrader.DEFAULT_CONFIDENCE_THRESHOLDS)
            kappa = 0.5
            if active_knowledge_policy is not None:
                learned_threshold = active_knowledge_policy.parameters.get("claim_activation_threshold")
                if isinstance(learned_threshold, (int, float)):
                    thresholds[CandidateStatus.SUPPORTED.value] = max(0.55, min(0.90, float(learned_threshold)))
                learned_kappa = active_knowledge_policy.parameters.get("source_discount_kappa")
                if isinstance(learned_kappa, (int, float)):
                    kappa = max(0.3, min(1.0, float(learned_kappa)))
            bayesian_upgrader = BayesianStatusUpgrader(
                confidence_thresholds=thresholds,
                source_discount_kappa=kappa,
            )
        self._bayesian_upgrader = bayesian_upgrader

        # 高性价比关键词倒排索引
        self._keyword_index = KeywordMatchIndex()
        self._keyword_warmed = False

        # 候选状态迁移回调（用于 FactOutcomeStore 记录候选晋升）
        self._on_candidate_transition = on_candidate_transition

        # 预热计时明细
        self.warmup_timing: dict[str, float] = {}

    def _ensure_keyword_index(self) -> None:
        """构建候选节点关键词倒排索引（仅一次）。"""
        if self._keyword_warmed:
            return
        nodes = self.candidate_store.list_candidates(view="exploration")
        self._keyword_index.build(nodes)
        self._keyword_warmed = True


    def activate(
        self,
        evidence_text: str,
        evidence_id: str,
        source_type: str = "unknown",
        *,
        stance: str = "support",
        event_date: str = "",
        source_url: str = "",
        source_lineage_id: str = "",
        published_at: str = "",
        retrieved_at: str = "",
        evidence_kind: str = "document",
        match_threshold: float | None = None,
        query_id: str = "",
        query_purpose: str = "",
    ) -> ActivationResult:
        """处理一条新证据,匹配候选并更新状态。

        Args:
            evidence_text: 证据文本内容。
            evidence_id: 证据唯一 ID。
            source_type: 来源类型(如 announcement/news/official)。
            stance: "support"、"counter" 或 "neutral"。
            event_date: 事件日期。
            source_url: 可追溯的原文地址。
            source_lineage_id: 独立来源/转载血缘标识。
            published_at: 原文发布时间。
            retrieved_at: 系统检索时间。
            evidence_kind: 证据类型。生成式检索假设不得进入证据槽。
            match_threshold: 名称匹配阈值,默认 NAME_MATCH_THRESHOLD。
            query_id: 触发本次检索的查询 ID(反证归因用)。
            query_purpose: 查询目的("support"/"counter"),用于保留反证归因。

        Returns:
            ActivationResult 摘要。
        """
        # LLM/模板生成内容只能用于构造查询，绝不能作为事实证据参与升级。
        if evidence_kind in {
            "pseudo_evidence",
            "retrieval_hypothesis",
            "query_prototype",
            "generated",
        }:
            return ActivationResult()

        if stance not in {"support", "counter", "neutral"}:
            stance = "neutral"

        threshold = match_threshold or self.NAME_MATCH_THRESHOLD
        result = ActivationResult()
        now = utc_now()
        source_quality = self._source_quality_weights.get(
            source_type, self._source_quality_weights.get("unknown", SOURCE_QUALITY_BOOST["unknown"])
        )

        # 高性价比关键词索引
        self._ensure_keyword_index()

        # 归一化 query_purpose，保留 support/counter 归因；缺失时安全降级为空串。
        purpose = str(query_purpose or "").lower()
        if purpose not in {"support", "counter"}:
            purpose = ""

        slot = {
            "evidence_id": evidence_id,
            "source_type": source_type,
            "stance": stance,
            "quality": source_quality,
            "event_date": event_date,
            "source_url": source_url,
            "source_lineage_id": source_lineage_id or evidence_id,
            "published_at": published_at,
            "retrieved_at": retrieved_at or now,
            "evidence_kind": evidence_kind,
            "activated_at": now,
            # 反证归因：保留查询来源与评估状态，禁止在合并 support/counter 时丢失 purpose。
            "query_id": query_id,
            "purpose": purpose,
            "evaluation_status": "evaluated",
            "evaluated_stance": stance,
        }

        # 1. 匹配候选节点：先用关键词索引快速过滤候选集
        t_kq = time.time()
        candidate_ids = self._keyword_index.query(evidence_text)
        if not candidate_ids:
            nodes = self.candidate_store.list_candidates(view="exploration")
        else:
            nodes = [
                self.candidate_store.get_node(cid)
                for cid in candidate_ids
                if self.candidate_store.get_node(cid) is not None
            ]
        t_keyword_query = time.time() - t_kq

        # 仅使用真实文本中的名称/别名匹配。
        node_updates: list[tuple[str, dict[str, Any]]] = []
        keyword_index_nodes: list[CandidateNode] = []
        for node in nodes:
            if node.status in (CandidateStatus.PRUNED.value, CandidateStatus.REJECTED.value, CandidateStatus.TRUSTED.value):
                continue
            score = self._match_node(node, evidence_text)
            if score < threshold:
                continue
            result.matched_node_ids.append(node.candidate_id)
            result.matched_count += 1

            existing_keys = {
                (s.get("evidence_id"), s.get("purpose", ""))
                for s in node.evidence_slots
            }
            if (evidence_id, purpose) not in existing_keys:
                slot_with_score = dict(slot)
                slot_with_score["match_score"] = round(score, 4)
                updated_slots = list(node.evidence_slots) + [slot_with_score]
            else:
                updated_slots = node.evidence_slots

            new_status = self._resolve_status_by_evidence(node.status, updated_slots)
            new_activation = self._compute_activation_score(updated_slots)

            old_status = node.status
            node_updates.append((node.candidate_id, {
                "evidence_slots": updated_slots,
                "status": new_status,
                "activation_score": round(new_activation, 4),
                "last_seen_at": now,
            }))
            keyword_index_nodes.append(node)
            if new_status != old_status:
                result.upgraded_nodes.append((node.candidate_id, old_status, new_status))
                if self._on_candidate_transition is not None:
                    try:
                        self._on_candidate_transition(
                            candidate_id=node.candidate_id,
                            promotion_type="candidate_node",
                            from_status=old_status,
                            to_status=new_status,
                            evidence_slots=updated_slots,
                            policy_version=getattr(self._bayesian_upgrader, "VERSION", "bayesian-v1"),
                        )
                    except Exception as exc:
                        logger.warning("候选节点迁移回调失败: %s", exc)
            if new_status == CandidateStatus.CONFLICTED.value:
                result.conflicts.append(node.candidate_id)

        # 批量写入节点,一次性触发 LanceDB 批量删除+添加
        t_persist_node = time.time()
        self.candidate_store.update_nodes_batch(node_updates)
        t_persist_node = time.time() - t_persist_node

        # 同步更新关键词索引中的节点状态
        for node in keyword_index_nodes:
            refreshed = self.candidate_store.get_node(node.candidate_id)
            if refreshed is not None:
                self._keyword_index.add_node(refreshed)

        # 2. 匹配候选边(两端节点均命中且文本表达关系语义才附加证据)
        edge_updates: list[tuple[str, dict[str, Any]]] = []
        matched_node_set = set(result.matched_node_ids)
        for edge in self.candidate_store.list_edges(view="exploration"):
            if edge.status in (CandidateStatus.PRUNED.value, CandidateStatus.REJECTED.value, CandidateStatus.TRUSTED.value):
                continue
            if edge.subject_id in matched_node_set and edge.object_id in matched_node_set:
                # 关系语义 grounding：文本必须表达 edge.relation 的语义，
                # 而非仅同时提到两个实体。relation 为空时安全降级跳过检查。
                if not self._edge_relation_grounded(edge.relation, evidence_text):
                    continue
                result.matched_edge_ids.append(edge.candidate_id)

                existing_keys = {
                    (s.get("evidence_id"), s.get("purpose", ""))
                    for s in edge.evidence_slots
                }
                if (evidence_id, purpose) not in existing_keys:
                    slot_with_score = dict(slot)
                    slot_with_score["match_score"] = 0.5
                    updated_slots = list(edge.evidence_slots) + [slot_with_score]
                else:
                    updated_slots = edge.evidence_slots

                new_status = self._resolve_status_by_evidence(edge.status, updated_slots)
                new_activation = self._compute_activation_score(updated_slots)

                old_status = edge.status
                edge_updates.append((edge.candidate_id, {
                    "evidence_slots": updated_slots,
                    "status": new_status,
                    "activation_score": round(new_activation, 4),
                    "last_seen_at": now,
                }))
                if new_status != old_status:
                    result.upgraded_edges.append((edge.candidate_id, old_status, new_status))
                    if self._on_candidate_transition is not None:
                        try:
                            self._on_candidate_transition(
                                candidate_id=edge.candidate_id,
                                promotion_type="candidate_edge",
                                from_status=old_status,
                                to_status=new_status,
                                evidence_slots=updated_slots,
                                policy_version=getattr(self._bayesian_upgrader, "VERSION", "bayesian-v1"),
                            )
                        except Exception as exc:
                            logger.warning("候选边迁移回调失败: %s", exc)

        # 批量写入边,一次性触发 LanceDB 批量删除+添加
        t_persist_edge = time.time()
        self.candidate_store.update_edges_batch(edge_updates)
        t_persist_edge = time.time() - t_persist_edge

        result.timing = {
            "keyword_query_s": round(t_keyword_query, 4),
            "name_match_compute_s": round(0.0, 4),
            "edge_match_compute_s": round(0.0, 4),
            "persist_node_s": round(t_persist_node, 4),
            "persist_edge_s": round(t_persist_edge, 4),
            "candidate_nodes": float(len(nodes)),
            "updated_nodes": float(len(node_updates)),
            "updated_edges": float(len(edge_updates)),
        }
        return result

    def activate_batch(
        self,
        evidence_items: list[dict[str, Any]],
        match_threshold: float | None = None,
    ) -> ActivationResult:
        """批量处理证据,高置信来源优先,打通多级状态升级通道。

        Args:
            evidence_items: 证据条目列表,每条至少包含 evidence_text/evidence_id,
                可选 source_type/stance/event_date。
            match_threshold: 名称匹配阈值,默认 NAME_MATCH_THRESHOLD。

        Returns:
            ActivationResult 摘要,timing 字段包含各阶段耗时(秒)。
        """
        timing: dict[str, float] = {}

        # 1. 索引预热
        t0 = time.time()
        self._ensure_keyword_index()
        timing["keyword_index_warmup_s"] = round(time.time() - t0, 3)

        # 分级搜索:按来源质量降序,让高质量证据优先参与升级
        def _source_quality(item: dict[str, Any]) -> float:
            st = item.get("source_type", "unknown")
            return SOURCE_QUALITY_BOOST.get(st, SOURCE_QUALITY_BOOST["unknown"])

        sorted_items = sorted(evidence_items, key=_source_quality, reverse=True)

        # 2. 逐条激活,分阶段计时
        result = ActivationResult()
        total_keyword_query = 0.0
        total_persist_node = 0.0
        total_persist_edge = 0.0
        total_activate_calls = 0.0

        for item in sorted_items:
            t_call_start = time.time()

            single = self.activate(
                evidence_text=item["evidence_text"],
                evidence_id=item["evidence_id"],
                source_type=item.get("source_type", "unknown"),
                stance=item.get("stance", "support"),
                event_date=item.get("event_date", ""),
                source_url=item.get("source_url", ""),
                source_lineage_id=item.get("source_lineage_id", ""),
                published_at=item.get("published_at", ""),
                retrieved_at=item.get("retrieved_at", ""),
                evidence_kind=item.get("evidence_kind", "document"),
                match_threshold=match_threshold,
                query_id=item.get("query_id", ""),
                query_purpose=item.get("query_purpose", item.get("purpose", "")),
            )
            total_activate_calls += time.time() - t_call_start
            total_keyword_query += single.timing.get("keyword_query_s", 0.0)
            total_persist_node += single.timing.get("persist_node_s", 0.0)
            total_persist_edge += single.timing.get("persist_edge_s", 0.0)

            result.matched_node_ids.extend(single.matched_node_ids)
            result.matched_edge_ids.extend(single.matched_edge_ids)
            result.upgraded_nodes.extend(single.upgraded_nodes)
            result.upgraded_edges.extend(single.upgraded_edges)
            result.matched_count += single.matched_count
            result.conflicts.extend(single.conflicts)

        timing["evidence_count"] = float(len(evidence_items))
        timing["total_activate_calls_s"] = round(total_activate_calls, 3)
        timing["keyword_query_total_s"] = round(total_keyword_query, 3)
        timing["persist_node_total_s"] = round(total_persist_node, 3)
        timing["persist_edge_total_s"] = round(total_persist_edge, 3)
        timing["compute_total_s"] = round(
            total_activate_calls - total_persist_node - total_persist_edge,
            3,
        )
        result.timing = timing
        return result

    def _match_node(self, node: CandidateNode, text: str) -> float:
        """计算节点名称与真实文本的匹配分数。

        匹配策略(多层回退):
        1. 精确子串命中:名称或别名作为完整子串出现在文本中 → 1.0
        2. 分词子串命中:主题根词/任意中文二字仅用于召回，不参与评分 → 0.0
           （由 ``_substring_match_score`` 返回 0.0；召回由关键词索引负责）
        3. 字符覆盖:中文名称所有字符都在文本中出现,且英文token也命中 → 0.7-0.9
        4. Bigram Jaccard:模糊匹配,对长文本更鲁棒 → 0-0.6

        检索假设不参与本方法，也不会构建生成内容的向量索引。
        """
        best_score = 0.0
        text_lower = text.lower()
        for name in [node.name] + list(node.aliases):
            if not name:
                continue
            name_lower = name.lower()
            # 1. 精确子串命中
            if name_lower in text_lower:
                best_score = max(best_score, 1.0)
                continue
            # 2. 分词子串命中：对中文长组合名做滑窗切分
            substr_score = self._substring_match_score(name, text_lower)
            best_score = max(best_score, substr_score)
            # 3. 字符覆盖率:中文名称每个字都在文本中出现
            score = self._char_coverage_score(name_lower, text_lower)
            best_score = max(best_score, score)
            # 4. Bigram Jaccard
            jaccard = _char_bigram_jaccard(name, text)
            if len(name) <= 2:
                jaccard *= 0.7
            best_score = max(best_score, jaccard)
        return best_score

    @staticmethod
    def _substring_match_score(name: str, text_lower: str) -> float:
        """主题根词/任意中文二字仅用于召回，不得直接写证据或升级。

        证据边界（P0 修复）：节点名中的英文主题根词（如 "AIPC"）和
        任意中文二字滑窗（如 "关键"、"材料"）只能作为关键词索引召回
        信号，由 ``_keyword_index.query`` 负责；它们不得贡献通过
        ``NAME_MATCH_THRESHOLD`` 的匹配分，否则会把仅含"AIPC电脑新品
        发布"的文本误判为"AIPC关键材料"节点的证据。

        证据级匹配由以下路径承担：
        - 精确完整子串命中（``_match_node`` 步骤 1，1.0）
        - 字符覆盖率：要求名称内全部中文字符与全部英文 token 同时
          出现（``_char_coverage_score``，最高 0.85）

        Args:
            name: 节点名称（原始大小写）。
            text_lower: 已小写的证据文本。

        Returns:
            恒为 0.0；本方法仅保留用于召回链路语义记录，不参与评分。
        """
        return 0.0

    @staticmethod
    def _char_coverage_score(name: str, text: str) -> float:
        """计算名称字符在文本中的覆盖率（要求中文字符连续出现）。

        P0 修复：旧实现逐字符检查 ``c in text``，长文中散落在各处的
        汉字会被拼凑为"全覆盖"并给出 0.85，导致普通长文成为伪证据。
        新规则：提取名称的中文字符序列，要求该序列作为**连续子串**出现
        在文本中，否则返回 0.0。英文/数字 token 仍要求全部命中。

        Args:
            name: 节点名称（已小写或原始均可）。
            text: 证据文本（已小写或原始均可）。

        Returns:
            连续子串命中且英文 token 全中 → 0.85；否则 0.0。
        """
        # 提取名称中的中文字符序列（保持原始顺序）
        chinese_seq = "".join(c for c in name if '\u4e00' <= c <= '\u9fa5')
        en_tokens = re.findall(r'[a-zA-Z0-9]+', name)

        # 中文字符序列必须作为连续子串出现在文本中
        if chinese_seq:
            if chinese_seq not in text:
                return 0.0

        # 英文/数字 token 必须全部作为完整 token 出现
        if en_tokens:
            text_lower = text.lower()
            hit = sum(1 for tok in en_tokens if tok.lower() in text_lower)
            if hit < len(en_tokens):
                return 0.0

        if not chinese_seq and not en_tokens:
            return 0.0

        # 中文连续子串命中 + 英文全中 → 0.85（弱于精确子串 1.0）
        return 0.85

    @staticmethod
    def _edge_relation_grounded(relation: str, text: str) -> bool:
        """检查文本是否表达边的关系语义。

        P0 修复：候选边匹配原来只检查两端节点都命中，即使文本只是
        同时提到两个实体而无关系表述也会附加证据。现在要求文本中
        至少出现 relation 的中文连续子串或英文 token 之一。

        Args:
            relation: 边的 relation 字段（如"依赖"、"供应"、"UPSTREAM_OF"）。
            text: 证据文本。

        Returns:
            True 表示文本表达关系语义；relation 为空时安全降级返回 True。
        """
        if not relation or not relation.strip():
            # 缺字段安全降级：无 relation 时不阻断（保持向后兼容）
            return True
        # 提取 relation 中的中文连续子串和英文 token
        chinese_seq = "".join(c for c in relation if '\u4e00' <= c <= '\u9fa5')
        en_tokens = re.findall(r'[a-zA-Z0-9]+', relation)
        text_lower = text.lower()
        # 中文子串连续出现 即算表达
        if chinese_seq and chinese_seq in text:
            return True
        # 英文 token 任一出现即算表达（大小写不敏感）
        for tok in en_tokens:
            if tok.lower() in text_lower:
                return True
        # relation 有内容但文本中无任何 relation 关键词 → 未表达关系语义
        return False

    def _resolve_status_by_evidence(
        self,
        current_status: str,
        evidence_slots: list[dict[str, Any]],
    ) -> str:
        """统一使用贝叶斯升级器解析目标状态。

        EvidenceActivator 不再维护固定阈值规则；所有状态迁移由
        BayesianStatusUpgrader 根据 Beta-Bernoulli 后验决定。
        """
        if not evidence_slots:
            return current_status
        # Contract rule: the first real, relevant support item creates a
        # CANDIDATE. Neutral/generated material cannot promote it.
        if current_status == CandidateStatus.DISCOVERED.value and any(
            slot.get("stance") == "support"
            and not is_blocked_evidence_kind(slot.get("evidence_kind", ""))
            for slot in evidence_slots
        ):
            return CandidateStatus.CANDIDATE.value
        last = evidence_slots[-1]
        result = self._bayesian_upgrader.decide(
            current_status,
            evidence_slots,
            last.get("stance", "support"),
            last.get("quality", 0.5),
        )
        # CandidateGraph is an exploration/retrieval surface. Terminal research
        # states are minted only by ClaimPolicy after audited sufficiency checks.
        if result.new_status in {
            CandidateStatus.ACTIVE.value,
            CandidateStatus.TRUSTED.value,
        }:
            return CandidateStatus.SUPPORTED.value
        return result.new_status

    def _compute_new_status(
        self,
        current_status: str,
        evidence_slots: list[dict[str, Any]],
        stance: str,
        source_quality: float,
    ) -> str:
        """根据累计证据计算新状态(兼容旧签名,内部调用 _resolve_status_by_evidence)。"""
        return self._resolve_status_by_evidence(current_status, evidence_slots)

    @staticmethod
    def _compute_activation_score(evidence_slots: list[dict[str, Any]]) -> float:
        """计算激活分数:基于证据数量和质量的加权和,归一化到 [0,1]。"""
        if not evidence_slots:
            return 0.0
        support_slots = [s for s in evidence_slots if s.get("stance") == "support"]
        counter_slots = [s for s in evidence_slots if s.get("stance") == "counter"]
        if not support_slots and not counter_slots:
            return 0.0
        total = sum(s.get("quality", 0.5) for s in support_slots)
        total -= sum(s.get("quality", 0.5) * 1.5 for s in counter_slots)
        # sigmoid-like 归一化
        import math
        score = 1.0 / (1.0 + math.exp(-0.5 * (total - 3)))
        return max(0.0, min(1.0, score))

    @staticmethod
    def compute_activation_score_as_of(
        evidence_slots: list[dict[str, Any]],
        as_of: str,
    ) -> float:
        """计算截至 as_of 时刻的激活分数。

        用于回测场景，过滤掉发布时间晚于 as_of 的证据，
        避免未来函数（用 T+5 的证据影响 T 日的激活分）。

        Args:
            evidence_slots: 证据槽列表。
            as_of: 截止日期（YYYY-MM-DD 或 ISO 格式）。

        Returns:
            截至该时刻的激活分数，范围 [0, 1]。
        """
        if not evidence_slots or not as_of:
            return EvidenceActivator._compute_activation_score(evidence_slots)
        cutoff = as_of[:10]  # YYYY-MM-DD
        visible_slots = [
            s for s in evidence_slots
            if not s.get("published_at") or str(s["published_at"])[:10] <= cutoff
        ]
        return EvidenceActivator._compute_activation_score(visible_slots)
