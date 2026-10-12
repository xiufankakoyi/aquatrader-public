"""FrontierScorer：对候选节点和路径进行 Alpha 评分（v2 概率版）。

v2 优化：
- 概率聚合：Noisy-OR 模型替代线性加权，正确处理多证据协同
- 乘法惩罚：惩罚作为折扣因子，非减法
- 时间核衰减：指数核替代硬编码分段
- 图缓存：节点深度/分数缓存 + 邻接索引
- 保序校准：分数映射为真实概率
- 瓶颈检测修复：正确遍历入边/出边的bottleneck关系
- WeightRegistry 集成：权重从配置加载，支持贝叶斯校准

node_score 维度保持向后兼容：
- upstream_position_score: 上游位置越深越稀缺
- axis_score: 结构轴稀缺性
- bottleneck_score: 卡脖子程度
- irreplaceability_score: 不可替代性
- evidence_score: 证据强度（使用edge confidence）
- source_quality_score: 来源质量
- scarcity_score: 市场关注度低/稀缺
- domestic_substitution_score: 国产替代空间
- obscurity_score: 冷门程度
- valuation_score: 估值吸引力
- hype_penalty: 过热惩罚
- generic_penalty: 泛概念惩罚（一票否决）
- conflict_penalty: 证据冲突惩罚
- invalid_company_position_penalty: 公司位置非法惩罚
"""
from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Mapping
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    GENERIC_BLACKLIST,
    GENERIC_SUBSTRINGS,
    NodeType,
    RelationType,
    SourceType,
    StructuralAxis,
    get_source_weight,
)
from core.research.perilla.alpha.weight_registry import WeightRegistry
from core.research.perilla.alpha.probabilistic_aggregator import (
    IsotonicCalibrator,
    noisy_or_aggregate,
    apply_multiplicative_penalties,
    apply_vetoes,
)
from core.research.perilla.alpha.graph_cache import (
    AdjacencyIndex,
    NodeDepthCache,
    NodeScoreCache,
)
from core.research.perilla.alpha.temporal_kernel import TemporalKernel
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.alpha.flow_path_pruner import FlowPathPruner, PrunedPath
from core.research.perilla.alpha.path_discovery_schema import PathCandidate
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite(value, default)))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_get(store: Any, key: Any) -> Any:
    if not isinstance(store, Mapping):
        return None
    try:
        return store.get(key)
    except (TypeError, ValueError):
        return None


def _limit(value: Any, default: int, maximum: int = 1000) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


def _edges(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []

# 需要检测卡脖子的关系类型
_BOTTLENECK_RELATIONS = frozenset({
    RelationType.BOTTLENECK_OF.value,
    RelationType.IMPORT_DEPENDENT_ON.value,
    RelationType.CONSTRAINED_BY.value,
})

# 优先探索的上游节点类型
_PREFERRED_NODE_TYPES = frozenset({
    NodeType.MATERIAL.value,
    NodeType.EQUIPMENT.value,
    NodeType.PROCESS.value,
    NodeType.TESTING.value,
    NodeType.CONSUMABLE.value,
})

# 优先探索的轴
_PREFERRED_AXES = frozenset({
    StructuralAxis.MATERIAL.value,
    StructuralAxis.EQUIPMENT.value,
    StructuralAxis.PROCESS.value,
    StructuralAxis.CERTIFICATION.value,
})


class FrontierScorer:
    """前沿节点与路径评分器（v2 概率版）。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
        event_engine: Any | None = None,
        use_probabilistic: bool = True,
        use_cache: bool = True,
        calibrator: IsotonicCalibrator | None = None,
        weight_registry: WeightRegistry | None = None,
        research_repository: Any | None = None,
    ) -> None:
        """初始化评分器。

        Args:
            kb: SAG 知识库
            entity_resolver: 实体解析器
            event_engine: 动态事件引擎
            use_probabilistic: 是否使用概率聚合（False则用legacy线性加权）
            use_cache: 是否启用缓存
            calibrator: 保序回归校准器
            weight_registry: 权重注册表
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()
        self.event_engine = event_engine
        self.use_probabilistic = use_probabilistic
        self.use_cache = use_cache
        self.calibrator = calibrator
        self.registry = weight_registry or WeightRegistry.get_instance()
        self._alpha_weight_multiplier = 1.0
        # 节点正向维度权重：registry 默认值 + 校准策略覆盖
        self._calibrated_node_weights: dict[str, dict] = dict(self.registry.get_node_weights())
        try:
            if research_repository is None:
                from core.research.control_plane.repository import ResearchRepository
                research_repository = ResearchRepository()
            active_alpha_policy = research_repository.get_active_policy("alpha")
            configured = active_alpha_policy.parameters.get("alpha_weight_multiplier") if active_alpha_policy else None
            if isinstance(configured, (int, float)):
                self._alpha_weight_multiplier = max(0.8, min(1.2, _finite(configured, 1.0)))
            # 校准的节点维度权重覆盖 registry 默认值，缺失维度回退默认
            if active_alpha_policy is not None:
                calibrated = active_alpha_policy.parameters.get("calibrated_node_weights", {})
                if isinstance(calibrated, dict):
                    for dim_name, dim_cfg in calibrated.items():
                        if isinstance(dim_cfg, dict):
                            self._calibrated_node_weights[str(dim_name)] = dim_cfg
        except Exception as exc:
            logger.debug("读取 Alpha 校准策略失败，使用默认乘数: %s", exc)

        # 时间核组件（指数衰减 + 内置Hawkes自激）
        self._temporal_kernel = TemporalKernel(registry=self.registry)

        # 缓存组件
        performance_cfg = _mapping(self.registry.get_performance_config())
        cache_cfg = _mapping(performance_cfg.get("cache"))
        self._depth_cache = NodeDepthCache(
            max_size=_limit(cache_cfg.get("node_depth_cache_size", 10000), 10000, 1000000)
        ) if use_cache else None
        self._score_cache = NodeScoreCache(
            ttl_seconds=_limit(cache_cfg.get("node_score_cache_ttl_seconds", 300), 300, 86400)
        ) if use_cache else None
        self._adj_index = AdjacencyIndex() if use_cache else None
        if self._adj_index is not None:
            self._rebuild_adj_index()

    def _rebuild_adj_index(self) -> None:
        """重建邻接索引。"""
        if self._adj_index is not None:
            self._adj_index.rebuild(getattr(self.kb, "edges", {}))

    def _invalidate_caches(self, node_name: str | None = None) -> None:
        """失效缓存。"""
        if not self.use_cache:
            return
        if node_name:
            if self._depth_cache:
                self._depth_cache.invalidate_node(node_name)
            if self._score_cache:
                self._score_cache.invalidate_node(node_name)
        else:
            if self._depth_cache:
                self._depth_cache.invalidate_all()
            if self._score_cache:
                self._score_cache.invalidate_all()
            self._rebuild_adj_index()

    def _get_neighbors_cached(self, node_name: str, direction: str = "out") -> list[Any]:
        """缓存版邻居查询。"""
        edges = getattr(self.kb, "edges", {})
        if self._adj_index is not None:
            if direction == "out":
                keys = self._adj_index.get_out(node_name)
            elif direction == "in":
                keys = self._adj_index.get_in(node_name)
            else:
                keys = self._adj_index.get_both(node_name)
            return [edges[k] for k in keys if isinstance(edges, Mapping) and k in edges]
        getter = getattr(self.kb, "get_neighbors", None)
        if not callable(getter):
            return []
        try:
            return _edges(getter(node_name, direction=direction))
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.debug("邻居查询失败: %s", node_name, exc_info=True)
            return []

    @staticmethod
    def _is_generic(name: str) -> bool:
        """判断是否为泛概念。"""
        name_clean = name.strip().lower()
        if len(name_clean) <= 1:
            return True
        if name_clean in GENERIC_BLACKLIST:
            return True
        if any(gs in name_clean for gs in GENERIC_SUBSTRINGS):
            return True
        return False

    def _ensure_edge_confidence(self, edge: Any) -> float:
        """确保边有有效的置信度（修复claim_score=0时confidence为0的问题）。"""
        conf = _score(_field(edge, "confidence", 0.0))
        if conf > 0:
            return conf
        # fallback: 从source_weights和claim的confidence推断
        weights = []
        source_values = _field(edge, "source_weights", {})
        if isinstance(source_values, Mapping) and source_values:
            weights.extend(_score(value) for value in source_values.values())
        if weights:
            # Noisy-OR 聚合source weights
            one_minus = 1.0
            for w in weights:
                one_minus *= (1.0 - min(0.99, _score(w)))
            return max(0.01, 1.0 - one_minus)
        # 从claim confidence推断
        claim_scores = []
        claim_ids = _field(edge, "claim_ids", [])
        claim_ids = [claim_ids] if isinstance(claim_ids, str) else claim_ids
        if isinstance(claim_ids, (list, tuple, set)):
            claims = getattr(self.kb, "claims", {})
            for cid in claim_ids:
                c = _mapping_get(claims, cid)
                if c:
                    cs = _score(_field(c, "claim_score", 0.0))
                    cc = _score(_field(c, "confidence", 0.0))
                    source_type = _field(c, "source_type", "default")
                    claim_scores.append(max(cs, cc * get_source_weight(source_type)))
        if claim_scores:
            one_minus = 1.0
            for s in claim_scores:
                one_minus *= (1.0 - min(0.99, _score(s)))
            return max(0.01, 1.0 - one_minus)
        return 0.1  # 默认低置信度

    def _node_depth(self, root: str, node_name: str) -> int:
        """计算节点到root的最短深度（带缓存的BFS）。"""
        if self._depth_cache is not None:
            cached = self._depth_cache.get(root, node_name)
            if cached is not None:
                return cached

        if root == node_name:
            depth = 0
            if self._depth_cache:
                self._depth_cache.set(root, node_name, depth)
            return depth

        # BFS
        visited = {root: 0}
        queue = deque([root])
        depth = 10  # 默认大深度
        while queue:
            current = queue.popleft()
            current_depth = visited[current]
            for edge in self._get_neighbors_cached(current, direction="out"):
                tail = _field(edge, "tail")
                if not isinstance(tail, str):
                    continue
                if tail not in visited:
                    visited[tail] = current_depth + 1
                    if self._depth_cache:
                        self._depth_cache.set(root, tail, current_depth + 1)
                    if tail == node_name:
                        depth = visited[tail]
                        return depth
                    queue.append(tail)
        return depth

    def _compute_bottleneck_score(self, node_name: str, all_edges: list) -> float:
        """计算节点的瓶颈分数（遍历所有方向的边）。"""
        best = 0.0
        for e in all_edges:
            if _field(e, "relation") in _BOTTLENECK_RELATIONS:
                conf = self._ensure_edge_confidence(e)
                best = max(best, conf)
        return best

    def _compute_domestic_substitution_score(self, all_edges: list) -> float:
        """计算国产替代分数。"""
        best = 0.0
        for e in all_edges:
            if _field(e, "relation") in (RelationType.IMPORT_DEPENDENT_ON.value, RelationType.SUBSTITUTE_FOR.value):
                conf = self._ensure_edge_confidence(e)
                best = max(best, conf)
        return best

    def score_node(self, root: str, node_name: str) -> dict[str, Any]:
        """对单个节点进行多维度评分。

        Args:
            root: 根概念
            node_name: 节点名

        Returns:
            {"node_name": str, "total": float, "dimensions": dict, "mode": str}
        """
        # 检查缓存
        root = str(root or "")
        node_name = str(node_name or "")
        if self._score_cache is not None:
            cached = self._score_cache.get(root, node_name)
            if cached is not None:
                return cached

        nodes = getattr(self.kb, "nodes", {})
        node = _mapping_get(nodes, node_name)
        if not node:
            result = {"node_name": node_name, "total": 0.0, "dimensions": {}, "mode": "probabilistic" if self.use_probabilistic else "legacy"}
            return result

        node_type = _field(node, "node_type", "")
        node_axes = _field(node, "axes", [])
        node_axes = [axis for axis in node_axes if isinstance(axis, str)] if isinstance(node_axes, (list, tuple)) else []
        source_count = _limit(_field(node, "source_count", 0), 0, 1000000)

        edges_out = self._get_neighbors_cached(node_name, direction="out")
        edges_in = self._get_neighbors_cached(node_name, direction="in")
        all_edges = edges_out + edges_in

        # 证据分数：边的平均置信度（使用修复后的置信度）
        evidence_score = 0.0
        if all_edges:
            confs = [self._ensure_edge_confidence(e) for e in all_edges]
            evidence_score = sum(confs) / len(confs)

        # 来源质量分数
        source_quality_score = 0.0
        if all_edges:
            weights = []
            for e in all_edges:
                source_values = _field(e, "source_weights", {})
                if isinstance(source_values, Mapping):
                    weights.extend(_score(value) for value in source_values.values())
            if weights:
                source_quality_score = sum(weights) / len(weights)

        # 上游位置分
        depth = self._node_depth(root, node_name)
        upstream_position_score = min(1.0, depth / 5.0)

        # 结构轴稀缺性
        axis_score = 0.0
        if node_axes:
            match = set(node_axes) & _PREFERRED_AXES
            axis_score = min(1.0, len(match) * 0.4)

        # 卡脖子分数（修复版）
        bottleneck_score = self._compute_bottleneck_score(node_name, all_edges)

        # 国产替代空间
        domestic_substitution_score = self._compute_domestic_substitution_score(all_edges)

        # 不可替代性
        irreplaceability_score = 0.0
        if edges_in:
            irreplaceability_score = min(1.0, len(edges_in) / (len(edges_out) + 1) / 3.0)

        # 稀缺性/冷门
        scarcity_cfg = _mapping(self.registry.get("node_scoring", "scarcity", {}))
        few_thresh = scarcity_cfg.get("few_sources_threshold", 2)
        few_score = _score(scarcity_cfg.get("few_sources_score", 0.6), 0.6)
        some_thresh = scarcity_cfg.get("some_sources_threshold", 4)
        some_score = _score(scarcity_cfg.get("some_sources_score", 0.3), 0.3)
        min_ev = _score(scarcity_cfg.get("min_evidence_for_scarcity", 0.5), 0.5)
        few_thresh = _limit(few_thresh, 2, 1000000)
        some_thresh = _limit(some_thresh, 4, 1000000)

        scarcity_score = 0.0
        if source_count <= few_thresh and evidence_score > min_ev:
            scarcity_score = few_score
        elif source_count <= some_thresh and evidence_score > min_ev:
            scarcity_score = some_score

        # 估值分数
        valuation_score = 0.0
        for e in edges_out:
            if _field(e, "relation") == RelationType.PERFORMANCE_DRIVER_OF.value and "估值" in str(_field(e, "tail", "")):
                valuation_score = self._ensure_edge_confidence(e)

        # 过热惩罚
        hype_cfg = _mapping(self.registry.get("node_scoring", "hype", {}))
        hype_thresh = _limit(hype_cfg.get("source_count_threshold", 10), 10, 1000000)
        hype_per = _score(hype_cfg.get("penalty_per_extra_source", 0.02), 0.02)
        hype_max = _score(hype_cfg.get("max_penalty", 0.4), 0.4)
        hype_penalty = 0.0
        if source_count > hype_thresh:
            hype_penalty = min(hype_max, (source_count - hype_thresh) * hype_per)

        # 泛概念惩罚（一票否决）
        is_generic = self._is_generic(node_name)
        generic_penalty = 1.0 if is_generic else 0.0

        # 公司位置非法惩罚
        invalid_company_position_penalty = 0.0
        validator = getattr(self.entity_resolver, "is_company_position_valid", None)
        for e in all_edges:
            try:
                valid = callable(validator) and validator(
                    _field(e, "head", ""),
                    _field(e, "relation", ""),
                    _field(e, "tail", ""),
                )
            except (AttributeError, TypeError, ValueError):
                valid = True
            if not valid:
                invalid_company_position_penalty = max(invalid_company_position_penalty, 0.3)

        # 冲突惩罚
        conflict_penalty = 0.0
        low_conf_sources = 0
        for e in all_edges:
            source_values = _field(e, "source_weights", {})
            for _source_type, w in source_values.items() if isinstance(source_values, Mapping) else ():
                if _score(w) < 0.5 and self._ensure_edge_confidence(e) < 0.5:
                    low_conf_sources += 1
        conflict_penalty = min(0.3, low_conf_sources * 0.1)

        # 修正节点类型
        try:
            resolved = self.entity_resolver.resolve(node_name)
        except (AttributeError, TypeError, ValueError):
            resolved = None
        ontology = getattr(self.kb, "ontology_registry", None)
        if (
            resolved is not None
            and _score(_field(resolved, "resolver_confidence", 0.0)) >= 0.7
            and callable(getattr(ontology, "is_valid_node_type", None))
            and ontology.is_valid_node_type(_field(resolved, "node_type", ""))
        ):
            node_type = _field(resolved, "node_type", node_type)
            if isinstance(node, Mapping):
                try:
                    node["node_type"] = node_type
                except (TypeError, ValueError):
                    pass
            else:
                try:
                    node.node_type = node_type
                except (AttributeError, TypeError):
                    pass

        dimensions = {
            "upstream_position_score": upstream_position_score,
            "axis_score": axis_score,
            "bottleneck_score": bottleneck_score,
            "irreplaceability_score": irreplaceability_score,
            "evidence_score": evidence_score,
            "source_quality_score": source_quality_score,
            "scarcity_score": scarcity_score,
            "domestic_substitution_score": domestic_substitution_score,
            "obscurity_score": scarcity_score,
            "valuation_score": valuation_score,
            "hype_penalty": hype_penalty,
            "generic_penalty": generic_penalty,
            "invalid_company_position_penalty": invalid_company_position_penalty,
            "conflict_penalty": conflict_penalty,
        }

        if self.use_probabilistic:
            total = self._probabilistic_node_score(dimensions)
        else:
            total = self._legacy_node_score(dimensions)

        result = {
            "node_name": node_name,
            "node_type": node_type,
            "axes": node_axes,
            "total": round(total, 3),
            "dimensions": {k: round(v, 3) for k, v in dimensions.items()},
            "mode": "probabilistic" if self.use_probabilistic else "legacy",
        }

        if self._score_cache is not None:
            self._score_cache.set(root, node_name, result)
        return result

    def _legacy_node_score(self, dimensions: dict[str, float]) -> float:
        """Legacy线性加权评分（用于对比）。"""
        weights = {
            "upstream_position_score": 0.10, "axis_score": 0.15, "bottleneck_score": 0.15,
            "irreplaceability_score": 0.10, "evidence_score": 0.15, "source_quality_score": 0.10,
            "scarcity_score": 0.10, "domestic_substitution_score": 0.10,
            "obscurity_score": 0.05, "valuation_score": 0.05,
            "hype_penalty": -0.10, "generic_penalty": -1.00,
            "invalid_company_position_penalty": -0.30, "conflict_penalty": -0.10,
        }
        total = 0.0
        for key, value in dimensions.items():
            w = weights.get(key, 0)
            if w < 0:
                total += value * w  # 惩罚项
            else:
                total += value * w
        return max(0.0, min(1.0, total))

    def _probabilistic_node_score(self, dimensions: dict[str, float]) -> float:
        """概率聚合评分（Noisy-OR + 乘法惩罚）。"""
        pos_weights = self._calibrated_node_weights
        pen_configs = self.registry.get_node_penalties()

        # 正向特征
        positive_features = {
            k: v for k, v in dimensions.items()
            if not k.endswith("_penalty") and v > 0
        }
        # 惩罚特征
        penalty_features = {
            k: v for k, v in dimensions.items()
            if k.endswith("_penalty") and k != "generic_penalty" and v > 0
        }
        # 一票否决
        veto_features = {
            "generic_penalty": dimensions.get("generic_penalty", 0) > 0.5,
        }

        # Noisy-OR聚合正向证据（先验5%）
        pos_prob = noisy_or_aggregate(positive_features, pos_weights, prior=0.05)

        # 乘法惩罚
        penalized = apply_multiplicative_penalties(pos_prob, penalty_features, pen_configs)

        # 一票否决
        final = apply_vetoes(penalized, veto_features)

        # 保序校准
        if self.calibrator is not None and getattr(self.calibrator, "_fitted", False):
            try:
                final = _score(self.calibrator.transform(final))
            except (AttributeError, TypeError, ValueError, RuntimeError):
                logger.debug("校准器转换失败", exc_info=True)

        return _score(final)

    def score_path(
        self,
        root: str,
        path: list[str],
        event_overlay: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """对整条路径进行 Alpha 评分。

        Args:
            root: 根概念
            path: 节点名列表
            event_overlay: 事件overlay

        Returns:
            评分结果字典
        """
        path = [str(item) for item in path if item is not None] if isinstance(path, (list, tuple)) else []
        root = str(root or "")
        event_overlay = dict(event_overlay) if isinstance(event_overlay, Mapping) else None
        validator = getattr(self.entity_resolver, "is_company_position_valid", None)
        if len(path) < 2:
            return {
                "path": path, "path_alpha_score": 0.0, "event_adjusted_score": 0.0,
                "event_overlay": event_overlay, "node_scores": [], "gaps": [],
            }

        node_scores = [self.score_node(root, n) for n in path]
        valid_node_scores = [s["total"] for s in node_scores if s["total"] > 0]
        if not valid_node_scores:
            return {"path": path, "path_alpha_score": 0.0, "node_scores": node_scores, "gaps": []}

        path_cfg = _mapping(self.registry.get_path_weights())
        len_cfg = _mapping(path_cfg.get("length_factors"))
        bonus_cfg = _mapping(path_cfg.get("bonuses"))
        event_cfg = _mapping(path_cfg.get("event_adjustment"))
        agg_cfg = _mapping(path_cfg.get("node_aggregation"))

        # 长度因子
        length_factor = 1.0
        if len(path) < 3:
            length_factor = _score(len_cfg.get("too_short_penalty", 0.7), 0.7)
        elif len(path) > max(1, int(_finite(len_cfg.get("ideal_max", 6), 6))):
            length_factor = _score(len_cfg.get("too_long_penalty", 0.8), 0.8)

        # 尾节点bonus
        tail_bonus = 0.0
        nodes = getattr(self.kb, "nodes", {})
        tail_node = _mapping_get(nodes, path[-1])
        if tail_node:
            tail_type = _field(tail_node, "node_type", "")
            if tail_type == NodeType.COMPANY.value:
                tail_bonus = _score(bonus_cfg.get("tail_company_bonus", 0.10), 0.10)
            elif tail_type in _PREFERRED_NODE_TYPES:
                tail_bonus = _score(bonus_cfg.get("tail_specific_node_bonus", 0.20), 0.20)

        # 方向分
        preferred_count = sum(
            1 for n in path[1:]
            if _field(_mapping_get(nodes, n), "node_type", "") in _PREFERRED_NODE_TYPES
        )
        direction_bonus = min(
            _score(bonus_cfg.get("direction_bonus_max", 0.20), 0.20),
            preferred_count * _score(bonus_cfg.get("direction_bonus_per_node", 0.07), 0.07)
        )

        # 多样性
        axes_set: set[str] = set()
        for s in node_scores:
            axes = s.get("axes", [])
            if isinstance(axes, (list, tuple, set)):
                axes_set.update(axis for axis in axes if isinstance(axis, str))
        diversity_bonus = min(
            _score(bonus_cfg.get("diversity_bonus_max", 0.15), 0.15),
            len(axes_set) * _score(bonus_cfg.get("diversity_bonus_per_axis", 0.03), 0.03)
        )
        if axes_set & _PREFERRED_AXES:
            diversity_bonus += _score(bonus_cfg.get("preferred_axis_extra_bonus", 0.05), 0.05)

        # 多源
        path_source_types: set[str] = set()
        for i in range(len(path) - 1):
            head, tail = path[i], path[i + 1]
            for edge in self._get_neighbors_cached(head, direction="out"):
                if _field(edge, "tail") == tail:
                    source_values = _field(edge, "source_weights", {})
                    if isinstance(source_values, Mapping):
                        path_source_types.update(
                            key for key in source_values if isinstance(key, str)
                        )
                    break
        multi_source_bonus = (
            min(_score(bonus_cfg.get("multi_source_max", 0.12), 0.12),
                (len(path_source_types) - 1) * _score(bonus_cfg.get("multi_source_per_type", 0.06), 0.06))
            if len(path_source_types) > 1 else 0.0
        )

        # 公司位置非法惩罚（乘法）
        invalid_position_penalty = 1.0
        for i in range(len(path) - 1):
            head, tail = path[i], path[i + 1]
            for edge in self._get_neighbors_cached(head, direction="out"):
                if _field(edge, "tail") == tail:
                    try:
                        valid = callable(validator) and validator(
                            _field(edge, "head", head),
                            _field(edge, "relation", ""),
                            _field(edge, "tail", tail),
                        )
                    except (AttributeError, TypeError, ValueError):
                        valid = True
                    if not valid:
                        invalid_position_penalty = min(invalid_position_penalty, 0.8)
                    break

        # 路径组合
        avg_node_score = sum(valid_node_scores) / len(valid_node_scores)
        max_bottleneck = max(
            (s["dimensions"].get("bottleneck_score", 0) for s in node_scores), default=0
        )

        avg_w = _score(agg_cfg.get("avg_node_weight", 0.45), 0.45)
        max_w = _score(agg_cfg.get("max_bottleneck_weight", 0.20), 0.20)

        if self.use_probabilistic:
            # 概率模式：基础分用Noisy-OR思想，bonus是加法但有cap
            base = avg_node_score * avg_w + max_bottleneck * max_w
            # bonus使用sigmoid饱和而非简单相加
            total_bonus = tail_bonus + direction_bonus + diversity_bonus + multi_source_bonus
            # bonus不超过base的50%，防止bonus主导
            total_bonus = min(total_bonus, base * 0.5)
            path_alpha_score = (base + total_bonus) * length_factor * invalid_position_penalty
        else:
            # Legacy模式
            path_alpha_score = (
                avg_node_score * avg_w + max_bottleneck * max_w
                + tail_bonus + direction_bonus + diversity_bonus + multi_source_bonus
            ) * length_factor * invalid_position_penalty

        path_alpha_score = max(0.0, min(1.0, path_alpha_score))

        # 证据缺口
        gaps: list[dict[str, Any]] = []
        for i in range(len(path) - 1):
            head, tail = path[i], path[i + 1]
            for edge in self._get_neighbors_cached(head, direction="out"):
                if _field(edge, "tail") == tail:
                    source_values = _field(edge, "source_weights", {})
                    if not isinstance(source_values, Mapping) or len(source_values) < 2:
                        gaps.append({
                            "head": head, "tail": tail, "relation": _field(edge, "relation", ""),
                            "missing": "多源证据",
                        })
                    break

        # 事件调整（概率模式使用乘法，legacy使用加法）
        if event_overlay is not None:
            event_boost = _score(event_overlay.get("event_boost_score", 0.0))
            event_risk = _score(event_overlay.get("event_risk_score", 0.0))
            freshness = _score(event_overlay.get("freshness_score", 0.0))
            promotion_pressure = _score(event_overlay.get("promotion_pressure", 0.0))
            contradiction_pressure = _score(event_overlay.get("contradiction_pressure", 0.0))

            if self.use_probabilistic:
                # 乘法模式：boost是乘数加成，risk/contradiction是折扣
                boost_factor = 1.0 + event_boost * _score(event_cfg.get("boost_scale", 0.25), 0.25)
                risk_factor = 1.0 - event_risk * _score(event_cfg.get("risk_scale", 0.35), 0.35)
                contra_factor = 1.0 - contradiction_pressure * _score(event_cfg.get("contradiction_scale", 0.40), 0.40)
                freshness_bonus = freshness * _score(event_cfg.get("freshness_scale", 0.05), 0.05)
                promo_factor = 1.0 + promotion_pressure * _score(event_cfg.get("promotion_pressure_scale", 0.10), 0.10)

                event_adjusted = path_alpha_score * boost_factor * risk_factor * contra_factor * promo_factor + freshness_bonus
            else:
                event_adjusted = (
                    path_alpha_score
                    + event_boost * _score(event_cfg.get("boost_scale", 0.25), 0.25)
                    - event_risk * _score(event_cfg.get("risk_scale", 0.35), 0.35)
                    - contradiction_pressure * _score(event_cfg.get("contradiction_scale", 0.40), 0.40)
                    + freshness * _score(event_cfg.get("freshness_scale", 0.05), 0.05)
                    + promotion_pressure * _score(event_cfg.get("promotion_pressure_scale", 0.10), 0.10)
                )
            event_adjusted_score = max(0.0, min(1.0, event_adjusted))
        else:
            event_adjusted_score = path_alpha_score

        return {
            "path": path,
            "path_alpha_score": round(path_alpha_score, 3),
            "event_adjusted_score": round(event_adjusted_score, 3),
            "event_overlay": event_overlay,
            "avg_node_score": round(avg_node_score, 3),
            "max_bottleneck_score": round(max_bottleneck, 3),
            "tail_bonus": round(tail_bonus, 3),
            "diversity_bonus": round(diversity_bonus, 3),
            "length_factor": round(length_factor, 3),
            "node_scores": node_scores,
            "gaps": gaps,
        }

    def enumerate_paths(
        self,
        root: str,
        max_depth: int = 5,
        max_paths: int = 500,
    ) -> list[dict[str, Any]]:
        """枚举 root 下游路径（带缓存优化）。"""
        root = str(root or "")
        max_depth = _limit(max_depth, 5, 50)
        max_paths = _limit(max_paths, 500, 5000)
        if not root or max_paths <= 0:
            return []
        event_result: Any = None
        if self.event_engine is not None:
            try:
                event_result = self.event_engine.propagate_for_root(root)
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.debug("事件路径 overlay 获取失败: %s", root, exc_info=True)

        event_overlays: dict[str, dict[str, Any]] = {}
        edge_activation: dict[tuple[str, str, str], float] = {}
        if event_result is not None:
            event_overlays = _field(event_result, "path_overlays", {})
            edge_activation = _field(event_result, "edge_activation", {})
            event_overlays = dict(event_overlays) if isinstance(event_overlays, Mapping) else {}
            edge_activation = dict(edge_activation) if isinstance(edge_activation, Mapping) else {}

        paths: list[list[str]] = []
        stack: list[tuple[str, list[str], set[str]]] = [(root, [root], {root})]

        while stack and len(paths) < max_paths:
            current, path, visited = stack.pop()
            if len(path) >= max_depth:
                paths.append(path)
                continue

            edges = self._get_neighbors_cached(current, direction="out")
            if not edges:
                paths.append(path)
                continue

            def _edge_priority(edge: Any) -> tuple[float, bool]:
                tail = _field(edge, "tail", "")
                tail_node = _mapping_get(getattr(self.kb, "nodes", {}), tail)
                is_preferred = (
                    tail_node is not None
                    and _field(tail_node, "node_type", "") in _PREFERRED_NODE_TYPES
                )
                conf = self._ensure_edge_confidence(edge)
                return (-conf, not is_preferred)

            edges.sort(key=_edge_priority)
            validator = getattr(self.entity_resolver, "is_company_position_valid", None)
            expanded = False
            for edge in edges:
                tail = _field(edge, "tail")
                if not isinstance(tail, str) or tail in visited:
                    continue
                try:
                    valid = callable(validator) and validator(
                        _field(edge, "head", current),
                        _field(edge, "relation", ""),
                        tail,
                    )
                except (AttributeError, TypeError, ValueError):
                    valid = True
                if not valid:
                    continue
                new_path = path + [tail]
                new_visited = visited | {tail}
                stack.append((tail, new_path, new_visited))
                expanded = True
            if not expanded:
                paths.append(path)

        def _path_edge_keys(path: list[str]) -> list[tuple[str, str, str]]:
            keys: list[tuple[str, str, str]] = []
            for i in range(len(path) - 1):
                head, tail = path[i], path[i + 1]
                for edge in self._get_neighbors_cached(head, direction="out"):
                    if _field(edge, "tail") == tail:
                        edge_key = (
                            _field(edge, "head", head),
                            _field(edge, "relation", ""),
                            _field(edge, "tail", tail),
                        )
                        if all(isinstance(item, str) for item in edge_key):
                            keys.append(edge_key)
                        break
            return keys

        scored: list[dict[str, Any]] = []
        for path in paths:
            path_id = "->".join(path)
            overlay = event_overlays.get(path_id)
            path_data = self.score_path(root, path, event_overlay=overlay)

            if overlay is not None:
                overlay = overlay if isinstance(overlay, Mapping) else {}
                event_boost = _score(overlay.get("event_boost_score", 0.0))
                event_risk = _score(overlay.get("event_risk_score", 0.0))
                promotion_pressure = _score(overlay.get("promotion_pressure", 0.0))
                contradiction_pressure = _score(overlay.get("contradiction_pressure", 0.0))
                cat_cfg = _mapping(self.registry.get("path_scoring", "category_thresholds", {}))
                contradiction_threshold = _score(cat_cfg.get("contradiction_pressure", 0.15), 0.15)
                event_risk_threshold = _score(cat_cfg.get("event_risk", 0.15), 0.15)
                promotion_threshold = _score(cat_cfg.get("promotion_pressure", 0.15), 0.15)
                confidence_threshold = _score(cat_cfg.get("high_confidence", 0.6), 0.6)
                if contradiction_pressure > contradiction_threshold:
                    path_category = "contradiction_candidate_path"
                elif event_risk > event_risk_threshold:
                    path_category = "event_risk_path"
                elif promotion_pressure > promotion_threshold:
                    path_category = "promotion_candidate_path"
                elif event_boost > 0.1 and len(path_data.get("gaps", [])) > 0:
                    path_category = "high_priority_probe_path"
                elif event_boost > 0.1:
                    path_category = "event_boosted_path"
                elif _score(path_data.get("path_alpha_score")) >= confidence_threshold:
                    path_category = "stable_high_confidence_path"
                else:
                    path_category = "normal"
            else:
                cat_cfg = _mapping(self.registry.get("path_scoring", "category_thresholds", {}))
                path_category = "stable_high_confidence_path" if _score(path_data.get("path_alpha_score")) >= _score(cat_cfg.get("high_confidence", 0.6), 0.6) else "normal"

            edge_keys = _path_edge_keys(path)
            if edge_keys and edge_activation:
                ppr_activation_score = sum(_score(edge_activation.get(k, 0.0)) for k in edge_keys) / len(edge_keys)
            else:
                ppr_activation_score = 0.0

            path_data["path_category"] = path_category
            path_data["ppr_activation_score"] = round(ppr_activation_score, 6)
            scored.append(path_data)

        scored.sort(key=lambda x: _score(x.get("path_alpha_score")), reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Path-based discovery ranking
    # ------------------------------------------------------------------

    def score_path_candidate(
        self,
        pruned_path: PrunedPath,
        event_overlay: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """对剪枝后的路径候选进行综合评分。"""
        if not isinstance(pruned_path, PrunedPath) or not isinstance(
            pruned_path.path_candidate, PathCandidate
        ):
            return {
                "path_id": "",
                "path": [],
                "path_category": "weak_path",
                "final_score": 0.0,
                "event_adjusted_score": 0.0,
                "evidence_gap_score": 1.0,
                "evidence_gaps": [],
            }
        candidate = pruned_path.path_candidate
        path = [str(item) for item in candidate.nodes if item is not None]
        edges = list(candidate.edges) if isinstance(candidate.edges, (list, tuple)) else []

        node_scores = [self.score_node(candidate.root_entity, n) for n in path]
        valid_node_scores = [s["total"] for s in node_scores if s["total"] > 0]
        avg_node_score = sum(valid_node_scores) / max(1, len(valid_node_scores))

        flow_score = _score(pruned_path.flow_score)
        component_scores = (
            dict(pruned_path.component_scores)
            if isinstance(pruned_path.component_scores, Mapping)
            else {}
        )

        event_boost = event_risk = promotion_pressure = contradiction_pressure = 0.0
        event_overlay = dict(event_overlay) if isinstance(event_overlay, Mapping) else None
        if event_overlay:
            event_boost = _score(event_overlay.get("event_boost_score", 0.0))
            event_risk = _score(event_overlay.get("event_risk_score", 0.0))
            promotion_pressure = _score(event_overlay.get("promotion_pressure", 0.0))
            contradiction_pressure = _score(event_overlay.get("contradiction_pressure", 0.0))

        edge_count = max(1, len(edges))
        graph_edges = getattr(self.kb, "edges", {})
        missing_edge_count = sum(
            1 for e in edges if not _field(_mapping_get(graph_edges, e), "claim_ids", [])
        )
        evidence_gap_score = missing_edge_count / edge_count

        bottleneck_prior_score = 0.0
        bottleneck_types = {
            "material_bottleneck_path", "equipment_bottleneck_path", "process_barrier_path",
            "certification_barrier_path", "import_dependency_path",
        }
        if candidate.path_type in bottleneck_types:
            bottleneck_prior_score = 0.25
        for ns in node_scores:
            bottleneck_prior_score = max(
                bottleneck_prior_score,
                ns.get("dimensions", {}).get("bottleneck_score", 0) * 0.5,
            )

        contradiction_risk_score = contradiction_pressure
        for e in edges:
            edge = _mapping_get(graph_edges, e)
            if edge and _field(edge, "conflict_status", "none") != "none":
                contradiction_risk_score = max(contradiction_risk_score, 0.3)

        if self.use_probabilistic:
            structural_score = avg_node_score * 0.3 + flow_score * 0.4 + bottleneck_prior_score * 0.3
            event_adjusted_score = structural_score * (1.0 + event_boost * 0.1) * (1.0 - event_risk * 0.15)
            final_score = event_adjusted_score * 0.6 + (1 - evidence_gap_score) * 0.2 * event_adjusted_score + promotion_pressure * 0.1 - contradiction_risk_score * 0.1
        else:
            structural_score = avg_node_score * 0.3 + flow_score * 0.4 + bottleneck_prior_score * 0.3
            event_adjusted_score = structural_score + event_boost * 0.1 - event_risk * 0.15
            final_score = (
                event_adjusted_score * 0.6
                + (1 - evidence_gap_score) * 0.2
                + promotion_pressure * 0.1
                - contradiction_risk_score * 0.1
            )
        final_score = _score(final_score * _score(self._alpha_weight_multiplier, 1.0))

        path_category = self._classify_path_category(
            final_score=final_score,
            evidence_gap_score=evidence_gap_score,
            event_boost=event_boost, event_risk=event_risk,
            promotion_pressure=promotion_pressure,
            contradiction_pressure=contradiction_pressure,
        )

        evidence_gaps: list[dict[str, Any]] = []
        for edge_key in edges:
            edge = _mapping_get(graph_edges, edge_key)
            edge_parts = list(edge_key) if isinstance(edge_key, (list, tuple)) else []
            edge_parts = edge_parts if len(edge_parts) == 3 else ["", "", ""]
            if edge is None or not _field(edge, "claim_ids", []):
                evidence_gaps.append({
                    "head": edge_parts[0], "tail": edge_parts[2], "relation": edge_parts[1],
                    "missing": "evidence",
                })
            elif not isinstance(_field(edge, "source_weights", {}), Mapping) or len(_field(edge, "source_weights", {})) < 2:
                evidence_gaps.append({
                    "head": edge_parts[0], "tail": edge_parts[2], "relation": edge_parts[1],
                    "missing": "多源证据",
                })

        return {
            "path_id": candidate.path_id, "path": path, "path_type": candidate.path_type,
            "path_category": path_category, "meta_path": candidate.meta_path,
            "root_entity": candidate.root_entity, "target_entity": candidate.target_entity,
            "hop_count": _limit(candidate.hop_count, 0, 1000), "node_scores": node_scores,
            "flow_score": flow_score, "structural_score": round(structural_score, 3),
            "path_flow_score": round(flow_score, 3),
            "event_adjusted_score": round(event_adjusted_score, 3),
            "evidence_gap_score": round(evidence_gap_score, 3),
            "bottleneck_prior_score": round(bottleneck_prior_score, 3),
            "contradiction_risk_score": round(contradiction_risk_score, 3),
            "final_score": round(final_score, 3),
            "alpha_policy_multiplier": round(self._alpha_weight_multiplier, 4),
            "component_scores": component_scores,
            "event_boost": round(event_boost, 3), "event_risk": round(event_risk, 3),
            "axes": candidate.axes if isinstance(candidate.axes, list) else [], "edges": edges,
            "evidence_gaps": evidence_gaps, "_candidate": candidate,
        }

    @staticmethod
    def _classify_path_category(
        final_score: float, evidence_gap_score: float,
        event_boost: float, event_risk: float,
        promotion_pressure: float, contradiction_pressure: float,
    ) -> str:
        if contradiction_pressure > 0.15 or event_risk > 0.2:
            return "contradiction_risk_path"
        if promotion_pressure > 0.15 and evidence_gap_score < 0.3:
            return "promotion_candidate_path"
        if final_score >= 0.65 and evidence_gap_score < 0.3:
            return "high_confidence_alpha_path"
        if event_boost > 0.1 and evidence_gap_score >= 0.3:
            return "event_boosted_but_unverified_path"
        if final_score >= 0.4 and evidence_gap_score >= 0.3:
            return "high_potential_probe_path"
        if final_score < 0.25:
            return "weak_path"
        return "normal_path"

    def rank_pruned_paths(
        self,
        pruned_paths: list[PrunedPath],
        event_overlays: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        event_overlays = event_overlays if isinstance(event_overlays, Mapping) else {}
        ranked: list[dict[str, Any]] = []
        for pp in pruned_paths if isinstance(pruned_paths, (list, tuple)) else ():
            candidate = pp.path_candidate if isinstance(pp, PrunedPath) else None
            overlay = event_overlays.get(_field(candidate, "path_id", ""))
            ranked.append(self.score_path_candidate(pp, event_overlay=overlay))
        ranked.sort(key=lambda x: _score(x.get("final_score")), reverse=True)
        return ranked

    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计。"""
        stats = {}
        if self._depth_cache:
            stats["depth_cache"] = self._depth_cache.stats()
        if self._score_cache:
            stats["score_cache"] = self._score_cache.stats()
        if self._adj_index:
            stats["adj_index"] = self._adj_index.stats()
        return stats

    def get_temporal_stats(self) -> dict[str, Any]:
        """获取时间核统计信息。"""
        return {
            "tracked_node_events": len(self._temporal_kernel._node_events),
            "kernel_half_lives": self._temporal_kernel._half_lives,
            "hawkes_alpha": self._temporal_kernel._alpha,
            "hawkes_beta": self._temporal_kernel._beta,
        }

    def get_calibrator(self) -> Any:
        """获取保序校准器实例（用于外部训练）。"""
        return self.calibrator

    def apply_temporal_decay(
        self,
        event_overlay: dict[str, Any],
        event_time: Any = None,
        source_type: str = "news",
        node_id: str | None = None,
    ) -> dict[str, Any]:
        """对事件overlay应用时间核衰减。

        Args:
            event_overlay: 原始事件overlay字典
            event_time: 事件时间（datetime或ISO字符串）
            source_type: 事件来源类型
            node_id: 关联节点ID（用于Hawkes自激跟踪）

        Returns:
            时间衰减后的event_overlay
        """
        import copy
        adjusted = copy.deepcopy(dict(event_overlay)) if isinstance(event_overlay, Mapping) else {}

        if event_time is not None:
            try:
                tw = _score(self._temporal_kernel.temporal_weight(event_time, source_type))
            except (AttributeError, TypeError, ValueError, OverflowError):
                tw = 0.0
            # 事件boost随时间衰减
            if "event_boost_score" in adjusted:
                adjusted["event_boost_score"] = _score(adjusted["event_boost_score"]) * tw
            # 记录事件用于Hawkes自激计算
            if _score(adjusted.get("event_boost_score", 0)) > 0.1 and isinstance(node_id, str) and node_id:
                # 解析时间
                from datetime import datetime
                if isinstance(event_time, str):
                    try:
                        ev_dt = datetime.fromisoformat(event_time.replace("Z", "+00:00").replace("+00:00", ""))
                    except (ValueError, TypeError):
                        ev_dt = datetime.now()
                elif isinstance(event_time, datetime):
                    ev_dt = event_time
                else:
                    ev_dt = datetime.now()
                if ev_dt.tzinfo is not None:
                    ev_dt = ev_dt.replace(tzinfo=None)
                self._temporal_kernel._node_events.setdefault(node_id, []).append(
                    (ev_dt, _score(adjusted["event_boost_score"]), True)
                )
            # freshness由时间核直接提供
            adjusted["freshness_score"] = tw

        return adjusted

    def record_outcome_for_calibration(
        self,
        predicted_score: float,
        actual_outcome: float,
        weight: float = 1.0,
    ) -> None:
        """记录预测结果用于保序校准和贝叶斯更新。

        Args:
            predicted_score: 预测分数（0-1）
            actual_outcome: 实际结果（0=失败，1=成功，或连续值）
            weight: 样本权重
        """
        if self.calibrator is not None:
            if not hasattr(self, "_calib_buffer"):
                self._calib_buffer = {"preds": [], "labels": []}
            self._calib_buffer["preds"].append(_score(predicted_score))
            self._calib_buffer["labels"].append(_score(actual_outcome))

    def trigger_calibration(self, min_samples: int = 50) -> bool:
        """触发保序校准训练。

        Args:
            min_samples: 最少需要的样本数

        Returns:
            是否训练成功
        """
        if self.calibrator is None:
            return False
        min_samples = _limit(min_samples, 50, 1000000)
        buf = getattr(self, "_calib_buffer", {"preds": [], "labels": []})
        if not isinstance(buf, Mapping):
            return False
        preds = buf.get("preds", [])
        labels = buf.get("labels", [])
        if not isinstance(preds, list) or not isinstance(labels, list):
            return False
        sample_count = min(len(preds), len(labels))
        if sample_count < min_samples:
            return False
        try:
            import numpy as np
            self.calibrator.fit(
                np.array(preds[:sample_count], dtype=np.float64),
                np.array(labels[:sample_count], dtype=np.float64),
            )
        except (TypeError, ValueError, RuntimeError):
            logger.debug("校准训练失败", exc_info=True)
            return False
        return True
