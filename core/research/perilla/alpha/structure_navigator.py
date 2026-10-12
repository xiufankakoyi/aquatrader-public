"""StructureNavigator：基于 SAG 图谱的产业链结构导航。

职责：
1. 从 SAG 查询 root 的已有 subgraph。
2. 如果 subgraph 稀疏，触发轻量 BFS/搜索补全骨架。
3. 对候选节点按结构轴分类。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

import hashlib

from core.research.perilla.alpha.graph_schema import (
    GENERIC_BLACKLIST,
    NODE_TYPES,
    NodeType,
    RelationType,
    SourceType,
    StructuralAxis,
    infer_axis,
)
from core.research.perilla.alpha.path_discovery_schema import PathCandidate
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any) -> float:
    return max(0.0, min(1.0, _finite(value)))


def _limit(value: Any, default: int, maximum: int = 1000) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


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

# 骨架稀疏度阈值
SPARSE_NODE_THRESHOLD = 3   # root 周围直接子节点少于该值视为稀疏
SPARSE_EDGE_THRESHOLD = 2   # root 出边平均证据来源数低于该值视为稀疏


class StructureNavigator:
    """产业链结构导航器。"""

    def __init__(self, kb: SAGKnowledgeBase) -> None:
        """初始化。

        Args:
            kb: SAG 知识库
        """
        self.kb = kb

    def is_sparse(self, root: str) -> bool:
        """判断 root 的 subgraph 是否稀疏。

        稀疏定义：直接子节点数量 < SPARSE_NODE_THRESHOLD，
        或出边平均 source_weights 种类 < SPARSE_EDGE_THRESHOLD。
        """
        try:
            root_edges = self.kb.get_neighbors(root, direction="out") or []
        except (AttributeError, KeyError, TypeError, ValueError):
            return True
        if not isinstance(root_edges, (list, tuple)):
            return True
        if len(root_edges) < SPARSE_NODE_THRESHOLD:
            return True

        if not root_edges:
            return True

        source_counts = [
            len(source_weights)
            for edge in root_edges
            for source_weights in [_field(edge, "source_weights", {})]
            if isinstance(source_weights, Mapping)
        ]
        avg_sources = sum(source_counts) / len(root_edges) if source_counts else 0.0
        return avg_sources < SPARSE_EDGE_THRESHOLD

    def classify_axes(self, node_name: str, relation: str | None = None) -> list[str]:
        """对节点进行结构轴分类。

        Args:
            node_name: 节点名
            relation: 可选关系提示

        Returns:
            结构轴列表
        """
        node_name = str(node_name or "")
        if node_name in GENERIC_BLACKLIST:
            return []

        # 优先使用 SAG 中已有节点的 axes
        norm = node_name.replace(" ", "").replace("　", "")
        nodes = getattr(self.kb, "nodes", {})
        for key, node in nodes.items() if isinstance(nodes, Mapping) else ():
            if key == norm:
                axes = _field(node, "axes", [])
                return [axis for axis in axes or [] if isinstance(axis, str)] if isinstance(axes, (list, tuple)) else []

        # 规则推断
        axes = list(infer_axis(node_name, relation))
        return axes

    def scout_skeleton(
        self,
        root: str,
        max_breadth: int = 5,
        source_type: str = SourceType.WEB_SEARCH.value,
        source_title: str = "结构导航骨架搜索",
    ) -> list[dict[str, Any]]:
        """轻量 BFS 搜索补全骨架。

        只搜索 root 的直接上下游，不深度下钻。

        Args:
            root: 根概念
            max_breadth: 每方向最多保留子项数
            source_type: 来源类型
            source_title: 来源标题

        Returns:
            claim dict 列表
        """
        from core.research.perilla.discovery.drill_down_engine import DrillDownEngine

        root = str(root or "").strip()
        max_breadth = _limit(max_breadth, 5, 100)
        if not root:
            return []
        engine = DrillDownEngine(max_depth=1, max_breadth=max_breadth)

        # 用 DrillDownEngine 做一次浅层下钻
        try:
            tree = engine.drill(root, max_depth=1)
        except (AttributeError, TypeError, ValueError):
            return []

        claims: list[dict[str, Any]] = []
        timestamp = __import__("datetime").datetime.now().isoformat()

        # 从 tree 的 children 还原 claim
        def _walk(node: dict[str, Any], parent_name: str | None = None) -> None:
            if not isinstance(node, Mapping):
                return
            current_name = str(node.get("name", "") or "")
            relation = node.get("relation", "")
            evidence = node.get("evidence", "")
            if (
                parent_name
                and current_name
                and isinstance(relation, str)
                and relation
                and isinstance(evidence, str)
                and evidence
            ):
                # 将 drill_down_engine 的关系映射到标准 relation
                std_relation = relation
                if relation == "sub_component":
                    std_relation = RelationType.CONTAINS.value
                elif relation == "used_material":
                    std_relation = RelationType.MADE_OF.value

                claims.append({
                    "head": parent_name,
                    "relation": std_relation,
                    "tail": current_name,
                    "evidence": evidence,
                    "source_type": source_type,
                    "source_title": source_title,
                    "timestamp": timestamp,
                    "confidence": 0.6,
                    "head_type": NodeType.COMPONENT.value,
                    "tail_type": NodeType.MATERIAL.value if std_relation == RelationType.MADE_OF.value else NodeType.COMPONENT.value,
                    "structure_axis": StructuralAxis.MATERIAL.value if std_relation == RelationType.MADE_OF.value else StructuralAxis.EQUIPMENT.value,
                })

            children = node.get("children", [])
            for child in children if isinstance(children, (list, tuple)) else ():
                _walk(child, current_name)

        _walk(tree)
        logger.info("骨架搜索 '%s' 产生 %d 条候选 claim", root, len(claims))
        return claims

    def build_or_enrich(
        self,
        root: str,
        max_breadth: int = 5,
    ) -> dict[str, Any]:
        """构建或丰富 root 的 subgraph。

        Args:
            root: 根概念
            max_breadth: 骨架搜索广度

        Returns:
            {"root": root, "was_sparse": bool, "added_claims": int, "subgraph": dict}
        """
        root = str(root or "").strip()
        max_breadth = _limit(max_breadth, 5, 100)
        if not root:
            return {"root": "", "was_sparse": True, "added_claims": 0, "subgraph": {}}
        was_sparse = self.is_sparse(root)
        added = 0

        if was_sparse:
            logger.info("'%s' subgraph 稀疏，触发骨架搜索", root)
            candidate_claims = self.scout_skeleton(root, max_breadth=max_breadth)
            for c in candidate_claims if isinstance(candidate_claims, (list, tuple)) else ():
                try:
                    added_claim = self.kb.add_claim(
                        head=c["head"],
                        relation=c["relation"],
                        tail=c["tail"],
                        evidence=c["evidence"],
                        source_type=c["source_type"],
                        source_title=c["source_title"],
                        timestamp=c["timestamp"],
                        confidence=c["confidence"],
                        head_type=c.get("head_type"),
                        tail_type=c.get("tail_type"),
                        metadata={"structure_axis": c.get("structure_axis")},
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    logger.debug("骨架 claim 写入失败", exc_info=True)
                    added_claim = False
                if added_claim:
                    added += 1

            try:
                saved = self.kb.save()
            except (AttributeError, OSError, TypeError, ValueError):
                saved = False
            if not saved and hasattr(self.kb, "storage_path"):
                logger.error("StructureNavigator 持久化失败: %s", self.kb.storage_path)

        try:
            subgraph = self.kb.get_subgraph(root, max_depth=3)
        except (AttributeError, KeyError, TypeError, ValueError):
            subgraph = {}
        return {
            "root": root,
            "was_sparse": was_sparse,
            "added_claims": added,
            "subgraph": subgraph,
        }

    # ------------------------------------------------------------------
    # Path-based discovery: 输出结构化 PathCandidate，不再使用裸 list
    # ------------------------------------------------------------------

    META_PATHS: list[list[str]] = [
        [RelationType.CONTAINS.value, RelationType.SUPPLIED_BY.value],
        [RelationType.MADE_OF.value, RelationType.SUPPLIED_BY.value],
        [RelationType.MADE_OF.value, RelationType.PROCESSED_BY.value, RelationType.REQUIRES.value],
        [RelationType.MADE_OF.value, RelationType.CERTIFIED_BY.value, RelationType.SUPPLIED_BY.value],
        [RelationType.MADE_OF.value, RelationType.IMPORT_DEPENDENT_ON.value],
        [RelationType.REQUIRES.value, RelationType.SUBSTITUTE_FOR.value],
    ]

    PATH_TYPE_BY_RELATIONS: dict[tuple[str, ...], str] = {
        (RelationType.SUPPLIED_BY.value,): "supplier_dependency_path",
        (RelationType.IMPORT_DEPENDENT_ON.value,): "import_dependency_path",
        (RelationType.MADE_OF.value, RelationType.SUPPLIED_BY.value): "material_bottleneck_path",
        (RelationType.CONTAINS.value, RelationType.SUPPLIED_BY.value): "supplier_dependency_path",
        (RelationType.MADE_OF.value, RelationType.IMPORT_DEPENDENT_ON.value): "import_dependency_path",
        (RelationType.MADE_OF.value, RelationType.PROCESSED_BY.value, RelationType.REQUIRES.value): "process_barrier_path",
        (RelationType.MADE_OF.value, RelationType.CERTIFIED_BY.value, RelationType.SUPPLIED_BY.value): "certification_barrier_path",
        (RelationType.REQUIRES.value, RelationType.SUBSTITUTE_FOR.value): "domestic_substitution_path",
    }

    @staticmethod
    def _candidate_path_id(path: list[str]) -> str:
        """根据节点序列生成稳定 path id。"""
        stable = "|".join(str(item) for item in path if item is not None) if isinstance(path, (list, tuple)) else ""
        return "pc-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _relations_match(relations: list[str], meta_path: list[str]) -> bool:
        """判断 relations 是否匹配 meta_path（允许前缀匹配）。"""
        if len(relations) > len(meta_path):
            return False
        return all(relations[i] == meta_path[i] for i in range(len(relations)))

    def _detect_meta_path(self, relations: list[str]) -> str:
        """返回 relations 匹配到的 meta-path 名称。"""
        for idx, mp in enumerate(self.META_PATHS):
            if self._relations_match(relations, mp):
                return f"meta_path_{idx + 1}"
        return ""

    def _classify_path_type(self, relations: list[str], path: list[str]) -> str:
        """根据关系序列和节点类型判定 path_type。"""
        key = tuple(relations)
        if key in self.PATH_TYPE_BY_RELATIONS:
            return self.PATH_TYPE_BY_RELATIONS[key]

        # 兜底：根据关系集合判断
        relation_set = set(relations)
        if RelationType.IMPORT_DEPENDENT_ON.value in relation_set:
            return "import_dependency_path"
        if RelationType.SUPPLIED_BY.value in relation_set:
            return "supplier_dependency_path"
        if RelationType.CERTIFIED_BY.value in relation_set:
            return "certification_barrier_path"
        if RelationType.BOTTLENECK_OF.value in relation_set or RelationType.CONSTRAINED_BY.value in relation_set:
            return "material_bottleneck_path"
        if RelationType.SUBSTITUTE_FOR.value in relation_set:
            return "domestic_substitution_path"

        # 根据节点类型判断
        nodes = getattr(self.kb, "nodes", {})
        for node_name in path[1:] if isinstance(path, (list, tuple)) else ():
            node = _mapping_get(nodes, node_name)
            if not node:
                continue
            node_type = _field(node, "node_type", "")
            if node_type == NodeType.EQUIPMENT.value:
                return "equipment_bottleneck_path"
            if node_type == NodeType.PROCESS.value:
                return "process_barrier_path"
            if node_type == NodeType.CERTIFICATION.value:
                return "certification_barrier_path"
            if node_type in (NodeType.MATERIAL.value, NodeType.CONSUMABLE.value):
                return "material_bottleneck_path"
        return "structural_path"

    def _is_meta_path_constrained(self, relations: list[str]) -> bool:
        """检查路径是否受 meta-path 约束。"""
        return any(self._relations_match(relations, mp) for mp in self.META_PATHS)

    def _path_base_score(self, path: list[str], relations: list[str]) -> float:
        """计算路径基础结构分。"""
        score = 0.5
        if self._is_meta_path_constrained(relations):
            score += 0.2
        # 节点类型丰富度
        node_type_count = 0
        nodes = getattr(self.kb, "nodes", {})
        for node_name in path if isinstance(path, (list, tuple)) else ():
            node = _mapping_get(nodes, node_name)
            if node and _field(node, "node_type", "") not in (NodeType.GENERIC.value, NodeType.ROOT.value):
                node_type_count += 1
        score += min(0.2, node_type_count * 0.05)
        return min(1.0, score)

    def generate_path_candidates(
        self,
        root: str,
        max_depth: int = 3,
        max_paths: int = 500,
        require_meta_path: bool = False,
    ) -> list[PathCandidate]:
        """生成结构路径候选（PathCandidate）。

        默认只生成 1-3 hop 路径；4 hop 必须命中 meta-path 或具有高置信 claim。
        不允许无约束 BFS。

        Args:
            root: 根节点名。
            max_depth: 最大深度（默认 3）。
            max_paths: 最大路径数。
            require_meta_path: 是否只返回命中 meta-path 的路径。

        Returns:
            PathCandidate 列表（按 base_score 降序）。
        """
        root = str(root or "").strip()
        max_depth = min(4, _limit(max_depth, 3, 4))
        max_paths = _limit(max_paths, 500)
        if not root or max_depth <= 0 or max_paths <= 0:
            return []

        candidates: list[PathCandidate] = []
        seen_path_ids: set[str] = set()
        stack: list[tuple[str, list[str], list[tuple[str, str, str]], list[str], set[str]]] = [
            (root, [root], [], [], {root})
        ]

        while stack and len(candidates) < max_paths:
            current, path, edges, relations, visited = stack.pop()

            if len(path) >= 2:
                path_id = self._candidate_path_id(path)
                if path_id not in seen_path_ids:
                    seen_path_ids.add(path_id)
                    meta_path = self._detect_meta_path(relations)
                    is_constrained = bool(meta_path) or self._is_meta_path_constrained(relations)

                    # 4 hop 必须命中 meta-path 或高置信
                    if len(path) == 4 and not is_constrained:
                        continue
                    if require_meta_path and not meta_path:
                        continue

                    axes: set[str] = set()
                    node_types: list[str] = []
                    for node_name in path:
                        nodes = getattr(self.kb, "nodes", {})
                        node = _mapping_get(nodes, node_name)
                        if node:
                            node_axes = _field(node, "axes", [])
                            if isinstance(node_axes, (list, tuple)):
                                axes.update(axis for axis in node_axes if isinstance(axis, str))
                            node_types.append(str(_field(node, "node_type", NodeType.GENERIC.value)))
                        else:
                            node_types.append(NodeType.GENERIC.value)

                    path_type = self._classify_path_type(relations, path)
                    candidates.append(PathCandidate(
                        path_id=path_id,
                        nodes=path[:],
                        edges=edges[:],
                        relations=relations[:],
                        axes=sorted(axes),
                        root_entity=root,
                        target_entity=path[-1],
                        hop_count=len(relations),
                        path_type=path_type,
                        generated_by="structure_navigator",
                        base_score=self._path_base_score(path, relations),
                        node_types=node_types,
                        meta_path=meta_path,
                    ))

            if len(path) >= max_depth:
                continue

            try:
                out_edges = self.kb.get_neighbors(current, direction="out") or []
            except (AttributeError, KeyError, TypeError, ValueError):
                out_edges = []
            if not isinstance(out_edges, (list, tuple)):
                out_edges = []
            # 按置信度和来源权重排序，优先探索高置信边
            out_edges = sorted(
                out_edges,
                key=lambda e: (_score(_field(e, "confidence", 0.0)), sum(
                    _score(value) for value in (_field(e, "source_weights", {}) or {}).values()
                ) if isinstance(_field(e, "source_weights", {}), Mapping) else 0.0),
                reverse=True,
            )

            for edge in out_edges:
                tail = _field(edge, "tail")
                relation = _field(edge, "relation")
                head = _field(edge, "head")
                if not all(isinstance(value, str) and value for value in (head, relation, tail)):
                    continue
                if tail in visited:
                    continue
                if str(tail).strip().lower() in GENERIC_BLACKLIST:
                    continue
                new_path = path + [tail]
                new_edges = edges + [(head, relation, tail)]
                new_relations = relations + [relation]
                stack.append((tail, new_path, new_edges, new_relations, visited | {tail}))

        candidates.sort(key=lambda c: c.base_score, reverse=True)
        logger.info("StructureNavigator 为 '%s' 生成 %d 条 PathCandidate", root, len(candidates))
        return candidates
