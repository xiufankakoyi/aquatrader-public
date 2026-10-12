"""Research→QUANT 准入门禁。

P0-1 修复：验证候选必须满足以下条件才能进入 QUANT 流水线：
1. Claim 必须为 ACTIVE/TRUSTED
2. 无冲突（conflict_status 为空/none/resolved）
3. 证据未过期且 PIT 可见
4. 公司映射可信（mapping_confidence >= 0.6 或不涉及公司映射）

门禁失败时返回 AdmissionResult(passed=False, reasons=[...])，
调用方应将未通过门禁的候选标记为 REJECTED + RESEARCH_GATE_FAIL。

设计原则（fail-closed）：
- 不复制投研模型，只读引用
- ResearchRepository 不可用时 fail-closed（拒绝而非放过）
- 所有校验项缺失时视为失败，不得 fail-open
- thesis_id 为空时视为门禁失败（无法反查 Claim）
- thesis_status 为空时视为门禁失败
- claim_status 为空且无 Repository 时视为门禁失败
- mapping_confidence 低于阈值时拒绝（包括 0）
- evidence 为空时视为门禁失败
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.quant.contracts import QuantCandidate, RejectReason

logger = logging.getLogger(__name__)

# Claim 状态准入集合
RESEARCH_ADMISSIBLE_STATUSES = {"ACTIVE", "TRUSTED"}

# 公司映射置信度阈值
MAPPING_CONFIDENCE_THRESHOLD = 0.6

# 观点状态准入集合（ThesisStatus.active_set 等价）
ADMISSIBLE_THESIS_STATUSES = {"active", "high_conviction"}


@dataclass
class AdmissionResult:
    """准入门禁结果。

    Attributes:
        passed: 是否通过门禁。
        reasons: 未通过原因列表（passed=True 时为空）。
        claim_id: 校验时的 Claim ID（可空）。
        claim_status: 校验时的 Claim 状态（可空）。
        mapping_confidence: 校验时的映射置信度。
        degraded: 是否发生降级（Repository 不可用等）。
    """

    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    claim_id: str = ""
    claim_status: str = ""
    mapping_confidence: float = 0.0
    degraded: bool = False


def check_admission(
    candidate: QuantCandidate,
    *,
    research_repository: Any | None = None,
    as_of: str = "",
) -> AdmissionResult:
    """执行 Research 准入门禁校验（fail-closed）。

    所有校验项缺失时视为失败，不得放过未验证的候选。

    Args:
        candidate: QUANT 候选（已适配）。
        research_repository: Research 仓库实例（ResearchRepository）；
            None 时 fail-closed（无法验证 Claim → 拒绝）。
        as_of: PIT 时点，用于证据可见性校验。

    Returns:
        AdmissionResult 门禁结果。
    """
    result = AdmissionResult()
    reasons: list[str] = []

    # 1. thesis_id 必须存在（否则无法反查 Claim）
    if not candidate.thesis_id:
        reasons.append("thesis_id 为空，无法反查 Claim")
        result.reasons = reasons
        return result

    # 2. thesis_status 必须存在且为 active/high_conviction（fail-closed：空=拒绝）
    if not candidate.thesis_status:
        reasons.append("thesis_status 为空，无法确认观点状态")
    elif candidate.thesis_status not in ADMISSIBLE_THESIS_STATUSES:
        reasons.append(
            f"thesis_status={candidate.thesis_status} 不在准入集合 {ADMISSIBLE_THESIS_STATUSES}"
        )

    # 3. 如果候选自身已携带 claim_status，直接校验（fail-closed：空=需 Repository 验证）
    if candidate.claim_status:
        if candidate.claim_status not in RESEARCH_ADMISSIBLE_STATUSES:
            reasons.append(
                f"claim_status={candidate.claim_status} 不在准入集合 {RESEARCH_ADMISSIBLE_STATUSES}"
            )
        result.claim_status = candidate.claim_status

    # 4. mapping_confidence 校验（fail-closed：0 或低于阈值=拒绝）
    result.mapping_confidence = candidate.mapping_confidence
    if candidate.mapping_confidence < MAPPING_CONFIDENCE_THRESHOLD:
        reasons.append(
            f"mapping_confidence={candidate.mapping_confidence} < {MAPPING_CONFIDENCE_THRESHOLD}"
        )

    # 5. 如果有 ResearchRepository，反查 Claim 做完整校验
    if research_repository is not None:
        repo_result = _check_via_repository(
            candidate, research_repository, as_of, reasons
        )
        if repo_result.get("claim_status"):
            result.claim_status = repo_result["claim_status"]
        if repo_result.get("mapping_confidence", 0) > 0:
            result.mapping_confidence = repo_result["mapping_confidence"]
        if repo_result.get("degraded"):
            result.degraded = True
    else:
        # fail-closed：无 Repository 时无法验证 Claim，直接拒绝
        result.degraded = True
        reasons.append("无 ResearchRepository，无法验证 Claim 状态和证据")
        logger.warning(
            "无 ResearchRepository，fail-closed 拒绝: candidate_id=%s thesis_id=%s",
            candidate.candidate_id,
            candidate.thesis_id,
        )

    result.claim_id = candidate.claim_id
    # 6. degraded 候选强制拒绝（P0-5 契约收紧）
    # adapter 标记的关键追溯字段缺失（claim_id/claim_status/mapping_confidence 等）
    # 不得放过进入 QUANT 流水线，fail-closed。
    if candidate.degraded:
        result.degraded = True
        if "候选标记为 degraded（关键字段缺失）" not in reasons:
            reasons.append("候选标记为 degraded（关键字段缺失）")
    result.passed = len(reasons) == 0 and not result.degraded
    result.reasons = reasons
    return result


def _check_via_repository(
    candidate: QuantCandidate,
    repository: Any,
    as_of: str,
    reasons: list[str],
) -> dict[str, Any]:
    """通过 ResearchRepository 反查 Claim 做完整校验。

    Args:
        candidate: QUANT 候选。
        repository: ResearchRepository 实例。
        as_of: PIT 时点。
        reasons: 累积原因列表（就地追加）。

    Returns:
        dict 包含 claim_status/mapping_confidence/degraded。
    """
    result: dict[str, Any] = {"degraded": False}

    try:
        # 通过 thesis_id 反查 Claim
        claim = None
        if candidate.claim_id and hasattr(repository, "get_claim"):
            claim = repository.get_claim(candidate.claim_id)
        elif hasattr(repository, "list_claims"):
            # 尝试通过 thesis_id 在 claims 中查找
            claims = repository.list_claims(limit=500)
            for c in claims:
                # Claim 可能有 thesis_id 字段或在 payload 中
                c_thesis = getattr(c, "thesis_id", "") or ""
                if not c_thesis and hasattr(c, "to_dict"):
                    c_thesis = c.to_dict().get("thesis_id", "")
                if c_thesis == candidate.thesis_id:
                    claim = c
                    break

        if claim is None:
            reasons.append(f"未找到 thesis_id={candidate.thesis_id} 对应的 Claim")
            return result

        claim_status = getattr(claim, "research_status", "") or ""
        result["claim_status"] = claim_status

        # Claim 状态校验
        if claim_status not in RESEARCH_ADMISSIBLE_STATUSES:
            reasons.append(
                f"Claim research_status={claim_status} 不在准入集合 {RESEARCH_ADMISSIBLE_STATUSES}"
            )

        # 冲突校验
        conflict_status = getattr(claim, "conflict_status", "") or ""
        if conflict_status not in {"", "none", "resolved"}:
            reasons.append(f"Claim conflict_status={conflict_status} 存在未解决冲突")

        # 公司映射置信度校验
        mapping_confidence = float(getattr(claim, "mapping_confidence", 0.0) or 0.0)
        result["mapping_confidence"] = mapping_confidence
        object_id = getattr(claim, "object_id", "") or ""
        high_impact = bool(getattr(claim, "high_impact", False))
        if (object_id or high_impact) and mapping_confidence < MAPPING_CONFIDENCE_THRESHOLD:
            reasons.append(
                f"Claim mapping_confidence={mapping_confidence} < {MAPPING_CONFIDENCE_THRESHOLD}"
            )

        # 证据过期校验（fail-closed：空=拒绝，全部不可见=拒绝）
        if hasattr(repository, "list_evidence"):
            try:
                evidence_list = repository.list_evidence(getattr(claim, "claim_id", ""))
                if not evidence_list:
                    reasons.append(f"thesis_id={candidate.thesis_id} 的证据列表为空")
                else:
                    _check_evidence_currency(
                        evidence_list, as_of, candidate.thesis_id, reasons
                    )
            except Exception as exc:
                logger.debug("证据校验失败: %s", exc)
                result["degraded"] = True
                reasons.append(f"证据校验异常: {exc}")

    except Exception as exc:
        logger.warning("ResearchRepository 校验异常: %s", exc)
        result["degraded"] = True
        reasons.append(f"Repository 校验异常: {exc}")

    return result


def _check_evidence_currency(
    evidence_list: list[Any],
    as_of: str,
    thesis_id: str,
    reasons: list[str],
) -> None:
    """校验证据是否过期且 PIT 可见（fail-closed）。

    尝试复用 core.research.control_plane.policies 的过期判断函数；
    如果不可导入，则降级为基于 valid_to 字段的简单检查。

    fail-closed 规则：
    - visible_count == 0 → 拒绝（无论 expired_count 是 0 还是 >0）
    - 证据全部晚于 as_of（未来数据）→ visible_count=0 → 拒绝

    Args:
        evidence_list: 证据列表。
        as_of: PIT 时点。
        thesis_id: 观点 ID（日志用）。
        reasons: 累积原因列表。
    """
    try:
        from core.research.control_plane.policies import (
            evidence_is_current,
            evidence_visible_as_of,
        )

        visible_count = 0
        expired_count = 0
        for evidence in evidence_list:
            if not evidence_is_current(evidence, as_of):
                expired_count += 1
                continue
            if as_of and not evidence_visible_as_of(evidence, as_of, historical=False):
                continue
            visible_count += 1

        # fail-closed：无可见证据即拒绝（包括全部过期、全部未来数据）
        if visible_count == 0:
            reasons.append(
                f"thesis_id={thesis_id} 的证据全部过期或不可见"
                f"（total={len(evidence_list)}, expired={expired_count}）"
            )
    except ImportError:
        # 降级：基于 valid_to 字段简单检查
        logger.debug("policies 模块不可用，降级为简单证据检查")
        cutoff = as_of or ""
        if not cutoff:
            # fail-closed：无 as_of 时无法判断证据可见性
            reasons.append(f"thesis_id={thesis_id} 无 as_of 时点，无法校验证据可见性")
            return
        visible_count = 0
        for evidence in evidence_list:
            valid_to = getattr(evidence, "valid_to", "") or ""
            if valid_to and cutoff > valid_to:
                continue
            visible_count += 1
        if visible_count == 0:
            reasons.append(f"thesis_id={thesis_id} 的证据全部过期（valid_to 早于 {cutoff}）")


def apply_admission_to_candidate(
    candidate: QuantCandidate,
    result: AdmissionResult,
) -> QuantCandidate:
    """把门禁结果应用到候选（设置 admission_passed 和降级标记）。

    Args:
        candidate: 原始候选。
        result: 门禁结果。

    Returns:
        更新后的候选（原对象就地修改并返回）。
    """
    candidate.admission_passed = result.passed
    if result.claim_status:
        candidate.claim_status = result.claim_status
    if result.mapping_confidence > 0:
        candidate.mapping_confidence = result.mapping_confidence
    if not result.passed:
        candidate.degraded = True
        if "admission" not in candidate.degraded_fields:
            candidate.degraded_fields.append("admission")
    if result.degraded and "repository" not in candidate.degraded_fields:
        candidate.degraded_fields.append("repository")
    return candidate
