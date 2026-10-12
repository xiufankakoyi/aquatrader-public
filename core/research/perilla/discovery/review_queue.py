"""Perilla Discovery Engine v1.25 - 审核队列管理.

管理需要人工审核的发现条目，构建审核队列并导出为 JSON/Markdown 格式。
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, List

from .models import (
    EVIDENCE_LEVEL_EARLY_SIGNAL,
    EVIDENCE_LEVEL_HYPOTHESIS,
    EVIDENCE_LEVEL_RUMOR,
    CompanyCandidate,
    PerillaDiscoveryItem,
    now_iso,
)

logger = logging.getLogger(__name__)

# 默认输出目录
_DEFAULT_OUTPUT_DIR = os.path.join("data", "reports", "perilla_discovery")


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _text(item))]


def _safe_probability(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return min(max(parsed, 0.0), 1.0) if math.isfinite(parsed) else 0.0


@dataclass
class ReviewItem:
    """审核条目 —— 需要人工审核的单条记录.

    Attributes:
        item_id: 关联的发现条目 ID。
        reason: 需要审核的原因。
        priority: 优先级 (high / medium / low)。
        required_action: 建议执行的操作。
        suggested_queries: 建议的搜索查询列表。
        related_sources: 关联来源列表。
        created_at: 创建时间。
    """

    item_id: str = ""
    reason: str = ""
    priority: str = "medium"
    required_action: str = ""
    suggested_queries: List[str] = field(default_factory=list)
    related_sources: List[str] = field(default_factory=list)
    created_at: str = ""


class ReviewQueue:
    """审核队列管理器 —— 构建和管理需人工审核的条目队列.

    根据分类结果和证据等级，自动识别需要人工审核的条目，
    分配优先级并生成建议操作和搜索查询。
    """

    def build_queue(
        self,
        discovery_items: List[PerillaDiscoveryItem],
        candidates: List[CompanyCandidate],
    ) -> List[ReviewItem]:
        """根据发现条目和候选信息构建审核队列.

        Args:
            discovery_items: 分类后的发现条目列表。
            candidates: 原始公司候选列表。

        Returns:
            需要人工审核的 ReviewItem 列表。
        """
        if not isinstance(discovery_items, (list, tuple)) or not discovery_items:
            logger.warning("审核队列构建收到空条目列表，返回空队列")
            return []

        # 构建候选查找表：candidate_id -> CompanyCandidate
        candidate_by_id = {
            _text(_field(candidate, "candidate_id")): candidate
            for candidate in (candidates if isinstance(candidates, (list, tuple)) else [])
            if _text(_field(candidate, "candidate_id"))
        }

        queue: List[ReviewItem] = []
        for item in discovery_items:
            if item is None:
                continue
            review_items = self._evaluate_item(item, candidate_by_id)
            queue.extend(review_items)

        # 按优先级排序：high > medium > low
        priority_order = {"high": 0, "medium": 1, "low": 2}
        queue.sort(key=lambda r: priority_order.get(r.priority, 1))

        logger.info("审核队列构建完成: %d 个条目需审核", len(queue))
        return queue

    def export_queue(
        self,
        queue: List[ReviewItem],
        output_dir: str = _DEFAULT_OUTPUT_DIR,
    ) -> str:
        """导出审核队列为 JSON 和 Markdown 文件.

        Args:
            queue: 审核条目列表。
            output_dir: 输出目录路径。

        Returns:
            导出的 JSON 文件绝对路径。
        """
        os.makedirs(output_dir, exist_ok=True)
        queue = [item for item in (queue if isinstance(queue, (list, tuple)) else [])
                 if isinstance(item, ReviewItem)]

        # 导出 JSON
        json_path = os.path.join(output_dir, "review_queue_latest.json")
        json_data = [asdict(r) for r in queue]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2, allow_nan=False)

        # 导出 Markdown
        md_path = os.path.join(output_dir, "review_queue_latest.md")
        md_content = self._render_markdown(queue)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        logger.info("审核队列已导出: JSON=%s, MD=%s", json_path, md_path)
        return os.path.abspath(json_path)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _evaluate_item(
        self,
        item: PerillaDiscoveryItem,
        candidate_by_id: dict,
    ) -> List[ReviewItem]:
        """评估单个发现条目，生成零到多个审核条目.

        Args:
            item: 发现条目。
            candidate_by_id: 按 candidate_id 索引的候选字典。

        Returns:
            该条目对应的 ReviewItem 列表（可能为空）。
        """
        reviews: List[ReviewItem] = []
        if item is None:
            return reviews
        classification = _text(_field(item, "classification"))
        evidence = _text(_field(item, "evidence_level"))
        risk_flags = set(_text_list(_field(item, "risk_flags")))

        # 规则 1: LLM 纯推理证据 -> 高优先级
        if self._is_llm_only_evidence(item):
            reviews.append(self._make_review(
                item=item,
                reason="仅有LLM抽取结果，无外部证据",
                priority="high",
                required_action="验证LLM抽取结果",
            ))

        # 规则 2: 谣言来源 -> 中优先级
        if evidence == EVIDENCE_LEVEL_RUMOR:
            reviews.append(self._make_review(
                item=item,
                reason="仅谣言/论坛来源",
                priority="medium",
                required_action="寻找更可靠来源确认",
            ))

        # 规则 3: 早期信号 -> 中优先级
        if evidence == EVIDENCE_LEVEL_EARLY_SIGNAL:
            reviews.append(self._make_review(
                item=item,
                reason="仅有早期信号，无正式供货证据",
                priority="medium",
                required_action="跟踪早期信号进展",
            ))

        # 规则 4: 纯假设 -> 中优先级
        if evidence == EVIDENCE_LEVEL_HYPOTHESIS:
            reviews.append(self._make_review(
                item=item,
                reason="仅AI推理假设，无外部证据",
                priority="medium",
                required_action="寻找外部证据验证假设",
            ))

        # 规则 5: 非核心市场 -> 低优先级
        if classification == "NON_CORE_MARKET":
            reviews.append(self._make_review(
                item=item,
                reason="非核心市场标的",
                priority="low",
                required_action="确认非核心市场标的信息",
            ))

        # 规则 6: 仅概念标签 -> 低优先级
        if classification == "CONCEPT_CLAIM_ONLY":
            reviews.append(self._make_review(
                item=item,
                reason="仅概念标签，无供货证据",
                priority="low",
                required_action="确认是否有供货证据",
            ))

        # 规则 7: 证据来源冲突 -> 高优先级
        if "证据来源冲突" in risk_flags:
            reviews.append(self._make_review(
                item=item,
                reason="证据来源冲突",
                priority="high",
                required_action="解决证据冲突",
            ))

        # 规则 8: 缺少证券代码 -> 中优先级
        if "缺少证券代码" in risk_flags:
            reviews.append(self._make_review(
                item=item,
                reason="缺少证券代码",
                priority="medium",
                required_action="确认股票代码",
            ))

        # 规则 9: 无官方供货证据 -> 中优先级
        if self._lacks_official_supply_evidence(item):
            reviews.append(self._make_review(
                item=item,
                reason="无官方供货证据",
                priority="medium",
                required_action="寻找官方供货证据",
            ))

        # 规则 10: 高紫苏概率但证据弱 -> 高优先级
        if _safe_probability(_field(item, "perilla_probability")) >= 0.5 and evidence not in (
            "CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER", "MAIN_ANCHOR",
        ):
            reviews.append(self._make_review(
                item=item,
                reason="高潜力但证据弱，需加强验证",
                priority="high",
                required_action="寻找更多证据支持高紫苏概率",
            ))

        # 去重：同一 item_id 可能触发多条规则，保留优先级最高的
        seen: dict = {}
        for r in reviews:
            key = r.item_id
            if key not in seen or self._priority_value(r.priority) < self._priority_value(seen[key].priority):
                seen[key] = r

        return list(seen.values())

    def _make_review(
        self,
        item: PerillaDiscoveryItem,
        reason: str,
        priority: str,
        required_action: str,
    ) -> ReviewItem:
        """创建审核条目.

        Args:
            item: 发现条目。
            reason: 审核原因。
            priority: 优先级。
            required_action: 建议操作。

        Returns:
            ReviewItem 实例。
        """
        return ReviewItem(
            item_id=_text(_field(item, "item_id")),
            reason=reason,
            priority=priority,
            required_action=required_action,
            suggested_queries=self._suggest_queries(item),
            related_sources=_text_list(_field(item, "source_urls"))[:5],
            created_at=now_iso(),
        )

    def _determine_priority(self, classification: str, evidence_level: str) -> str:
        """根据分类和证据等级确定优先级.

        Args:
            classification: 分类标签。
            evidence_level: 证据等级。

        Returns:
            优先级字符串 (high / medium / low)。
        """
        # 高优先级：紫苏候选但证据弱
        if classification == "PERILLA_CANDIDATE" and evidence_level not in (
            "CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER",
        ):
            return "high"

        # 高优先级：证据冲突
        if evidence_level in ("RUMOR_ONLY", "HYPOTHESIS"):
            if classification == "PERILLA_CANDIDATE":
                return "high"

        # 中优先级：早期信号、假设、概念标签
        if classification in ("EARLY_SIGNAL_WATCH", "HYPOTHESIS_ONLY", "CONCEPT_CLAIM_ONLY"):
            return "medium"

        # 低优先级：非核心市场、谣言观察
        if classification in ("NON_CORE_MARKET", "RUMOR_WATCH"):
            return "low"

        # 默认中优先级
        return "medium"

    def _suggest_queries(self, item: PerillaDiscoveryItem) -> List[str]:
        """根据条目上下文生成验证搜索查询.

        Args:
            item: 发现条目。

        Returns:
            搜索查询字符串列表。
        """
        queries: List[str] = []
        company = _text(_field(item, "company_name"))
        concept = _text(_field(item, "concept"))
        node = _text(_field(item, "chain_node"))

        if not company:
            return queries

        # 通用查询
        if concept:
            queries.append(f"{company} {concept} 供应商")
            queries.append(f"{company} {concept} 供货")

        # 官方渠道查询
        queries.append(f"{company} 公告 供货")
        queries.append(f"{company} 年报 客户")

        # 针对特定分类的查询
        classification = _text(_field(item, "classification"))
        if classification == "EARLY_SIGNAL_WATCH":
            queries.append(f"{company} 招聘 {node or concept}")
            queries.append(f"{company} 招标 {node or concept}")

        if classification == "RUMOR_WATCH":
            queries.append(f"{company} 互动平台 供货")

        if classification == "CONCEPT_CLAIM_ONLY":
            queries.append(f"{company} 供应商认证")
            queries.append(f"{company} 供货协议")

        if classification == "HYPOTHESIS_ONLY":
            queries.append(f"{company} {node or concept} 布局")
            queries.append(f"{company} {node or concept} 产能")

        # 限制查询数量，避免过多
        return queries[:6]

    def _is_llm_only_evidence(self, item: PerillaDiscoveryItem) -> bool:
        """判断条目是否仅有 LLM 推理证据.

        LLM-only 的特征：证据等级为 HYPOTHESIS 且无任何 source_urls。

        Args:
            item: 发现条目。

        Returns:
            是否仅有 LLM 证据。
        """
        return (
            _field(item, "evidence_level") == EVIDENCE_LEVEL_HYPOTHESIS
            and not _text_list(_field(item, "source_urls"))
        )

    def _lacks_official_supply_evidence(self, item: PerillaDiscoveryItem) -> bool:
        """判断条目是否缺少官方供货证据.

        缺少官方证据的特征：分类为 PERILLA_CANDIDATE 但证据等级
        不是 CONFIRMED_SUPPLIER 或 PROBABLE_SUPPLIER。

        Args:
            item: 发现条目。

        Returns:
            是否缺少官方供货证据。
        """
        if _field(item, "classification") != "PERILLA_CANDIDATE":
            return False
        return _field(item, "evidence_level") not in (
            "CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER",
        )

    @staticmethod
    def _priority_value(priority: str) -> int:
        """将优先级字符串转为数值用于排序.

        Args:
            priority: 优先级字符串。

        Returns:
            数值，越小优先级越高。
        """
        return {"high": 0, "medium": 1, "low": 2}.get(_text(priority).lower(), 1)

    def _render_markdown(self, queue: List[ReviewItem]) -> str:
        """将审核队列渲染为 Markdown 表格.

        Args:
            queue: 审核条目列表。

        Returns:
            Markdown 格式字符串。
        """
        lines: List[str] = [
            "# Perilla Discovery 审核队列",
            "",
            f"生成时间: {now_iso()}",
            f"待审核条目数: {len(queue)}",
            "",
            "| 优先级 | ID | 原因 | 建议操作 | 建议查询 |",
            "|--------|-----|------|----------|----------|",
        ]

        for r in queue:
            queries_str = "; ".join(_text_list(r.suggested_queries)[:3]) if r.suggested_queries else "-"
            # 转义 Markdown 表格中的管道符
            reason_safe = _text(r.reason).replace("|", "/")
            action_safe = _text(r.required_action).replace("|", "/")
            queries_safe = queries_str.replace("|", "/")
            lines.append(
                f"| {_text(r.priority)} | {_text(r.item_id)[:12]}... | {reason_safe} | {action_safe} | {queries_safe} |"
            )

        lines.append("")
        return "\n".join(lines)
