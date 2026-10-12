"""Graph-first Alpha Discovery Engine 的图谱 schema 与常量。

包含关系类型、结构轴、节点类型、来源权重、Claim 状态等常量定义。
本体层枚举已下沉到 shared.ontology.ontology_models，本模块保留
re-export 以维持既有导入路径的兼容性。

v2优化：权重通过 WeightRegistry 从YAML配置加载，支持热重载和贝叶斯校准。
保留硬编码常量作为fallback默认值。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import FrozenSet

from core.research.perilla.shared.ontology.ontology_models import (
    CLAIM_STATUS_SET,
    NODE_TYPES,
    RELATION_TYPES,
    SOURCE_TYPES,
    STRUCTURAL_AXES,
    ClaimStatus,
    NodeType,
    RelationType,
    SourceType,
    StructuralAxis,
)

# Re-export ontology enums so existing ``from .graph_schema import X`` keep working.
__all__ = [
    "RelationType",
    "RELATION_TYPES",
    "ClaimStatus",
    "CLAIM_STATUS_SET",
    "StructuralAxis",
    "STRUCTURAL_AXES",
    "NodeType",
    "NODE_TYPES",
    "SourceType",
    "SOURCE_TYPES",
    "SOURCE_WEIGHTS",
    "get_source_weight",
    "apply_source_floor",
    "ClaimScoreComponents",
]

# 关系方向语义：head → relation → tail
# 例如：光模块 --made_of--> 光芯片；光芯片 --supplied_by--> 公司A


# 来源默认可信度权重（0-1）—— 这些是fallback默认值，运行时优先从WeightRegistry加载
SOURCE_WEIGHTS: dict[str, float] = {
    # 高可信正式来源
    SourceType.ANNOUNCEMENT.value: 0.95,
    SourceType.ANNUAL_REPORT.value: 0.95,
    SourceType.PROSPECTUS.value: 0.95,
    SourceType.OFFICIAL_WEBSITE.value: 0.95,
    # 研报/白皮书
    SourceType.JULANG_REPORT.value: 0.85,
    SourceType.BROKER_REPORT.value: 0.85,
    SourceType.INDUSTRY_WHITEPAPER.value: 0.85,
    SourceType.WHITEPAPER.value: 0.82,
    # 专利/标准
    SourceType.PATENT.value: 0.75,
    SourceType.STANDARD.value: 0.75,
    # 互动/问答/财经平台
    SourceType.WENCAI.value: 0.65,
    SourceType.EASTMONEY.value: 0.65,
    SourceType.CNINFO_INTERACTION.value: 0.65,
    SourceType.INVESTOR_RELATION.value: 0.65,
    SourceType.INTERACTION_EASY.value: 0.65,
    # 新闻/展会/纪要
    SourceType.NEWS.value: 0.45,
    SourceType.EXHIBITION.value: 0.45,
    SourceType.MEETING_MINUTES.value: 0.45,
    SourceType.CONFERENCE.value: 0.45,
    # 低可信网络来源
    SourceType.WEB_SEARCH_SNIPPET.value: 0.30,
    SourceType.WEB_SEARCH.value: 0.30,
    SourceType.FORUM.value: 0.20,
    SourceType.SOCIAL_MEDIA.value: 0.20,
    # 数据/模型
    SourceType.MARKET_DATA.value: 0.90,
    SourceType.LLM_INFERENCE.value: 0.20,
    # 旧类型兼容
    SourceType.TDX_REPORT.value: 0.50,
}

# 结构轴关键词提示（用于 LLM/规则分类）
AXIS_KEYWORDS: dict[str, list[str]] = {
    StructuralAxis.MATERIAL.value: [
        "材料", "原材料", "化学品", "矿产", "衬底", "晶圆", "基板",
        "金属", "合金", "树脂", "陶瓷", "玻璃", "纤维", "薄膜",
    ],
    StructuralAxis.EQUIPMENT.value: [
        "设备", "装备", "机台", "仪器", "光刻机", "刻蚀机", "沉积设备",
        "测试设备", "检测设备", "封装设备",
    ],
    StructuralAxis.PROCESS.value: [
        "工艺", "制程", "加工", "封装", "测试", "切割", "抛光", "光刻",
        "蚀刻", "镀膜", "拉晶", "提纯", "烧结",
    ],
    StructuralAxis.PERFORMANCE.value: [
        "性能", "功耗", "带宽", "速率", "良率", "散热", "插损", "速率",
        "可靠性", "稳定性", "精度",
    ],
    StructuralAxis.SUPPLIER.value: [
        "供应商", "供应", "供货", "采购", "客户", "厂商", "集中度",
        "份额", "市占率", "龙头",
    ],
    StructuralAxis.CERTIFICATION.value: [
        "认证", "车规", "军规", "医疗", "AEC-Q", "IATF", "ISO",
        "UL", "CE", "FDA",
    ],
    StructuralAxis.DOMESTIC_SUBSTITUTION.value: [
        "国产替代", "国产化", "自主可控", "进口替代", "国产",
        "海外依赖", "进口", "卡脖子",
    ],
    StructuralAxis.COST.value: [
        "成本", "价格", "占比", "BOM", "降价", "毛利率", "原材料成本",
    ],
    StructuralAxis.VALUATION.value: [
        "估值", "市值", "PE", "PB", "涨幅", "热度", "关注度", "研报覆盖",
    ],
}

# 泛概念黑名单（FrontierScorer / SAG 用）
GENERIC_BLACKLIST: FrozenSet[str] = frozenset([
    "上游", "下游", "中游", "产业链", "行业", "市场", "企业", "公司",
    "产品", "技术", "领域", "板块", "概念", "龙头", "核心", "关键",
    "重要", "主要", "相关", "其他", "部分", "方面", "环节",
])

# 泛概念子串黑名单
GENERIC_SUBSTRINGS: tuple[str, ...] = (
    "产业链", "上游", "下游", "中游", "制造环节", "加工环节", "生产环节",
    "的唯一", "的实现", "实现突破", "的突破", "高度绑定", "深度绑定",
    "的方面", "的领域", "的环节", "相关", "的重要", "的核心", "的关键",
    "等环节", "等领域", "等方面", "等产品", "等技术", "等公司",
)

# 延迟导入 WeightRegistry 避免循环依赖
_registry_singleton = None


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _bounded(value: object, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite_float(value, default)))


def _get_registry():
    """延迟获取 WeightRegistry 单例。"""
    global _registry_singleton
    if _registry_singleton is None:
        try:
            from core.research.perilla.alpha.weight_registry import WeightRegistry
            _registry_singleton = WeightRegistry.get_instance()
        except Exception:
            _registry_singleton = None
    return _registry_singleton


def get_source_weight(source_type: str) -> float:
    """获取来源权重，优先从WeightRegistry加载，fallback到硬编码。

    Args:
        source_type: 来源类型字符串

    Returns:
        来源可信度权重（0-1）
    """
    source_type = source_type if isinstance(source_type, str) else "default"
    registry = _get_registry()
    if registry is not None:
        return _bounded(registry.get_source_weight(source_type), 0.50)
    return _bounded(SOURCE_WEIGHTS.get(source_type, 0.50), 0.50)


def apply_source_floor(claim_score: float, source_weight: float) -> float:
    """对正式来源设置可信度地板。

    Args:
        claim_score: 原始 claim 评分
        source_weight: 来源权重

    Returns:
        应用地板后的评分
    """
    claim_score = _bounded(claim_score)
    source_weight = _bounded(source_weight)
    # 尝试从WeightRegistry获取地板配置
    floor = 0.0
    try:
        registry = _get_registry()
        if registry is not None:
            floor_cfg = registry.get("claim_scoring", "source_floor", {})
            for level_name, level_cfg in floor_cfg.items() if isinstance(floor_cfg, Mapping) else ():
                if not isinstance(level_cfg, Mapping):
                    continue
                if source_weight >= _bounded(level_cfg.get("min_weight", 1.0), 1.0):
                    floor = _bounded(level_cfg.get("floor", 0.0))
                    break
            if floor == 0.0:
                # fallback到硬编码
                if source_weight >= 0.95:
                    floor = 0.60
                elif source_weight >= 0.85:
                    floor = 0.50
                elif source_weight >= 0.65:
                    floor = 0.38
        else:
            if source_weight >= 0.95:
                floor = 0.60
            elif source_weight >= 0.85:
                floor = 0.50
            elif source_weight >= 0.65:
                floor = 0.38
    except Exception:
        if source_weight >= 0.95:
            floor = 0.60
        elif source_weight >= 0.85:
            floor = 0.50
        elif source_weight >= 0.65:
            floor = 0.38

    return max(round(claim_score, 2), floor)


@dataclass
class ClaimScoreComponents:
    """Claim 评分的可解释组成部分。

    v2: compute() 优先使用 WeightRegistry 的权重，无配置时fallback到硬编码。
    """

    source_weight: float = 0.0
    extractor_confidence: float = 0.0
    evidence_clarity: float = 0.0
    relation_clarity: float = 0.0
    recency_score: float = 0.0
    text_quality_score: float = 0.0
    noise_penalty: float = 0.0
    contradiction_penalty: float = 0.0

    def compute(self, use_registry: bool = True) -> float:
        """计算综合评分。

        Args:
            use_registry: 是否使用WeightRegistry的权重（默认True）

        Returns:
            综合评分（0-1）
        """
        # 默认权重（硬编码fallback）
        default_weights = {
            "source_weight": 0.30,
            "extractor_confidence": 0.20,
            "evidence_clarity": 0.20,
            "relation_clarity": 0.15,
            "recency_score": 0.10,
            "text_quality_score": 0.05,
        }
        weights = dict(default_weights)

        if use_registry:
            registry = _get_registry()
            if registry is not None:
                reg_weights = registry.get_claim_weights()
                if isinstance(reg_weights, Mapping):
                    weights.update(reg_weights)

        score = (
            _finite_float(weights.get("source_weight", 0.30), 0.30) * _bounded(self.source_weight)
            + _finite_float(weights.get("extractor_confidence", 0.20), 0.20) * _bounded(self.extractor_confidence)
            + _finite_float(weights.get("evidence_clarity", 0.20), 0.20) * _bounded(self.evidence_clarity)
            + _finite_float(weights.get("relation_clarity", 0.15), 0.15) * _bounded(self.relation_clarity)
            + _finite_float(weights.get("recency_score", 0.10), 0.10) * _bounded(self.recency_score)
            + _finite_float(weights.get("text_quality_score", 0.05), 0.05) * _bounded(self.text_quality_score)
            - _bounded(self.noise_penalty)
            - _bounded(self.contradiction_penalty)
        )
        return _bounded(score)


def infer_axis(name: str, relation: str | None = None) -> set[str]:
    """根据节点名和关系快速推断结构轴（规则版）。"""
    axes: set[str] = set()
    text = str(name or "").lower()
    relation = relation if isinstance(relation, str) else None
    for axis, keywords in AXIS_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            axes.add(axis)
    if relation == RelationType.MADE_OF.value:
        axes.add(StructuralAxis.MATERIAL.value)
    if relation in (RelationType.SUPPLIED_BY.value, RelationType.IMPORT_DEPENDENT_ON.value):
        axes.add(StructuralAxis.SUPPLIER.value)
        axes.add(StructuralAxis.DOMESTIC_SUBSTITUTION.value)
    if relation in (RelationType.CERTIFIED_BY.value,):
        axes.add(StructuralAxis.CERTIFICATION.value)
    if relation in (RelationType.COST_DRIVER_OF.value,):
        axes.add(StructuralAxis.COST.value)
    if relation in (RelationType.PERFORMANCE_DRIVER_OF.value,):
        axes.add(StructuralAxis.PERFORMANCE.value)
    return axes or {StructuralAxis.MATERIAL.value}
