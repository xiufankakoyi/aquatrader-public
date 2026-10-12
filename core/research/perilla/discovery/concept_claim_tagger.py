"""Perilla Discovery Engine - 概念声明标签器.

识别仅出现在概念页面/指数页面而缺乏实质供应证据的公司候选，
将其标记为 CONCEPT_CLAIM_ONLY，避免将"蹭概念"误判为真实供应商。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, List

from .models import (
    EVIDENCE_LEVEL_CLAIM_ONLY,
    EVIDENCE_LEVEL_INSUFFICIENT,
    SOURCE_TYPES,
    CompanyCandidate,
    SourceDocument,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _field(value: Any, name: str, default: Any = "") -> Any:
    """统一读取模型对象和边界映射输入。"""
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _set_field(value: Any, name: str, field_value: Any) -> bool:
    """尽量写回候选；不可变边界对象直接跳过。"""
    try:
        if isinstance(value, Mapping):
            value[name] = field_value  # type: ignore[index]
        else:
            setattr(value, name, field_value)
        return True
    except Exception:
        return False

# 概念型来源类型（缺乏实质供应证据的来源）
_CONCEPT_SOURCE_TYPES = {"concept_page", "index_page"}

# 供应关系关键词
_SUPPLY_KEYWORDS = (
    "供货", "供应商", "核心供应商", "进入供应链", "客户认证",
    "送样", "量产", "独家供应", "主要供应商", "合格供应商",
    "战略供应商", "供货商", "供货关系",
)


class ConceptClaimTagger:
    """概念声明标签器.

    对公司候选进行概念声明检测，识别仅出现在概念/指数页面
    而缺乏实质供应证据的公司，标记为 CONCEPT_CLAIM_ONLY。
    """

    def tag_concept_claims(
        self,
        candidates: List[CompanyCandidate],
        source_docs: List[SourceDocument],
    ) -> List[CompanyCandidate]:
        """对候选公司进行概念声明标注.

        对 evidence_level 为 CONCEPT_CLAIM_ONLY 或 INSUFFICIENT_EVIDENCE
        的候选，检查其是否仅有概念型来源且缺乏供应证据。
        若是，则标记为 CONCEPT_CLAIM_ONLY 并设置需人工审核。

        Args:
            candidates: 公司候选列表。
            source_docs: 关联的来源文档列表。

        Returns:
            更新后的公司候选列表。
        """
        candidates = list(_items(candidates))
        source_docs = _items(source_docs)
        for candidate in candidates:
            if candidate is None:
                continue
            company_name = _text(_field(candidate, "company_name"))
            # 仅处理 CONCEPT_CLAIM_ONLY 和 INSUFFICIENT_EVIDENCE 等级
            if _field(candidate, "evidence_level", None) not in (
                EVIDENCE_LEVEL_CLAIM_ONLY,
                EVIDENCE_LEVEL_INSUFFICIENT,
            ):
                continue

            # 检查是否有实质供应证据
            has_supply = self._has_supply_evidence(
                company_name, source_docs,
            )

            if has_supply:
                # 有供应证据，不标记为概念声明
                logger.debug(
                    "%s has supply evidence, skipping concept claim tag",
                    company_name,
                )
                continue

            # 检查是否仅出现在概念型来源中
            candidate_docs = self._filter_docs_for_company(
                company_name, source_docs,
            )
            candidate_source_types = {
                _text(_field(doc, "source_type"))
                for doc in candidate_docs
                if _text(_field(doc, "source_type"))
            }

            if not candidate_source_types:
                # 无任何关联文档，保持原等级
                continue

            # 仅概念型来源 -> 标记为 CONCEPT_CLAIM_ONLY
            if candidate_source_types <= _CONCEPT_SOURCE_TYPES:
                logger.info(
                    "Tagging %s as CONCEPT_CLAIM_ONLY (only concept sources: %s)",
                    company_name, candidate_source_types,
                )
                _set_field(candidate, "evidence_level", EVIDENCE_LEVEL_CLAIM_ONLY)
                _set_field(candidate, "needs_human_review", True)

        return candidates

    def _has_supply_evidence(
        self, company_name: str, docs: List[SourceDocument],
    ) -> bool:
        """检查文档中是否存在公司与供应关系的实质证据.

        在文档文本中搜索公司名称与供应关键词的共现，
        判断是否存在实质供应证据。

        Args:
            company_name: 公司名称。
            docs: 来源文档列表。

        Returns:
            True 表示存在供应证据，False 表示不存在。
        """
        company_name = _text(company_name)
        if not company_name:
            return False

        for doc in _items(docs):
            text = _text(_field(doc, "content_text"))
            if not text:
                continue

            # 公司名称必须在文本中出现
            if company_name not in text:
                continue

            # 检查供应关键词
            for kw in _SUPPLY_KEYWORDS:
                if kw in text:
                    logger.debug(
                        "Supply evidence found for %s: keyword '%s' in doc %s",
                        company_name, kw, _text(_field(doc, "document_id")),
                    )
                    return True

        return False

    @staticmethod
    def _filter_docs_for_company(
        company_name: str, docs: List[SourceDocument],
    ) -> List[SourceDocument]:
        """筛选与公司名称相关的文档.

        Args:
            company_name: 公司名称。
            docs: 来源文档列表。

        Returns:
            包含该公司名称的文档列表。
        """
        company_name = _text(company_name)
        if not company_name:
            return []

        return [
            doc for doc in _items(docs)
            if _text(_field(doc, "content_text"))
            and company_name in _text(_field(doc, "content_text"))
        ]
