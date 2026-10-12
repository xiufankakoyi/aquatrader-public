"""紫苏叶评分适配器：认知缺口的优先级评分。

用户要求的评分公式：
    研究价值 = 因果中心性 × 交易相关性 × 信息新颖度 × 证据缺口
             × 时效性 × 冲突强度 × 可验证性 ÷ 预计成本

设计原则（DRY + KISS）：
    1. 轻量初筛：基于 UnifiedResearchMemory.identify_gaps() 的缺口数据快速打分
       —— O(N) 遍历，过滤掉明显低价值的缺口
    2. 精评：仅对初筛 Top-K 候选执行多维评分
       —— 查询邻居、证据、冲突等，计算综合研究价值
    3. 可选委托：若提供了 FrontierScorer 适配器，精评阶段可委托其 score_node

不依赖 SAGKnowledgeBase，直接基于 GraphStore 数据结构。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from core.research_graph.unified_memory import UnifiedResearchMemory

logger = logging.getLogger(__name__)


@dataclass
class ScoredGap:
    """带评分的认知缺口。

    Attributes:
        gap: 原始缺口字典（来自 identify_gaps）。
        research_value: 综合研究价值 [0, 1]。
        dimensions: 各维度分数。
        estimated_cost: 预计研究成本 [0, 1]（越高越贵）。
        reason: 评分理由（人类可读）。
    """

    gap: dict[str, Any]
    research_value: float
    dimensions: dict[str, float] = field(default_factory=dict)
    estimated_cost: float = 0.5
    reason: str = ""


class PerillaGapScorer:
    """紫苏叶评分适配器：轻量初筛 + 精评。

    Args:
        memory: 统一研究记忆层。
        light_screen_threshold: 轻量初筛阈值，低于此分的缺口不进入精评。
        frontier_scorer: 可选的 FrontierScorer 实例，提供时精评委托其 score_node。
    """

    def __init__(
        self,
        memory: UnifiedResearchMemory,
        light_screen_threshold: float = 0.15,
        frontier_scorer: Any | None = None,
    ) -> None:
        self._memory = memory
        self._light_threshold = light_screen_threshold
        self._frontier_scorer = frontier_scorer

    # ------------------------------------------------------------------
    # 轻量初筛：O(N) 快速过滤
    # ------------------------------------------------------------------

    def light_screen(self, gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """轻量初筛：基于缺口类型和严重度快速过滤。

        Args:
            gaps: identify_gaps 返回的缺口列表。

        Returns:
            通过初筛的缺口列表（按初筛分降序）。
        """
        scored: list[tuple[float, dict[str, Any]]] = []
        for gap in gaps:
            score = self._light_score(gap)
            if score >= self._light_threshold:
                scored.append((score, gap))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [g for _, g in scored]

    def _light_score(self, gap: dict[str, Any]) -> float:
        """轻量初筛评分：缺口类型基础分 × 严重度。

        Args:
            gap: 单个缺口字典。

        Returns:
            初筛分数 [0, 1]。
        """
        gap_type = gap.get("type", "")
        severity = gap.get("severity", 0.0)

        # 缺口类型基础分
        type_base = {
            "conflict": 0.8,       # 冲突最高优先级
            "weak_edge": 0.5,      # 弱边次之
            "island": 0.3,         # 孤岛最低
            # 这些缺口是认知链路的核心，不应因使用默认 0.2 而在初筛阶段消失。
            "causal_gap": 0.7,
            "temporal_gap": 0.6,
            "stale_verification": 0.75,
            "company_mapping_gap": 0.65,
            "source_lineage_gap": 0.6,
        }.get(gap_type, 0.2)

        return type_base * severity

    # ------------------------------------------------------------------
    # 精评：多维评分
    # ------------------------------------------------------------------

    def score_gaps(
        self,
        gaps: list[dict[str, Any]],
        max_precise: int = 20,
    ) -> list[ScoredGap]:
        """对缺口列表执行轻量初筛 + 精评。

        Args:
            gaps: identify_gaps 返回的缺口列表。
            max_precise: 精评最大数量（只精评 Top-K）。

        Returns:
            按 research_value 降序排列的 ScoredGap 列表。
        """
        # 1. 轻量初筛
        screened = self.light_screen(gaps)
        logger.info(
            "轻量初筛: %d → %d 缺口通过阈值 %.2f",
            len(gaps), len(screened), self._light_threshold,
        )

        # 2. 精评 Top-K
        candidates = screened[:max_precise]
        scored_gaps: list[ScoredGap] = []
        for gap in candidates:
            scored = self._precise_score(gap)
            scored_gaps.append(scored)

        # 3. 按研究价值降序
        scored_gaps.sort(key=lambda sg: sg.research_value, reverse=True)
        return scored_gaps

    def _precise_score(self, gap: dict[str, Any]) -> ScoredGap:
        """对单个缺口执行精评。

        评分维度（用户要求）：
            研究价值 = 因果中心性 × 交易相关性 × 信息新颖度 × 证据缺口
                     × 时效性 × 冲突强度 × 可验证性 ÷ 预计成本

        Args:
            gap: 单个缺口字典。

        Returns:
            ScoredGap 评分结果。
        """
        store = self._memory.graph_store
        dimensions: dict[str, float] = {}

        # 确定缺口关联的节点
        node_id = gap.get("node_id", "")
        edge_id = gap.get("edge_id", "")

        if node_id:
            node = store.nodes.get(node_id)
            node_name = node.name if node else ""
        elif edge_id:
            edge = store.edges.get(edge_id)
            node_id = edge.subjects[0] if edge and edge.subjects else ""
            node = store.nodes.get(node_id)
            node_name = node.name if node else ""
        else:
            node_name = ""

        # 1. 因果中心性：介数中心性（小图降级度数）
        dimensions["causal_centrality"] = self._causal_centrality(node_id, store)

        # 2. 交易相关性：节点类型权重（PRODUCT/MATERIAL/COMPANY 高，CONCEPT 低）
        dimensions["trade_relevance"] = self._trade_relevance(node)

        # 3. 信息新颖度：节点创建时间越近越新颖
        dimensions["info_novelty"] = self._info_novelty(node)

        # 4. 证据缺口：缺口类型映射
        dimensions["evidence_gap"] = self._evidence_gap_score(gap, edge_id, store)

        # 5. 时效性：基于节点关联证据的 event_date
        dimensions["timeliness"] = self._timeliness(node_id, store)

        # 6. 冲突强度：反证数量
        dimensions["conflict_intensity"] = self._conflict_intensity(edge_id, store)

        # 7. 可验证性：节点类型决定可验证性（具体实体高，抽象概念低）
        dimensions["verifiability"] = self._verifiability(node)

        # 8. 预计成本：度数越高成本越低（已有上下文）
        dimensions["estimated_cost"] = self._estimated_cost(node_id, store)

        # 综合评分：核心维度算术平均 + bonus 维度加法 + 成本除数
        # 核心维度（乘法对零值太敏感，改用算术平均）
        core_dims = [
            "causal_centrality", "trade_relevance",
            "evidence_gap", "verifiability",
        ]
        core_avg = sum(dimensions[d] for d in core_dims) / len(core_dims)

        # bonus 维度（加法，不导致归零）
        novelty_bonus = dimensions["info_novelty"] * 0.15
        timeliness_bonus = dimensions["timeliness"] * 0.10
        conflict_bonus = dimensions["conflict_intensity"] * 0.25  # 冲突高价值

        # 基础分 + bonus（cap 在 1.0）
        base_score = min(1.0, core_avg + novelty_bonus + timeliness_bonus + conflict_bonus)

        # 成本作为除数（成本越高分数越低）
        cost = max(0.1, min(1.0, dimensions["estimated_cost"]))
        research_value = base_score / (1.0 + cost * 0.5)
        research_value = min(1.0, research_value)

        # 可选：委托 FrontierScorer 精评，合并 14 维评分
        if self._frontier_scorer is not None and node_name:
            try:
                # 缺口评分场景下，以节点自身为根调用 FrontierScorer
                fs_result = self._frontier_scorer.score_node(node_name, node_name)
                if fs_result and isinstance(fs_result.get("dimensions"), dict):
                    # 合并 FrontierScorer 14 维评分到 dimensions，前缀 frontier_
                    for dim_name, dim_value in fs_result["dimensions"].items():
                        try:
                            dimensions[f"frontier_{dim_name}"] = float(dim_value)
                        except (TypeError, ValueError):
                            continue
                    # 加权融合：自身评分 0.7 + FrontierScorer total 0.3
                    fs_total = float(fs_result.get("total", 0.0))
                    if fs_total > 0:
                        research_value = min(
                            1.0,
                            research_value * 0.7 + fs_total * 0.3,
                        )
            except Exception as exc:
                logger.debug("FrontierScorer 精评失败: %s", exc)

        reason = self._build_reason(gap, dimensions, node_name)

        return ScoredGap(
            gap=gap,
            research_value=round(research_value, 4),
            dimensions={k: round(v, 4) for k, v in dimensions.items()},
            estimated_cost=round(cost, 4),
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 评分维度实现
    # ------------------------------------------------------------------

    def _causal_centrality(self, node_id: str, store: Any) -> float:
        """因果中心性：基于介数中心性（betweenness centrality）。

        使用 networkx 计算节点在所有最短路径中出现的频率，
        反映节点作为信息中介的重要性。图太小（<3 节点）或
        networkx 不可用时降级使用度数归一化。

        Args:
            node_id: 节点 ID。
            store: GraphStore 实例。

        Returns:
            中心性分数 [0, 1]。
        """
        if not node_id:
            return 0.0

        all_node_ids = set(store.nodes.keys())
        # 图太小（<3 节点）时降级使用度数：betweenness 在小图上无意义
        if len(all_node_ids) < 3:
            return self._degree_centrality(node_id, store)

        try:
            import networkx as nx
        except ImportError:
            logger.debug("networkx 不可用，降级使用度数中心性")
            return self._degree_centrality(node_id, store)

        # 构建无向图：超边展开为无向边
        graph = nx.Graph()
        for nid in all_node_ids:
            graph.add_node(nid)
        for edge in store.edges.values():
            for subject_id in edge.subjects:
                for object_id in edge.objects:
                    if subject_id in all_node_ids and object_id in all_node_ids:
                        graph.add_edge(subject_id, object_id)

        # 计算介数中心性（normalized=True，结果在 [0, 1]）
        centrality = nx.betweenness_centrality(graph, normalized=True)
        return min(1.0, float(centrality.get(node_id, 0.0)))

    def _degree_centrality(self, node_id: str, store: Any) -> float:
        """度数中心性：节点度数归一化（小图降级方案）。

        Args:
            node_id: 节点 ID。
            store: GraphStore 实例。

        Returns:
            度数中心性 [0, 1]。
        """
        if not node_id:
            return 0.0
        degree = 0
        for edge in store.edges.values():
            if node_id in edge.subjects or node_id in edge.objects:
                degree += 1
        # 归一化：度数 10 为满分
        return min(1.0, degree / 10.0)

    def _trade_relevance(self, node: Any) -> float:
        """交易相关性：节点类型权重。

        Args:
            node: HyperNode 实例（可能为 None）。

        Returns:
            交易相关性 [0, 1]。
        """
        if node is None:
            return 0.2
        type_weights = {
            "PRODUCT": 0.9,
            "MATERIAL": 0.85,
            "COMPANY": 0.95,
            "EQUIPMENT": 0.8,
            "PROCESS": 0.6,
            "EVENT": 0.7,
            "CONCEPT": 0.3,
            "THEME": 0.2,
        }
        return type_weights.get(node.type, 0.4)

    def _info_novelty(self, node: Any) -> float:
        """信息新颖度：节点创建时间越近越新颖。

        Args:
            node: HyperNode 实例。

        Returns:
            新颖度 [0, 1]。
        """
        if node is None or not node.created_at:
            return 0.5
        try:
            from datetime import datetime, timezone
            created = datetime.fromisoformat(node.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_hours = (now - created).total_seconds() / 3600
            # 24 小时内满分，7 天后衰减到 0.3
            if age_hours < 24:
                return 1.0
            elif age_hours < 168:  # 7 天
                return 0.8 - 0.5 * (age_hours - 24) / 144
            else:
                return max(0.2, 0.3 - 0.01 * (age_hours - 168) / 24)
        except Exception:
            return 0.5

    def _timeliness(self, node_id: str, store: Any) -> float:
        """时效性：基于节点关联证据的 event_date。

        查询节点关联的所有证据，取最近 event_date 计算时效性：
        - 7 天内：1.0
        - 30 天内：0.7
        - 90 天内：0.4
        - 更早或无事件：0.2

        Args:
            node_id: 节点 ID。
            store: GraphStore 实例。

        Returns:
            时效性 [0, 1]。
        """
        if not node_id:
            return 0.2

        # 收集节点关联的所有证据的 event_date
        evidence_dates: list[str] = []
        for edge in store.edges.values():
            if node_id in edge.subjects or node_id in edge.objects:
                for evidence_id in edge.evidence_ids + edge.counter_evidence_ids:
                    evidence = store.evidence.get(evidence_id)
                    if evidence and evidence.event_date:
                        evidence_dates.append(evidence.event_date)

        if not evidence_dates:
            return 0.2

        # 取最近的 event_date（ISO 字符串可比较）
        latest_date_str = max(evidence_dates)

        from datetime import datetime, timezone
        try:
            # 兼容 ISO 8601 各种格式（含 Z 后缀）
            ev_date = datetime.fromisoformat(latest_date_str.replace("Z", "+00:00"))
            if ev_date.tzinfo is None:
                ev_date = ev_date.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_days = (now - ev_date).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0.2

        if age_days <= 7:
            return 1.0
        elif age_days <= 30:
            return 0.7
        elif age_days <= 90:
            return 0.4
        else:
            return 0.2

    def _evidence_gap_score(
        self, gap: dict[str, Any], edge_id: str, store: Any
    ) -> float:
        """证据缺口分数：缺口类型映射。

        Args:
            gap: 缺口字典。
            edge_id: 关联边 ID。
            store: GraphStore 实例。

        Returns:
            证据缺口分数 [0, 1]。
        """
        gap_type = gap.get("type", "")
        if gap_type == "conflict":
            return 0.9  # 冲突缺口最需要补充证据
        if gap_type == "weak_edge":
            return 0.7  # 弱边需要证据支撑
        if gap_type == "island":
            return 0.4  # 孤岛节点需要建立连接
        return 0.5

    def _conflict_intensity(self, edge_id: str, store: Any) -> float:
        """冲突强度：反证数量归一化。

        Args:
            edge_id: 边 ID。
            store: GraphStore 实例。

        Returns:
            冲突强度 [0, 1]。
        """
        if not edge_id:
            return 0.0
        edge = store.edges.get(edge_id)
        if edge is None:
            return 0.0
        counter_count = len(edge.counter_evidence_ids or [])
        # 3 条反证满分
        return min(1.0, counter_count / 3.0)

    def _verifiability(self, node: Any) -> float:
        """可验证性：具体实体高，抽象概念低。

        Args:
            node: HyperNode 实例。

        Returns:
            可验证性 [0, 1]。
        """
        if node is None:
            return 0.3
        type_verifiability = {
            "COMPANY": 0.95,    # 公司可查财报
            "PRODUCT": 0.9,     # 产品可查规格
            "MATERIAL": 0.85,   # 材料可查物性
            "EQUIPMENT": 0.8,   # 设备可查参数
            "PROCESS": 0.7,     # 工艺可查专利
            "EVENT": 0.6,       # 事件可查公告
            "CONCEPT": 0.3,     # 概念难验证
            "THEME": 0.2,       # 主题最模糊
        }
        return type_verifiability.get(node.type, 0.4)

    def _estimated_cost(self, node_id: str, store: Any) -> float:
        """预计研究成本：度数越高成本越低（已有上下文）。

        Args:
            node_id: 节点 ID。
            store: GraphStore 实例。

        Returns:
            预计成本 [0, 1]（越高越贵）。
        """
        if not node_id:
            return 0.9
        degree = 0
        for edge in store.edges.values():
            if node_id in edge.subjects or node_id in edge.objects:
                degree += 1
        # 度数 10 以上成本最低 0.2，度数 0 成本最高 0.9
        return max(0.2, 0.9 - degree * 0.07)

    def _build_reason(
        self, gap: dict[str, Any], dimensions: dict[str, float], node_name: str
    ) -> str:
        """构建人类可读的评分理由。

        Args:
            gap: 缺口字典。
            dimensions: 各维度分数。
            node_name: 节点名称。

        Returns:
            评分理由字符串。
        """
        gap_type = gap.get("type", "unknown")
        parts = [f"缺口类型={gap_type}"]
        if node_name:
            parts.append(f"节点={node_name}")

        # 标注高分维度
        high_dims = [
            k for k, v in dimensions.items()
            if v >= 0.7 and k != "estimated_cost"
        ]
        if high_dims:
            parts.append(f"高分维度={','.join(high_dims)}")

        # 标注高成本
        if dimensions.get("estimated_cost", 0) >= 0.7:
            parts.append("高成本")

        return " | ".join(parts)
