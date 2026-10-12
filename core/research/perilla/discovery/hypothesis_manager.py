"""Perilla Discovery Engine - 假设管理器.

管理发现流程中产生的各类假设，包括注册、验证、驳回及导出。
支持从供应链节点、瓶颈假设和公司候选中自动创建假设。
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import (
    EVIDENCE_LEVEL_HYPOTHESIS,
    CompanyCandidate,
    DynamicSupplyChain,
    BottleneckHypothesis,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)

# 假设类型常量
HYPOTHESIS_TYPE_CHAIN_NODE = "chain_node_hypothesis"
HYPOTHESIS_TYPE_BOTTLENECK = "bottleneck_hypothesis"
HYPOTHESIS_TYPE_COMPANY_MAPPING = "company_mapping_hypothesis"
HYPOTHESIS_TYPE_ROUTE_CHANGE = "route_change_hypothesis"

VALID_HYPOTHESIS_TYPES = {
    HYPOTHESIS_TYPE_CHAIN_NODE,
    HYPOTHESIS_TYPE_BOTTLENECK,
    HYPOTHESIS_TYPE_COMPANY_MAPPING,
    HYPOTHESIS_TYPE_ROUTE_CHANGE,
}

# 假设状态常量
STATUS_PENDING = "pending_verification"
STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"

VALID_STATUSES = {STATUS_PENDING, STATUS_VERIFIED, STATUS_REJECTED}

# 优先级常量
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"

VALID_PRIORITIES = {PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW}

# 默认导出目录
_DEFAULT_OUTPUT_DIR = "data/reports/perilla_discovery"


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, complex):
        return str(value)
    return ""


def _safe_text_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _safe_text(item))]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass
class Hypothesis:
    """假设数据结构.

    表示发现流程中产生的一条假设，记录假设类型、依据、
    缺失证据、验证查询及当前状态。

    Attributes:
        hypothesis_id: 假设唯一标识。
        hypothesis_type: 假设类型。
        reason: 假设推理理由。
        based_on_evidence_ids: 依据的证据 ID 列表。
        missing_evidence: 缺失的证据描述列表。
        verification_queries: 验证查询建议列表。
        verification_priority: 验证优先级 (high / medium / low)。
        status: 假设状态 (pending_verification / verified / rejected)。
        created_at: 创建时间。
    """

    hypothesis_id: str = ""
    hypothesis_type: str = HYPOTHESIS_TYPE_CHAIN_NODE
    reason: str = ""
    based_on_evidence_ids: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    verification_queries: List[str] = field(default_factory=list)
    verification_priority: str = PRIORITY_MEDIUM
    status: str = STATUS_PENDING
    created_at: str = ""


class HypothesisManager:
    """假设管理器.

    管理发现流程中产生的各类假设，提供注册、查询、
    验证、驳回及导出功能，并支持从供应链和瓶颈数据
    自动创建假设。
    """

    def __init__(self) -> None:
        """初始化假设管理器."""
        self._hypotheses: Dict[str, Hypothesis] = {}

    # ------------------------------------------------------------------
    # 核心操作
    # ------------------------------------------------------------------

    def register_hypothesis(
        self,
        hypothesis_type: str,
        reason: str,
        based_on_evidence_ids: List[str],
        missing_evidence: List[str],
        verification_queries: List[str],
        priority: str = PRIORITY_MEDIUM,
    ) -> Hypothesis:
        """注册一条新假设.

        Args:
            hypothesis_type: 假设类型，须为 VALID_HYPOTHESIS_TYPES 之一。
            reason: 假设推理理由。
            based_on_evidence_ids: 依据的证据 ID 列表。
            missing_evidence: 缺失的证据描述列表。
            verification_queries: 验证查询建议列表。
            priority: 验证优先级，默认 medium。

        Returns:
            新创建的 Hypothesis 对象。

        Raises:
            ValueError: 假设类型或优先级不合法时抛出。
        """
        if hypothesis_type not in VALID_HYPOTHESIS_TYPES:
            raise ValueError(
                f"Invalid hypothesis_type '{hypothesis_type}', "
                f"expected one of {VALID_HYPOTHESIS_TYPES}"
            )
        if priority not in VALID_PRIORITIES:
            raise ValueError(
                f"Invalid priority '{priority}', "
                f"expected one of {VALID_PRIORITIES}"
            )

        hypothesis = Hypothesis(
            hypothesis_id=new_id("hyp_"),
            hypothesis_type=hypothesis_type,
            reason=_safe_text(reason),
            based_on_evidence_ids=_safe_text_list(based_on_evidence_ids),
            missing_evidence=_safe_text_list(missing_evidence),
            verification_queries=_safe_text_list(verification_queries),
            verification_priority=priority,
            status=STATUS_PENDING,
            created_at=now_iso(),
        )

        self._hypotheses[hypothesis.hypothesis_id] = hypothesis
        logger.info(
            "Registered hypothesis %s: type=%s, priority=%s",
            hypothesis.hypothesis_id, hypothesis_type, priority,
        )
        return hypothesis

    def get_hypotheses(
        self, status: Optional[str] = None,
    ) -> List[Hypothesis]:
        """获取假设列表.

        Args:
            status: 可选的状态过滤条件，为 None 时返回全部。

        Returns:
            假设列表，按创建时间排序。

        Raises:
            ValueError: 状态参数不合法时抛出。
        """
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status '{status}', "
                f"expected one of {VALID_STATUSES}"
            )

        hypotheses = list(self._hypotheses.values())
        if status is not None:
            hypotheses = [h for h in hypotheses if h.status == status]

        # 按创建时间排序
        hypotheses.sort(key=lambda h: h.created_at)
        return hypotheses

    def verify_hypothesis(
        self, hypothesis_id: str, new_evidence_ids: List[str],
    ) -> Hypothesis:
        """将假设标记为已验证.

        Args:
            hypothesis_id: 假设 ID。
            new_evidence_ids: 新增的证据 ID 列表。

        Returns:
            更新后的 Hypothesis 对象。

        Raises:
            KeyError: 假设 ID 不存在时抛出。
            ValueError: 假设已非 pending 状态时抛出。
        """
        hypothesis = self._get_hypothesis_or_raise(hypothesis_id)

        if hypothesis.status != STATUS_PENDING:
            raise ValueError(
                f"Cannot verify hypothesis {hypothesis_id}: "
                f"current status is '{hypothesis.status}', expected '{STATUS_PENDING}'"
            )

        hypothesis.status = STATUS_VERIFIED
        # 合并新证据
        evidence_ids = _safe_text_list(new_evidence_ids)
        existing = set(hypothesis.based_on_evidence_ids)
        for eid in evidence_ids:
            if eid not in existing:
                hypothesis.based_on_evidence_ids.append(eid)

        logger.info(
            "Verified hypothesis %s with %d new evidence IDs",
            hypothesis_id, len(evidence_ids),
        )
        return hypothesis

    def reject_hypothesis(
        self, hypothesis_id: str, reason: str,
    ) -> Hypothesis:
        """将假设标记为已驳回.

        Args:
            hypothesis_id: 假设 ID。
            reason: 驳回理由。

        Returns:
            更新后的 Hypothesis 对象。

        Raises:
            KeyError: 假设 ID 不存在时抛出。
            ValueError: 假设已非 pending 状态时抛出。
        """
        hypothesis = self._get_hypothesis_or_raise(hypothesis_id)

        if hypothesis.status != STATUS_PENDING:
            raise ValueError(
                f"Cannot reject hypothesis {hypothesis_id}: "
                f"current status is '{hypothesis.status}', expected '{STATUS_PENDING}'"
            )

        hypothesis.status = STATUS_REJECTED
        # 将驳回理由追加到 missing_evidence
        hypothesis.missing_evidence.append(f"REJECTED: {_safe_text(reason)}")

        logger.info(
            "Rejected hypothesis %s: %s", hypothesis_id, reason,
        )
        return hypothesis

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export_hypotheses(
        self, output_dir: str = _DEFAULT_OUTPUT_DIR,
    ) -> str:
        """导出假设到 JSON 和 Markdown 文件.

        Args:
            output_dir: 输出目录路径。

        Returns:
            JSON 文件的绝对路径。
        """
        os.makedirs(output_dir, exist_ok=True)

        hypotheses = self.get_hypotheses()
        hypotheses_data = [asdict(h) for h in hypotheses]

        # 导出 JSON
        json_path = os.path.join(output_dir, "hypotheses_latest.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(hypotheses_data, f, ensure_ascii=False, indent=2)

        # 导出 Markdown
        md_path = os.path.join(output_dir, "hypotheses_latest.md")
        md_content = self._generate_markdown(hypotheses)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        logger.info(
            "Exported %d hypotheses to %s", len(hypotheses), output_dir,
        )
        return os.path.abspath(json_path)

    # ------------------------------------------------------------------
    # 从供应链和瓶颈自动创建假设
    # ------------------------------------------------------------------

    def from_chain_and_bottlenecks(
        self,
        chain: DynamicSupplyChain,
        bottlenecks: List[BottleneckHypothesis],
        candidates: List[CompanyCandidate],
    ) -> List[Hypothesis]:
        """从供应链、瓶颈假设和公司候选中自动创建假设.

        自动识别以下场景并创建假设：
        - 供应链中标记为潜在瓶颈的节点
        - BottleneckHypothesis 中标记为假设的条目
        - CompanyCandidate 中证据等级为 HYPOTHESIS 的条目

        Args:
            chain: 动态供应链对象。
            bottlenecks: 瓶颈假设列表。
            candidates: 公司候选列表。

        Returns:
            新创建的 Hypothesis 列表。
        """
        new_hypotheses: List[Hypothesis] = []
        if chain is None:
            return new_hypotheses
        if not isinstance(bottlenecks, (list, tuple)):
            bottlenecks = []
        if not isinstance(candidates, (list, tuple)):
            candidates = []

        # 1. 潜在瓶颈节点
        nodes = getattr(chain, "nodes", ())
        if not isinstance(nodes, (list, tuple)):
            nodes = []
        for node in nodes:
            if not getattr(node, "is_potential_bottleneck", False):
                hyp = self.register_hypothesis(
                    hypothesis_type=HYPOTHESIS_TYPE_CHAIN_NODE,
                    reason=f"供应链节点 '{node.name}' 被标记为潜在瓶颈: {node.hypothesis_reason}",
                    based_on_evidence_ids=_safe_text_list(getattr(node, "evidence_ids", None)),
                    missing_evidence=["需要六问评估确认瓶颈强度"],
                    verification_queries=[
                        f"查询 {node.name} 的供应商集中度和切换成本",
                        f"查询 {node.name} 的产能扩张计划",
                    ],
                    priority=PRIORITY_HIGH,
                )
                new_hypotheses.append(hyp)

        # 2. 瓶颈假设中尚未验证的条目
        for bn in bottlenecks:
            if not isinstance(bn, BottleneckHypothesis) or not bn.is_hypothesis:
                hyp = self.register_hypothesis(
                    hypothesis_type=HYPOTHESIS_TYPE_BOTTLENECK,
                    reason=(
                        f"瓶颈假设 '{bn.node_name}' 尚未充分验证，"
                        f"紫苏概率={bn.perilla_probability:.2f}，"
                        f"总分={bn.total_score}"
                    ),
                    based_on_evidence_ids=_safe_text_list(getattr(bn, "evidence_ids", None)),
                    missing_evidence=self._infer_missing_from_six_questions(
                        bn.six_question_scores,
                    ),
                    verification_queries=[
                        f"验证 {bn.node_name} 的供应瓶颈假设",
                        f"查询 {bn.node_name} 的客户认证和订单情况",
                    ],
                        priority=(
                            PRIORITY_HIGH
                            if _safe_float(bn.perilla_probability) >= 0.6
                            else PRIORITY_MEDIUM
                        ),
                )
                new_hypotheses.append(hyp)

        # 3. 证据等级为 HYPOTHESIS 的公司候选
        for cand in candidates:
            if not isinstance(cand, CompanyCandidate):
                continue
            if cand.evidence_level == EVIDENCE_LEVEL_HYPOTHESIS:
                hyp = self.register_hypothesis(
                    hypothesis_type=HYPOTHESIS_TYPE_COMPANY_MAPPING,
                    reason=(
                        f"公司 '{cand.company_name}' 映射到供应链节点 "
                        f"缺乏外部证据支持"
                    ),
                    based_on_evidence_ids=_safe_text_list(getattr(cand, "evidence_ids", None)),
                    missing_evidence=[
                        "需要任何外部证据支持公司-节点映射",
                        "需要公告/官网/研报确认供应关系",
                    ],
                    verification_queries=[
                        f"搜索 {cand.company_name} 供应关系公告",
                        f"搜索 {cand.company_name} 客户认证信息",
                    ],
                    priority=PRIORITY_MEDIUM,
                )
                new_hypotheses.append(hyp)

        logger.info(
            "Auto-created %d hypotheses from chain/bottlenecks/candidates",
            len(new_hypotheses),
        )
        return new_hypotheses

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _get_hypothesis_or_raise(self, hypothesis_id: str) -> Hypothesis:
        """获取假设或抛出 KeyError.

        Args:
            hypothesis_id: 假设 ID。

        Returns:
            Hypothesis 对象。

        Raises:
            KeyError: 假设 ID 不存在。
        """
        if hypothesis_id not in self._hypotheses:
            raise KeyError(f"Hypothesis '{hypothesis_id}' not found")
        return self._hypotheses[hypothesis_id]

    @staticmethod
    def _infer_missing_from_six_questions(
        scores: Dict[str, int],
    ) -> List[str]:
        """根据六问评分推断缺失证据.

        评分低于 3 的维度视为证据不足。

        Args:
            scores: 六问评分字典。

        Returns:
            缺失证据描述列表。
        """
        dimension_labels = {
            "attach_rate": "搭载率/渗透率证据",
            "bom_share": "BOM 占比证据",
            "supplier_concentration": "供应商集中度证据",
            "capacity_expansion": "产能扩张证据",
            "switching_cost": "切换成本证据",
            "pricing_state": "定价权/价格状态证据",
        }

        if not isinstance(scores, dict):
            scores = {}
        missing = []
        for key, label in dimension_labels.items():
            try:
                score = float(scores.get(key, 0) or 0)
                if not math.isfinite(score):
                    score = 0.0
            except (TypeError, ValueError, OverflowError):
                score = 0.0
            if score < 3:
                missing.append(f"需要{label}（当前评分: {score}/10）")

        return missing

    @staticmethod
    def _generate_markdown(hypotheses: List[Hypothesis]) -> str:
        """生成假设报告的 Markdown 内容.

        Args:
            hypotheses: 假设列表。

        Returns:
            Markdown 格式的报告字符串。
        """
        lines = ["# Perilla Discovery - 假设报告", ""]

        if not hypotheses:
            lines.append("暂无假设记录。")
            return "\n".join(lines)

        # 统计
        status_counts = {STATUS_PENDING: 0, STATUS_VERIFIED: 0, STATUS_REJECTED: 0}
        for h in hypotheses:
            status_counts[h.status] = status_counts.get(h.status, 0) + 1

        lines.append("## 概览")
        lines.append("")
        lines.append(f"- 总计: {len(hypotheses)} 条假设")
        lines.append(f"- 待验证: {status_counts.get(STATUS_PENDING, 0)}")
        lines.append(f"- 已验证: {status_counts.get(STATUS_VERIFIED, 0)}")
        lines.append(f"- 已驳回: {status_counts.get(STATUS_REJECTED, 0)}")
        lines.append("")

        # 按状态分组
        for status in [STATUS_PENDING, STATUS_VERIFIED, STATUS_REJECTED]:
            status_hyps = [h for h in hypotheses if h.status == status]
            if not status_hyps:
                continue

            status_label = {
                STATUS_PENDING: "待验证",
                STATUS_VERIFIED: "已验证",
                STATUS_REJECTED: "已驳回",
            }.get(status, status)

            lines.append(f"## {status_label}")
            lines.append("")

            for h in status_hyps:
                priority_label = {
                    PRIORITY_HIGH: "高",
                    PRIORITY_MEDIUM: "中",
                    PRIORITY_LOW: "低",
                }.get(h.verification_priority, h.verification_priority)

                lines.append(f"### {h.hypothesis_id}")
                lines.append(f"- **类型**: {h.hypothesis_type}")
                lines.append(f"- **优先级**: {priority_label}")
                lines.append(f"- **理由**: {h.reason}")
                lines.append(f"- **创建时间**: {h.created_at}")

                if h.based_on_evidence_ids:
                    lines.append(
                        f"- **依据证据**: {', '.join(h.based_on_evidence_ids)}"
                    )
                if h.missing_evidence:
                    lines.append(f"- **缺失证据**:")
                    for me in h.missing_evidence:
                        lines.append(f"  - {me}")
                if h.verification_queries:
                    lines.append(f"- **验证查询**:")
                    for vq in h.verification_queries:
                        lines.append(f"  - {vq}")

                lines.append("")

        return "\n".join(lines)
