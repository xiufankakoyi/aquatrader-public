"""Research engine models — Entity, Relation, EntityRegistry, EvidenceGraph, MoatDetector."""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """Knowledge graph entity.

    Attributes:
        entity_id: 唯一实体标识
        entity_type: 实体类型 stock/theme/industry/product/event/news/report/announcement/financial_metric/moat/risk/provider
        entity_name: 实体名称
        aliases: 别名列表
        symbols: 关联股票代码列表
        created_at: 创建时间
        updated_at: 更新时间
    """

    entity_id: str = ""
    entity_type: str = ""
    entity_name: str = ""
    aliases: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        """自动填充 entity_id 和时间戳。"""
        if not self.entity_id:
            self.entity_id = f"ent_{uuid.uuid4().hex[:12]}"
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        self.aliases = _string_list(self.aliases)
        self.symbols = _string_list(self.symbols)


@dataclass
class Relation:
    """Knowledge graph relation between two entities.

    Attributes:
        relation_id: 唯一关系标识
        source_entity_id: 源实体 ID
        target_entity_id: 目标实体 ID
        relation_type: 关系类型 HAS_EVIDENCE/MENTIONED_IN/BELONGS_TO_THEME/etc
        evidence_ids: 支持此关系的证据 ID 列表
        weight: 关系权重
        confidence: 置信度 high/medium/low
        data_date: 数据日期
        created_at: 创建时间
    """

    relation_id: str = ""
    source_entity_id: str = ""
    target_entity_id: str = ""
    relation_type: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    weight: float = 0.0
    confidence: str = "medium"
    data_date: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        """自动填充 relation_id 和 created_at。"""
        if not self.relation_id:
            self.relation_id = f"rel_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.evidence_ids = _string_list(self.evidence_ids)
        self.weight = max(0.0, min(1.0, _safe_float(self.weight)))


# ---------------------------------------------------------------------------
# EntityRegistry — entity lookup and dedup
# ---------------------------------------------------------------------------


class EntityRegistry:
    """Registry for managing entities with lookup and dedup.

    Supports adding, querying, and deduplicating entities by name, symbol,
    or alias. Provides efficient lookups via multiple index structures.

    Attributes:
        entities: 所有已注册的实体字典
    """

    def __init__(self) -> None:
        """初始化实体注册表。"""
        self._entities: Dict[str, Entity] = {}
        # 名称 -> entity_id 映射（用于去重查找）
        self._name_index: Dict[str, str] = {}
        # symbol -> entity_id 映射
        self._symbol_index: Dict[str, str] = {}
        # alias -> entity_id 映射
        self._alias_index: Dict[str, str] = {}

    def add(self, entity: Entity) -> Entity:
        """添加实体到注册表。

        如果已存在同名/同 symbol/同 alias 的实体，则合并信息
        （将新的 aliases 和 symbols 补充到已有实体），返回已有实体。

        Args:
            entity: 要添加的实体

        Returns:
            实际存储的实体（可能是已有实体或新实体）
        """
        if not isinstance(entity, Entity):
            raise TypeError("entity must be an Entity")
        # 去重检查：按名称、symbol、alias 查找已有实体
        existing_id = self._find_existing(entity)
        if existing_id:
            existing = self._entities[existing_id]
            # 合并 aliases
            for alias in entity.aliases:
                if alias and alias not in existing.aliases:
                    existing.aliases.append(alias)
                    self._alias_index[alias] = existing.entity_id
            # 合并 symbols
            for symbol in entity.symbols:
                if symbol and symbol not in existing.symbols:
                    existing.symbols.append(symbol)
                    self._symbol_index[symbol] = existing.entity_id
            existing.updated_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            return existing

        # 新实体
        self._entities[entity.entity_id] = entity
        # 更新索引
        if entity.entity_name:
            self._name_index[entity.entity_name] = entity.entity_id
        for symbol in entity.symbols:
            if symbol:
                self._symbol_index[symbol] = entity.entity_id
        for alias in entity.aliases:
            if alias:
                self._alias_index[alias] = entity.entity_id
        return entity

    def _find_existing(self, entity: Entity) -> Optional[str]:
        """查找是否已存在同名/同 symbol/同 alias 的实体。

        Args:
            entity: 待检查的实体

        Returns:
            已有实体的 ID，不存在则返回 None
        """
        # 按名称查找
        if entity.entity_name and entity.entity_name in self._name_index:
            return self._name_index[entity.entity_name]
        # 按 symbol 查找
        for symbol in entity.symbols:
            if symbol and symbol in self._symbol_index:
                return self._symbol_index[symbol]
        # 按 alias 查找
        for alias in entity.aliases:
            if alias and alias in self._alias_index:
                return self._alias_index[alias]
        return None

    def get(self, entity_id: str) -> Optional[Entity]:
        """根据 ID 获取实体。

        Args:
            entity_id: 实体 ID

        Returns:
            Entity 实例或 None
        """
        return self._entities.get(entity_id)

    def get_by_name(self, name: str) -> Optional[Entity]:
        """根据名称获取实体。

        Args:
            name: 实体名称

        Returns:
            Entity 实例或 None
        """
        entity_id = self._name_index.get(name)
        if entity_id:
            return self._entities.get(entity_id)
        return None

    def get_by_symbol(self, symbol: str) -> Optional[Entity]:
        """根据股票代码获取实体。

        Args:
            symbol: 股票代码

        Returns:
            Entity 实例或 None
        """
        entity_id = self._symbol_index.get(symbol)
        if entity_id:
            return self._entities.get(entity_id)
        return None

    def find(self, query: str) -> List[Entity]:
        """模糊查找实体，按名称、symbol、alias 匹配。

        Args:
            query: 搜索关键词

        Returns:
            匹配的实体列表
        """
        if not isinstance(query, str) or not query:
            return []
        results: List[Entity] = []
        query_lower = query.lower()
        seen_ids: Set[str] = set()

        for entity in self._entities.values():
            if entity.entity_id in seen_ids:
                continue
            # 名称匹配
            if query_lower in entity.entity_name.lower():
                results.append(entity)
                seen_ids.add(entity.entity_id)
                continue
            # symbol 匹配
            if any(query_lower in s.lower() for s in entity.symbols):
                results.append(entity)
                seen_ids.add(entity.entity_id)
                continue
            # alias 匹配
            if any(query_lower in a.lower() for a in entity.aliases):
                results.append(entity)
                seen_ids.add(entity.entity_id)

        return results

    def list_by_type(self, entity_type: str) -> List[Entity]:
        """按类型列出实体。

        Args:
            entity_type: 实体类型

        Returns:
            该类型的所有实体列表
        """
        return [e for e in self._entities.values() if e.entity_type == entity_type]

    def count(self) -> int:
        """获取已注册实体总数。"""
        return len(self._entities)

    def all_entities(self) -> List[Entity]:
        """获取所有已注册实体。"""
        return list(self._entities.values())


# ---------------------------------------------------------------------------
# EvidenceGraph — entity-relation graph with traversal
# ---------------------------------------------------------------------------


class EvidenceGraph:
    """Knowledge graph connecting entities via evidence-backed relations.

    Supports adding entities and relations, then querying the graph by
    entity, relation type, or neighborhood traversal.

    Attributes:
        entities: 实体注册表
    """

    def __init__(self, entity_registry: Optional[EntityRegistry] = None) -> None:
        """初始化证据图谱。

        Args:
            entity_registry: 实体注册表，默认创建新的
        """
        self.entities = entity_registry or EntityRegistry()
        self._relations: Dict[str, Relation] = {}
        # 邻接表：source_entity_id -> [Relation]
        self._outgoing: Dict[str, List[Relation]] = defaultdict(list)
        # 反向邻接表：target_entity_id -> [Relation]
        self._incoming: Dict[str, List[Relation]] = defaultdict(list)

    def add_relation(self, relation: Relation) -> Relation:
        """添加关系到图谱。

        如果已存在相同 source/target/relation_type 的关系，则合并
        evidence_ids 和更新 weight。

        Args:
            relation: 要添加的关系

        Returns:
            实际存储的关系
        """
        if not isinstance(relation, Relation):
            raise TypeError("relation must be a Relation")
        # 查找是否已有相同 source/target/type 的关系
        for existing in self._outgoing.get(relation.source_entity_id, []):
            if (
                existing.target_entity_id == relation.target_entity_id
                and existing.relation_type == relation.relation_type
            ):
                # 合并 evidence_ids
                for eid in relation.evidence_ids:
                    if eid not in existing.evidence_ids:
                        existing.evidence_ids.append(eid)
                # 更新 weight（取较大值）
                existing.weight = max(existing.weight, relation.weight)
                # 更新 confidence（取较高等级）
                confidence_order = {"high": 3, "medium": 2, "low": 1}
                if confidence_order.get(relation.confidence, 0) > confidence_order.get(
                    existing.confidence, 0
                ):
                    existing.confidence = relation.confidence
                return existing

        # 新关系
        self._relations[relation.relation_id] = relation
        self._outgoing[relation.source_entity_id].append(relation)
        self._incoming[relation.target_entity_id].append(relation)
        return relation

    def get_relations(
        self,
        entity_id: str,
        direction: str = "both",
        relation_type: Optional[str] = None,
    ) -> List[Relation]:
        """获取与指定实体相关的关系。

        Args:
            entity_id: 实体 ID
            direction: 方向 outgoing/incoming/both
            relation_type: 可选，按关系类型过滤

        Returns:
            关系列表
        """
        results: List[Relation] = []
        seen_ids: Set[str] = set()

        if direction in ("outgoing", "both"):
            for rel in self._outgoing.get(entity_id, []):
                if rel.relation_id not in seen_ids:
                    if relation_type is None or rel.relation_type == relation_type:
                        results.append(rel)
                        seen_ids.add(rel.relation_id)

        if direction in ("incoming", "both"):
            for rel in self._incoming.get(entity_id, []):
                if rel.relation_id not in seen_ids:
                    if relation_type is None or rel.relation_type == relation_type:
                        results.append(rel)
                        seen_ids.add(rel.relation_id)

        return results

    def get_neighbors(
        self,
        entity_id: str,
        max_depth: int = 1,
        relation_type: Optional[str] = None,
    ) -> Dict[str, int]:
        """获取指定实体的邻居实体及其距离。

        使用 BFS 遍历图谱，返回所有可达实体及其最短距离。

        Args:
            entity_id: 起始实体 ID
            max_depth: 最大遍历深度
            relation_type: 可选，只沿指定类型的关系遍历

        Returns:
            实体 ID -> 距离 的映射
        """
        try:
            max_depth = max(0, int(max_depth))
        except (TypeError, ValueError):
            max_depth = 0
        distances: Dict[str, int] = {entity_id: 0}
        queue: List[Tuple[str, int]] = [(entity_id, 0)]

        while queue:
            current_id, current_dist = queue.pop(0)
            if current_dist >= max_depth:
                continue

            for rel in self.get_relations(current_id, direction="both", relation_type=relation_type):
                # 确定邻居
                neighbor_id = (
                    rel.target_entity_id
                    if rel.source_entity_id == current_id
                    else rel.source_entity_id
                )
                if neighbor_id not in distances:
                    distances[neighbor_id] = current_dist + 1
                    queue.append((neighbor_id, current_dist + 1))

        return distances

    def get_evidence_chain(self, entity_id: str) -> List[Dict[str, Any]]:
        """获取与指定实体相关的所有证据链。

        返回从该实体出发的所有关系及其关联的证据 ID。

        Args:
            entity_id: 实体 ID

        Returns:
            证据链列表，每项包含 relation_type、target、evidence_ids
        """
        chains: List[Dict[str, Any]] = []
        for rel in self.get_relations(entity_id, direction="both"):
            target_id = (
                rel.target_entity_id
                if rel.source_entity_id == entity_id
                else rel.source_entity_id
            )
            target_entity = self.entities.get(target_id)
            chains.append({
                "relation_type": rel.relation_type,
                "target_entity_id": target_id,
                "target_name": target_entity.entity_name if target_entity else target_id,
                "evidence_ids": rel.evidence_ids,
                "weight": rel.weight,
                "confidence": rel.confidence,
            })
        return chains

    def relation_count(self) -> int:
        """获取关系总数。"""
        return len(self._relations)

    def all_relations(self) -> List[Relation]:
        """获取所有关系。"""
        return list(self._relations.values())


# ---------------------------------------------------------------------------
# MoatDetector — detect competitive moat patterns from evidence
# ---------------------------------------------------------------------------


class MoatDetector:
    """Detect competitive moat (competitive advantage) patterns from evidence.

    Analyzes extraction results and evidence to identify moat indicators
    such as brand strength, switching costs, network effects, cost advantages,
    and intangible assets.

    Attributes:
        graph: 证据图谱
    """

    # 护城河关键词映射
    MOAT_KEYWORDS: Dict[str, List[str]] = {
        "brand": [
            "品牌", "知名度", "口碑", "溢价", "品牌力", "品牌价值",
            "brand", "brand value", "premium",
        ],
        "switching_cost": [
            "转换成本", "客户粘性", "用户粘性", "替换成本", "迁移成本",
            "高壁垒", "客户留存", "switching cost",
        ],
        "network_effect": [
            "网络效应", "规模效应", "平台效应", "双边市场", "生态",
            "network effect", "ecosystem",
        ],
        "cost_advantage": [
            "成本优势", "规模经济", "低成本", "降本增效", "成本领先",
            "cost advantage", "economies of scale",
        ],
        "intangible": [
            "专利", "牌照", "资质", "技术壁垒", "知识产权", "许可",
            "patent", "license", "barrier",
        ],
    }

    # 风险关键词
    RISK_KEYWORDS: List[str] = [
        "竞争加剧", "价格战", "市场份额下降", "政策风险", "监管",
        "技术替代", "需求下滑", "产能过剩", "债务风险",
        "competition", "price war", "regulatory risk",
    ]

    def __init__(self, graph: Optional[EvidenceGraph] = None) -> None:
        """初始化护城河检测器。

        Args:
            graph: 证据图谱，默认创建新的
        """
        self.graph = graph or EvidenceGraph()

    def detect_from_text(
        self,
        text: str,
        symbol: str = "",
        stock_name: str = "",
    ) -> Dict[str, Any]:
        """从文本中检测护城河信号。

        通过关键词匹配识别护城河类型和风险信号。

        Args:
            text: 待分析的文本
            symbol: 股票代码
            stock_name: 股票名称

        Returns:
            检测结果，包含 moat_signals、risk_signals、moat_score
        """
        text_lower = text.lower() if isinstance(text, str) else ""
        moat_signals: List[Dict[str, Any]] = []
        risk_signals: List[str] = []

        # 检测护城河信号
        for moat_type, keywords in self.MOAT_KEYWORDS.items():
            matched_keywords = [kw for kw in keywords if kw.lower() in text_lower]
            if matched_keywords:
                moat_signals.append({
                    "moat_type": moat_type,
                    "matched_keywords": matched_keywords,
                    "strength": self._estimate_strength(matched_keywords),
                })

        # 检测风险信号
        for risk_kw in self.RISK_KEYWORDS:
            if risk_kw.lower() in text_lower:
                risk_signals.append(risk_kw)

        # 计算护城河综合得分
        moat_score = self._calculate_moat_score(moat_signals, risk_signals)

        result: Dict[str, Any] = {
            "symbol": symbol,
            "stock_name": stock_name,
            "moat_signals": moat_signals,
            "risk_signals": risk_signals,
            "moat_score": moat_score,
            "moat_types": [s["moat_type"] for s in moat_signals],
            "has_moat": moat_score > 0.4,
        }

        # 如果检测到护城河，将结果写入图谱
        if symbol and moat_signals:
            self._update_graph(symbol, stock_name, result)

        return result

    def detect_from_extraction(
        self,
        moat_claims: List[str],
        risks: List[str],
        symbol: str = "",
        stock_name: str = "",
    ) -> Dict[str, Any]:
        """从 ExtractionResult 的 moat_claims 和 risks 字段检测护城河。

        Args:
            moat_claims: 护城河声明列表
            risks: 风险列表
            symbol: 股票代码
            stock_name: 股票名称

        Returns:
            检测结果
        """
        moat_claims = _string_list(moat_claims)
        risks = _string_list(risks)
        combined_text = " ".join(moat_claims + risks)
        result = self.detect_from_text(combined_text, symbol=symbol, stock_name=stock_name)

        # 将原始 claims 也附加到结果中
        result["raw_moat_claims"] = moat_claims
        result["raw_risks"] = risks

        return result

    def _estimate_strength(self, matched_keywords: List[str]) -> str:
        """根据匹配关键词数量估计信号强度。

        Args:
            matched_keywords: 匹配到的关键词列表

        Returns:
            强度等级 high/medium/low
        """
        count = len(matched_keywords)
        if count >= 3:
            return "high"
        elif count >= 2:
            return "medium"
        return "low"

    def _calculate_moat_score(
        self,
        moat_signals: List[Dict[str, Any]],
        risk_signals: List[str],
    ) -> float:
        """计算护城河综合得分。

        基于护城河信号的数量和强度加分，风险信号减分。
        得分范围 [0.0, 1.0]。

        Args:
            moat_signals: 护城河信号列表
            risk_signals: 风险信号列表

        Returns:
            护城河得分
        """
        score = 0.0
        # 护城河加分
        strength_weights = {"high": 0.25, "medium": 0.15, "low": 0.08}
        for signal in moat_signals:
            score += strength_weights.get(signal.get("strength", "low"), 0.08)

        # 风险减分
        risk_penalty = min(len(risk_signals) * 0.1, 0.5)
        score -= risk_penalty

        # 钳制到 [0.0, 1.0]
        return max(0.0, min(1.0, score))

    def _update_graph(
        self,
        symbol: str,
        stock_name: str,
        result: Dict[str, Any],
    ) -> None:
        """将检测结果更新到证据图谱。

        创建 stock 实体和 moat 实体，并建立关系。

        Args:
            symbol: 股票代码
            stock_name: 股票名称
            result: 检测结果
        """
        # 确保股票实体存在
        stock_entity = self.graph.entities.get_by_symbol(symbol)
        if not stock_entity:
            stock_entity = Entity(
                entity_type="stock",
                entity_name=stock_name or symbol,
                symbols=[symbol],
            )
            self.graph.entities.add(stock_entity)

        # 为每种护城河类型创建实体和关系
        for moat_signal in result.get("moat_signals", []):
            moat_type = moat_signal["moat_type"]
            moat_entity = Entity(
                entity_type="moat",
                entity_name=f"{stock_name or symbol}_{moat_type}",
                aliases=[moat_type],
            )
            self.graph.entities.add(moat_entity)

            relation = Relation(
                source_entity_id=stock_entity.entity_id,
                target_entity_id=moat_entity.entity_id,
                relation_type="HAS_MOAT",
                weight=moat_signal.get("strength", "low") == "high" and 1.0 or 0.5,
                confidence=moat_signal.get("strength", "medium"),
            )
            self.graph.add_relation(relation)

    def get_stock_moat_summary(self, symbol: str) -> Dict[str, Any]:
        """获取指定股票的护城河摘要。

        从图谱中查询该股票关联的所有护城河关系。

        Args:
            symbol: 股票代码

        Returns:
            护城河摘要
        """
        stock_entity = self.graph.entities.get_by_symbol(symbol)
        if not stock_entity:
            return {"symbol": symbol, "has_moat": False, "moat_types": []}

        moat_relations = self.graph.get_relations(
            stock_entity.entity_id,
            direction="outgoing",
            relation_type="HAS_MOAT",
        )

        moat_types: List[str] = []
        for rel in moat_relations:
            target = self.graph.entities.get(rel.target_entity_id)
            if target:
                moat_types.extend(target.aliases)

        return {
            "symbol": symbol,
            "stock_name": stock_entity.entity_name,
            "has_moat": len(moat_relations) > 0,
            "moat_types": moat_types,
            "relation_count": len(moat_relations),
        }
