"""LLM 抽取输出 Schema — 严格定义 LLM 返回的 JSON 结构。

每个子对象必须包含 evidence_text（原文摘录）和 source_chunk_offset（句子位置），
确保所有 LLM 输出可验证、可溯源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EvidenceItem:
    """证据条目 — LLM 输出的最小证据单元。

    Attributes:
        category: 证据类别 theme/risk/catalyst/moat/financial_signal/industry_relation
        label: 证据标签（如具体主题名、风险关键词等）
        evidence_text: 原文中连续摘录的短句，禁止改写或归纳
        source_chunk_offset: 该证据在 chunk 内的句子位置（如 "句子3-4"）
        confidence: 置信度 high/medium/low
    """

    category: str = ""
    label: str = ""
    evidence_text: str = ""
    source_chunk_offset: str = ""
    confidence: str = "low"

    def validate(self) -> List[str]:
        """校验证据条目，返回错误列表。

        Returns:
            错误描述列表，空列表表示校验通过
        """
        errors: List[str] = []
        if not self.evidence_text or not self.evidence_text.strip():
            errors.append("evidence_text 不能为空")
        if self.category not in (
            "theme", "risk", "catalyst", "moat",
            "financial_signal", "industry_relation",
        ):
            errors.append(f"未知 category: {self.category}")
        if self.confidence not in ("high", "medium", "low"):
            errors.append(f"未知 confidence: {self.confidence}")
        # 中文字符数 < 4 视为过短
        cn_chars = sum(1 for c in self.evidence_text if '\u4e00' <= c <= '\u9fff')
        if cn_chars < 4 and len(self.evidence_text.strip()) < 6:
            errors.append("evidence_text 过短（中文<4字符或总长<6字符）")
        return errors


@dataclass
class ExtractedRelation:
    """提取的实体关系。

    Attributes:
        from_entity: 起始实体
        relation_type: 关系类型
        to_entity: 目标实体
        evidence_text: 支持该关系的原文证据
        source_chunk_offset: 证据在 chunk 内的位置
        confidence: 置信度
    """

    from_entity: str = ""
    relation_type: str = ""
    to_entity: str = ""
    evidence_text: str = ""
    source_chunk_offset: str = ""
    confidence: str = "low"


@dataclass
class LLMExtractionOutput:
    """LLM 抽取输出的严格 Schema。

    所有列表字段若原文无对应信息必须为空数组 []。
    summary 中可写中性说明如"未发现显著风险信号"。

    Attributes:
        summary: 提取摘要
        key_points: 关键要点列表
        themes: 题材列表
        catalysts: 催化剂列表
        risks: 风险列表
        moat_claims: 护城河声明列表
        financial_signals: 财务信号列表
        industry_chain_relations: 产业链关系列表
        sentiment: 情绪倾向 positive/neutral/negative
        confidence: 置信度 high/medium/low
        evidence_items: 证据条目列表
        extracted_relations: 提取的关系列表
    """

    summary: str = ""
    key_points: List[str] = field(default_factory=list)
    themes: List[str] = field(default_factory=list)
    catalysts: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    moat_claims: List[str] = field(default_factory=list)
    financial_signals: List[str] = field(default_factory=list)
    industry_chain_relations: List[str] = field(default_factory=list)
    sentiment: str = "neutral"
    confidence: str = "medium"
    evidence_items: List[Dict[str, Any]] = field(default_factory=list)
    extracted_relations: List[Dict[str, Any]] = field(default_factory=list)

    def validate_evidence_items(self) -> List[str]:
        """校验所有 evidence_items，返回错误列表。

        Returns:
            错误描述列表
        """
        all_errors: List[str] = []
        for i, item in enumerate(self.evidence_items):
            if not isinstance(item, dict):
                all_errors.append(f"evidence_items[{i}] 不是字典")
                continue
            ei = EvidenceItem(
                category=item.get("category", ""),
                label=item.get("label", ""),
                evidence_text=item.get("evidence_text", ""),
                source_chunk_offset=item.get("source_chunk_offset", ""),
                confidence=item.get("confidence", "low"),
            )
            errors = ei.validate()
            for err in errors:
                all_errors.append(f"evidence_items[{i}]: {err}")
        return all_errors

    @classmethod
    def from_parsed_json(cls, data: Dict[str, Any]) -> "LLMExtractionOutput":
        """从 LLM 返回的解析后 JSON 构造实例，缺失字段补空。

        Args:
            data: 解析后的 JSON 字典

        Returns:
            LLMExtractionOutput 实例
        """
        return cls(
            summary=str(data.get("summary", ""))[:300],
            key_points=data.get("key_points", []) or [],
            themes=data.get("themes", []) or [],
            catalysts=data.get("catalysts", []) or [],
            risks=data.get("risks", []) or [],
            moat_claims=data.get("moat_claims", []) or [],
            financial_signals=data.get("financial_signals", []) or [],
            industry_chain_relations=data.get("industry_chain_relations", []) or [],
            sentiment=data.get("sentiment", "neutral"),
            confidence=data.get("confidence", "medium"),
            evidence_items=data.get("evidence_items", []) or [],
            extracted_relations=data.get("extracted_relations", []) or [],
        )
