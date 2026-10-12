"""Dynamic Event Propagation Layer for SAG / Perilla.

将 EventClaim / EventEdge 转化为动态 overlay 信号，用于对稳定图路径进行
事件感知的排序与风险/机会增强。本模块不修改稳定图的边或 Claim，只生成
overlay 信号。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import deque
from typing import Any

from core.research.perilla.alpha.graph_schema import SourceType, get_source_weight
from core.research.perilla.event.event_graph_schema import (
    EVENT_CLAIM_STATUSES,
    EventClaim,
    EventClaimStatus,
    EventType,
)
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase
from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry

logger = logging.getLogger(__name__)

# 事件类型基础权重：确定性规则，无 LLM。
EVENT_BASE_WEIGHTS: dict[str, float] = {
    EventType.PRICE_INCREASE.value: 0.25,
    EventType.CAPACITY_EXPANSION.value: 0.10,  # 语义双向：既可 Boost 也隐含风险
    EventType.CUSTOMER_CERTIFIED.value: 0.30,
    EventType.ORDER_DISCLOSED.value: 0.35,
    EventType.PRODUCT_LAUNCHED.value: 0.20,
    EventType.SUPPLY_SHORTAGE.value: 0.30,
    EventType.DOMESTIC_SUBSTITUTION_PROGRESS.value: 0.25,
    EventType.POLICY_SIGNAL.value: 0.15,
    EventType.IMPORT_DEPENDENCY_SIGNAL.value: -0.20,
    EventType.CERTIFICATION_PROGRESS.value: 0.20,
    EventType.BOTTLENECK_HYPOTHESIS.value: 0.0,
    EventType.CONCEPT_SIGNAL.value: 0.05,
    EventType.OTHER.value: 0.05,
}

# 状态乘数：确定性规则。
STATUS_MULTIPLIERS: dict[str, float] = {
    EventClaimStatus.ACTIVE.value: 1.0,
    EventClaimStatus.CONFIRMED.value: 1.2,
    EventClaimStatus.PROMOTION_CANDIDATE.value: 1.1,
    EventClaimStatus.PROMOTED.value: 1.0,
    EventClaimStatus.STALE.value: 0.3,
    EventClaimStatus.EXPIRED.value: 0.0,
    EventClaimStatus.RETRACTED.value: -1.0,
    EventClaimStatus.CONTRADICTED.value: -1.2,
}

# 高可信正式来源集合（用于 reward mapper）。
_OFFICIAL_SOURCE_TYPES: frozenset[str] = frozenset(
    [
        SourceType.ANNOUNCEMENT.value,
        SourceType.ANNUAL_REPORT.value,
        SourceType.PROSPECTUS.value,
        SourceType.OFFICIAL_WEBSITE.value,
    ]
)


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite_float(value, default)))


def _bounded_int(value: Any, default: int, maximum: int = 10000) -> int:
    try:
        number = int(float(value))
    except (OverflowError, TypeError, ValueError):
        number = default
    return max(0, min(maximum, number))


def _now_iso() -> str:
    """返回当前 ISO 时间字符串。"""
    return datetime.now().isoformat()


def _default_freshness(claim: EventClaim) -> float:
    """当 claim.freshness_score 未设置时的保守默认值。"""
    freshness = _score(_field(claim, "freshness_score"))
    return freshness if freshness > 0 else 0.1


@dataclass
class EventImpactSignal:
    """单个事件声明对稳定图的动态影响信号。

    Attributes:
        event_claim_id: 来源 EventClaim ID。
        event_type: 标准化事件类型。
        subject: 事件主体。
        object: 事件对象。
        target_node_ids: 影响的稳定图节点 ID 列表。
        target_edge_keys: 影响的稳定图边键列表。
        impact_score: 带符号的影响分（正为机会，负为风险）。
        boost_score: 非负的上涨/机会压力。
        risk_score: 非负的下跌/风险压力。
        freshness_score: 新鲜度得分 [0, 1]。
        promotion_pressure: 晋升稳定图的压力。
        contradiction_pressure: 与稳定图冲突的压力。
        retrieval_reward: 检索/召回奖励（由 EventRewardMapper 计算）。
        reason: 可读的得分原因。
        timestamp: 信号时间戳。
    """

    event_claim_id: str
    event_type: str
    subject: str
    object: str
    target_node_ids: list[str]
    target_edge_keys: list[tuple[str, str, str]]
    impact_score: float
    boost_score: float
    risk_score: float
    freshness_score: float
    promotion_pressure: float
    contradiction_pressure: float
    retrieval_reward: float
    reason: str
    timestamp: str


@dataclass
class EventPropagationResult:
    """一次传播计算的结果容器。

    Attributes:
        root: 传播的根节点。
        seed_nodes: 用于 PPR 的种子节点。
        node_activation: 节点激活分数。
        edge_activation: 边激活分数。
        path_overlays: 稳定路径上的事件 overlay，key 为路径 ID。
        impact_signals: 本次传播生成的所有影响信号。
        promotion_candidates: 晋升候选列表。
        contradiction_candidates: 冲突候选列表。
    """

    root: str
    seed_nodes: list[str]
    node_activation: dict[str, float]
    edge_activation: dict[tuple[str, str, str], float]
    path_overlays: dict[str, dict[str, Any]]
    impact_signals: list[EventImpactSignal]
    promotion_candidates: list[Any]
    contradiction_candidates: list[Any]


class EventImpactScorer:
    """基于确定性规则对 EventClaim 进行影响评分。"""

    def __init__(self, ontology_registry: OntologyRegistry | None = None) -> None:
        """初始化评分器。

        Args:
            ontology_registry: Ontology 注册表，None 则使用默认值
        """
        self.ontology_registry = ontology_registry or OntologyRegistry.default()

    def score_event_claim(self, claim: EventClaim) -> EventImpactSignal:
        """对单个 EventClaim 生成 EventImpactSignal。

        Args:
            claim: 输入事件声明。

        Returns:
            计算得到的影响信号。
        """
        event_type_raw = _text(_field(claim, "event_type"))
        try:
            event_type_valid = self.ontology_registry.is_valid_event_type(event_type_raw)
        except (AttributeError, TypeError, ValueError):
            event_type_valid = False
        event_type = event_type_raw if event_type_valid else EventType.OTHER.value
        base_weight = EVENT_BASE_WEIGHTS.get(event_type, EVENT_BASE_WEIGHTS[EventType.OTHER.value])
        source_weight = get_source_weight(_text(_field(claim, "source_type")))
        freshness_score = _default_freshness(claim)
        status_raw = _text(_field(claim, "status"))
        status = status_raw if status_raw in EVENT_CLAIM_STATUSES else EventClaimStatus.ACTIVE.value
        status_multiplier = STATUS_MULTIPLIERS.get(status, 1.0)

        confidence = _score(_field(claim, "confidence") or _field(claim, "score"))

        # impact_score = base_weight * confidence * source_weight * freshness_score * status_multiplier
        impact_score = base_weight * confidence * source_weight * freshness_score * status_multiplier

        boost_score = max(0.0, impact_score)
        risk_score = max(0.0, -impact_score)

        # promotion_pressure: 仅在正向活跃/确认/候选状态时产生
        if status in (
            EventClaimStatus.ACTIVE.value,
            EventClaimStatus.CONFIRMED.value,
            EventClaimStatus.PROMOTION_CANDIDATE.value,
        ):
            promotion_pressure = boost_score
        else:
            promotion_pressure = 0.0

        # contradiction_pressure: 仅在撤销/冲突/过期时产生
        if status in (
            EventClaimStatus.RETRACTED.value,
            EventClaimStatus.CONTRADICTED.value,
            EventClaimStatus.EXPIRED.value,
        ):
            contradiction_pressure = risk_score
        else:
            contradiction_pressure = 0.0

        subject = _text(_field(claim, "subject"))
        object_entity = _text(_field(claim, "object"))
        target_node_ids = [_text(node) for node in _items(_field(claim, "linked_node_ids")) if _text(node)] or [node for node in (subject, object_entity) if node]
        timestamp = _text(_field(claim, "source_time")) or _text(_field(claim, "time")) or _now_iso()

        reason = (
            f"base={base_weight:.2f}, confidence={confidence:.2f}, source_weight={source_weight:.2f}, "
            f"freshness={freshness_score:.2f}, status={status}, multiplier={status_multiplier:.2f}, "
            f"impact={impact_score:.4f}"
        )

        return EventImpactSignal(
            event_claim_id=_text(_field(claim, "event_id")),
            event_type=event_type,
            subject=subject,
            object=object_entity,
            target_node_ids=target_node_ids,
            target_edge_keys=[tuple(_text(part) for part in key) for key in _items(_field(claim, "linked_edge_keys")) if isinstance(key, (list, tuple)) and len(key) == 3],
            impact_score=impact_score,
            boost_score=boost_score,
            risk_score=risk_score,
            freshness_score=freshness_score,
            promotion_pressure=promotion_pressure,
            contradiction_pressure=contradiction_pressure,
            retrieval_reward=0.0,
            reason=reason,
            timestamp=timestamp,
        )


class EventAwarePPRRanker:
    """基于稳定图出边的 Personalized PageRank 排名器。

    仅读取稳定图边，不修改图结构。
    """

    def __init__(self, kb: SAGKnowledgeBase) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
        """
        self.kb = kb

    def _reachable_nodes(self, root: str) -> set[str]:
        """BFS 返回从 root 经稳定图出边可达的所有节点（含 root）。"""
        visited: set[str] = {root}
        queue = deque([root])
        while queue:
            current = queue.popleft()
            for edge in self.kb.get_neighbors(current, direction="out"):
                if edge.tail not in visited:
                    visited.add(edge.tail)
                    queue.append(edge.tail)
        return visited

    def _build_transition(
        self, nodes: set[str]
    ) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
        """构建归一化转移矩阵与悬空节点集合。

        Returns:
            (transition_dict, dangling_nodes_set) 其中 transition_dict[u][v] 为转移概率。
        """
        transition: dict[str, dict[str, float]] = {}
        dangling: set[str] = set()
        for node in nodes:
            out_edges = [edge for edge in self.kb.get_neighbors(node, direction="out") if edge.tail in nodes]
            if not out_edges:
                dangling.add(node)
                continue
            weight = 1.0 / len(out_edges)
            transition[node] = {edge.tail: weight for edge in out_edges}
        return transition, dangling

    def rank(
        self,
        root: str,
        seed_nodes: list[str],
        max_iter: int = 30,
        damping: float = 0.85,
        tol: float = 1e-4,
    ) -> dict[str, float]:
        """执行事件感知的 PPR，返回节点激活分数。

        Args:
            root: 根节点。
            seed_nodes: 种子节点列表；为空时则使用从 root 可达的所有节点。
            max_iter: 最大迭代次数。
            damping: 阻尼系数。
            tol: 收敛阈值。

        Returns:
            节点 -> 激活分数 的映射。
        """
        root = _text(root)
        max_iter = max(1, _bounded_int(max_iter, 30, 1000))
        damping = max(0.0, min(1.0, _finite_float(damping, 0.85)))
        tol = max(0.0, _finite_float(tol, 1e-4))
        if seed_nodes:
            personalization_nodes = sorted({_text(node) for node in _items(seed_nodes) if _text(node)} | {root})
        else:
            personalization_nodes = sorted(self._reachable_nodes(root))

        if not personalization_nodes:
            return {}

        nodes = self._reachable_nodes(root)
        if not nodes:
            return {root: 1.0}

        transition, dangling = self._build_transition(nodes)

        # 初始均匀分布
        node_list = sorted(nodes)
        n = len(node_list)
        init_value = 1.0 / n
        rank: dict[str, float] = {node: init_value for node in node_list}

        # personalization vector: 在 personalization_nodes 上均匀分布
        personalization: dict[str, float] = {node: 0.0 for node in node_list}
        personal_sum = len([node for node in personalization_nodes if node in personalization])
        personal_value = 1.0 / personal_sum if personal_sum > 0 else 0.0
        for node in personalization_nodes:
            if node in personalization:
                personalization[node] = personal_value

        for _ in range(max_iter):
            new_rank: dict[str, float] = {}
            dangling_mass = sum(rank.get(dangling_node, 0.0) for dangling_node in dangling)
            for node in node_list:
                # 来自非悬空节点的贡献
                contribution = 0.0
                for prev_node, prev_dist in transition.items():
                    if node in prev_dist:
                        contribution += rank.get(prev_node, 0.0) * prev_dist[node]
                # 悬空节点将其概率质量全部传送给 personalization vector
                dangling_contribution = dangling_mass * personalization.get(node, 0.0)
                new_rank[node] = (1.0 - damping) * personalization.get(node, 0.0) + damping * (
                    contribution + dangling_contribution
                )

            # 归一化，避免数值漂移
            total = sum(new_rank.values())
            if total > 0:
                new_rank = {k: v / total for k, v in new_rank.items()}

            diff = sum(abs(new_rank[node] - rank[node]) for node in node_list)
            rank = new_rank
            if diff < tol:
                break

        return rank

    def edge_activation(
        self, node_activation: dict[str, float]
    ) -> dict[tuple[str, str, str], float]:
        """根据节点激活分数计算边激活分数。

        使用头尾节点激活分数的几何平均。

        Args:
            node_activation: 节点激活分数。

        Returns:
            边键 -> 激活分数 的映射。
        """
        result: dict[tuple[str, str, str], float] = {}
        node_activation = node_activation if isinstance(node_activation, dict) else {}
        for edge_key, edge in self.kb.edges.items():
            head_score = max(0.0, _finite_float(node_activation.get(edge.head)))
            tail_score = max(0.0, _finite_float(node_activation.get(edge.tail)))
            if head_score > 0 and tail_score > 0:
                result[edge_key] = math.sqrt(head_score * tail_score)
            else:
                result[edge_key] = 0.0
        return result


class EventRewardMapper:
    """为 EventClaim 计算检索/召回奖励分数。"""

    def __init__(self, kb: SAGKnowledgeBase | None = None) -> None:
        """初始化。

        Args:
            kb: 可选的 SAG 知识库，用于检查多源事件边。
        """
        self.kb = kb

    def reward_for_claim(self, claim: EventClaim, query_intent: str = "") -> float:
        """计算单个 EventClaim 的奖励分数。

        Args:
            claim: 输入事件声明。
            query_intent: 查询意图字符串（当前仅作兼容性保留）。

        Returns:
            归一化到 [-1, 1] 的奖励分数。
        """
        reward = 0.0
        confidence = _score(_field(claim, "confidence") or _field(claim, "score"))
        source_weight = get_source_weight(_text(_field(claim, "source_type")))
        freshness_score = _default_freshness(claim)
        status_raw = _text(_field(claim, "status"))
        status = status_raw if status_raw in EVENT_CLAIM_STATUSES else EventClaimStatus.ACTIVE.value

        # 正向奖励
        if confidence > 0.7:
            reward += 0.2
        if source_weight > 0.7:
            reward += 0.2
        if freshness_score > 0.7:
            reward += 0.2
        if status == EventClaimStatus.CONFIRMED.value:
            reward += 0.1
        if _text(_field(claim, "source_type")) in _OFFICIAL_SOURCE_TYPES:
            reward += 0.1

        # 多源事件边奖励
        if self.kb is not None:
            edge_key = (_text(_field(claim, "subject")), _text(_field(claim, "event_type")), _text(_field(claim, "object")))
            event_edges = getattr(self.kb, "event_edges", {})
            event_edge = event_edges.get(edge_key) if isinstance(event_edges, dict) else None
            if event_edge is not None and _bounded_int(_field(event_edge, "source_count"), 0) > 1:
                reward += 0.1

        # 查询意图相关性奖励（轻量、确定性）
        query_intent = _text(query_intent)
        if query_intent:
            intent_tokens = set(query_intent.lower().split())
            claim_text = f"{_text(_field(claim, 'subject'))} {_text(_field(claim, 'object'))} {_text(_field(claim, 'evidence'))}".lower()
            if intent_tokens and any(token in claim_text for token in intent_tokens):
                reward += 0.05

        # 负向惩罚
        if status in (
            EventClaimStatus.RETRACTED.value,
            EventClaimStatus.CONTRADICTED.value,
            EventClaimStatus.EXPIRED.value,
        ):
            reward -= 0.5
        if status == EventClaimStatus.STALE.value:
            reward -= 0.2
        if confidence < 0.3:
            reward -= 0.1
        if source_weight < 0.3:
            reward -= 0.1

        return max(-1.0, min(1.0, reward))


class EventPathOverlayBuilder:
    """为单条稳定图路径聚合事件影响信号。"""

    def __init__(self, kb: SAGKnowledgeBase) -> None:
        """初始化。

        Args:
            kb: SAG 知识库，用于解析路径上的稳定边键。
        """
        self.kb = kb

    def _edge_keys_on_path(self, path: list[str]) -> set[tuple[str, str, str]]:
        """收集路径上所有连续节点对之间的稳定图边键。"""
        path = [_text(node) for node in _items(path) if _text(node)]
        edge_keys: set[tuple[str, str, str]] = set()
        for i in range(len(path) - 1):
            head = path[i]
            tail = path[i + 1]
            for edge in self.kb.get_neighbors(head, direction="out"):
                if edge.tail == tail:
                    edge_keys.add((edge.head, edge.relation, edge.tail))
        return edge_keys

    def build_overlay(
        self, path: list[str], impact_signals: list[EventImpactSignal]
    ) -> dict[str, Any]:
        """为路径构建事件 overlay。

        Args:
            path: 稳定图路径节点列表。
            impact_signals: 全局影响信号列表。

        Returns:
            包含聚合事件分数的 dict。
        """
        path = [_text(node) for node in _items(path) if _text(node)]
        path_set = set(path)
        edge_keys_on_path = self._edge_keys_on_path(path)

        related_ids: list[str] = []
        event_boost_score = 0.0
        event_risk_score = 0.0
        freshness_score = 0.0
        promotion_pressure = 0.0
        contradiction_pressure = 0.0

        for signal in _items(impact_signals):
            node_hit = any(_text(node_id) in path_set for node_id in _items(_field(signal, "target_node_ids")))
            edge_hit = any(edge_key in edge_keys_on_path for edge_key in _items(_field(signal, "target_edge_keys")))
            if not node_hit and not edge_hit:
                continue

            related_ids.append(_text(_field(signal, "event_claim_id")))
            event_boost_score += _score(_field(signal, "boost_score"))
            event_risk_score += _score(_field(signal, "risk_score"))
            freshness_score = max(freshness_score, _score(_field(signal, "freshness_score")))
            promotion_pressure += _score(_field(signal, "promotion_pressure"))
            contradiction_pressure += _score(_field(signal, "contradiction_pressure"))

        return {
            "path": path,
            "activation_nodes": list(path_set),
            "related_event_claim_ids": related_ids,
            "event_boost_score": round(event_boost_score, 6),
            "event_risk_score": round(event_risk_score, 6),
            "freshness_score": round(freshness_score, 6),
            "promotion_pressure": round(promotion_pressure, 6),
            "contradiction_pressure": round(contradiction_pressure, 6),
        }


def _enumerate_paths_simple(
    kb: SAGKnowledgeBase,
    root: str,
    max_depth: int = 5,
    max_paths: int = 200,
) -> list[list[str]]:
    """简单的 BFS/栈路径枚举器（FrontierScorer 不可用时作为 fallback）。"""
    root = _text(root)
    max_depth = max(1, _bounded_int(max_depth, 5, 1000))
    max_paths = _bounded_int(max_paths, 200, 100000)
    paths: list[list[str]] = []
    stack: list[tuple[str, list[str]]] = [(root, [root])] if root else []

    while stack and len(paths) < max_paths:
        current, path = stack.pop()
        if len(path) >= max_depth:
            paths.append(path)
            continue

        out_edges = kb.get_neighbors(current, direction="out")
        if not out_edges:
            paths.append(path)
            continue

        for edge in out_edges:
            tail = edge.tail
            if tail in path:  # 避免环
                continue
            stack.append((tail, path + [tail]))

    return paths


try:
    from core.research.perilla.alpha.frontier_scorer import FrontierScorer

    _HAS_FRONTIER_SCORER = True
except Exception:  # pragma: no cover - 防御性导入保护
    _HAS_FRONTIER_SCORER = False


class DynamicEventPropagationEngine:
    """动态事件传播引擎：协调评分、PPR、路径 overlay 生成。"""

    def __init__(self, kb: SAGKnowledgeBase, entity_resolver: Any | None = None) -> None:
        """初始化引擎。

        Args:
            kb: SAG 知识库。
            entity_resolver: 可选实体解析器，透传给 FrontierScorer。
        """
        self.kb = kb
        self.entity_resolver = entity_resolver
        self.scorer = EventImpactScorer(kb.ontology_registry)
        self.ppr_ranker = EventAwarePPRRanker(kb)
        self.reward_mapper = EventRewardMapper(kb)
        self.overlay_builder = EventPathOverlayBuilder(kb)

    def _enumerate_stable_paths(self, root: str) -> list[list[str]]:
        """枚举稳定图路径，优先使用 FrontierScorer，失败则回退到简单 BFS。"""
        if _HAS_FRONTIER_SCORER:
            try:
                frontier_scorer = FrontierScorer(self.kb, self.entity_resolver)
                scored_paths = frontier_scorer.enumerate_paths(
                    root, max_depth=5, max_paths=200
                )
                return [p["path"] for p in _items(scored_paths) if isinstance(p, dict) and isinstance(p.get("path"), list)]
            except Exception as exc:  # pragma: no cover
                logger.warning("FrontierScorer 路径枚举失败，回退到简单 BFS: %s", exc)

        return _enumerate_paths_simple(self.kb, root, max_depth=5, max_paths=200)

    def propagate_for_root(
        self,
        root: str,
        query_intent: str = "",
        recent_days: int = 90,
        min_confidence: float = 0.0,
    ) -> EventPropagationResult:
        """为指定根节点执行完整的事件传播流程。

        Args:
            root: 根节点名称。
            query_intent: 查询意图字符串。
            recent_days: 只考虑最近 N 天内的事件。
            min_confidence: 事件 claim 最低置信度。

        Returns:
            EventPropagationResult 传播结果。
        """
        root = _text(root)
        recent_days = max(0, _finite_float(recent_days, 90))
        min_confidence = _score(min_confidence)
        query_intent = _text(query_intent)
        # 1. 更新事件分数
        self.kb.update_event_scores()

        # 2. 获取近期事件 claim
        since_dt = datetime.now() - timedelta(days=recent_days)
        since = since_dt.isoformat()
        recent_claims = self.kb.get_recent_event_claims(
            since=since, min_confidence=min_confidence
        )

        # 3. 生成影响信号
        signals = [self.scorer.score_event_claim(c) for c in _items(recent_claims)]

        # 回填 retrieval_reward
        for signal in signals:
            event_claims = getattr(self.kb, "event_claims", {})
            claim = event_claims.get(signal.event_claim_id) if isinstance(event_claims, dict) else None
            if claim is not None:
                signal.retrieval_reward = self.reward_mapper.reward_for_claim(
                    claim, query_intent=query_intent
                )

        # 4. 从正向 boost 信号中构造种子节点
        seed_nodes = sorted(
            {
                node_id
                for signal in signals
                if signal.boost_score > 0
                for node_id in signal.target_node_ids
                if _text(node_id)
            }
        )

        # 5-6. PPR 节点/边激活
        node_activation = self.ppr_ranker.rank(root, seed_nodes)
        edge_activation = self.ppr_ranker.edge_activation(node_activation)

        # 7. 路径 overlay
        stable_paths = self._enumerate_stable_paths(root)
        path_overlays: dict[str, dict[str, Any]] = {}
        for path in stable_paths:
            overlay = self.overlay_builder.build_overlay(path, signals)
            path_id = "->".join(_text(node) for node in _items(path))
            path_overlays[path_id] = overlay

        # 晋升 / 冲突候选
        promotion_candidates = [
            claim for claim in recent_claims if claim.promotion_candidate
        ]
        contradiction_candidates = [
            claim for claim in recent_claims if claim.contradiction_candidate
        ]

        return EventPropagationResult(
            root=root,
            seed_nodes=seed_nodes,
            node_activation=node_activation,
            edge_activation=edge_activation,
            path_overlays=path_overlays,
            impact_signals=signals,
            promotion_candidates=promotion_candidates,
            contradiction_candidates=contradiction_candidates,
        )
