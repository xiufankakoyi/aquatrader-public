"""Claim 晋升引擎。

统一负责 claim 评分、状态晋升/降级、edge/path 置信度传播与 promotion log。

在 Path-based Alpha Discovery 架构中，本引擎新增 path-aware promotion：
- 单条 claim 不允许脱离路径上下文直接晋升。
- 晋升必须通过 ontology check、evidence check、contradiction check、path quality check。
- EventClaim 必须先转为 PromotionCandidate，再经路径验证后才可晋升。
- 路径与已有 stable claim 冲突时，交给 ContradictionDetector，不覆盖旧 claim。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    ClaimStatus,
    apply_source_floor,
    get_source_weight,
)
from core.research.perilla.alpha.claim_quality_scorer import ClaimQualityScorer
from core.research.perilla.alpha.contradiction_detector import ContradictionDetector
from core.research.perilla.event.event_graph_schema import (
    EventClaim,
    EventClaimStatus,
)
from core.research.perilla.alpha.path_discovery_schema import (
    PathEvidenceBundle,
    PathQualityReport,
    PathVerificationResult,
    PromotionDecision,
)
from core.research.perilla.alpha.sag_knowledge_base import Claim, SAGKnowledgeBase
from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry
from core.research.perilla.shared.ontology.schema_promotion_gate import (
    SchemaPromotionGate,
)

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite(value, default)))


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


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


def _edge_tuple(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    return tuple(_text(item) for item in value)

# 连续评分基础区间
STATUS_BY_SCORE: list[tuple[float, str]] = [
    (0.00, ClaimStatus.RAW.value),
    (0.30, ClaimStatus.CANDIDATE.value),
    (0.55, ClaimStatus.SUPPORTED.value),
    (0.75, ClaimStatus.TRUSTED.value),
]

def _score_to_status(score: float) -> str:
    """根据分数映射到基础状态区间。"""
    score = _score(score)
    status = ClaimStatus.RAW.value
    for threshold, st in STATUS_BY_SCORE:
        if score >= threshold:
            status = st
    return status


class ClaimPromotionEngine:
    """Claim 评分、晋升、降级与传播引擎。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        quality_scorer: ClaimQualityScorer | None = None,
        contradiction_detector: ContradictionDetector | None = None,
        ontology_registry: OntologyRegistry | None = None,
        promotion_gate: SchemaPromotionGate | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库
            quality_scorer: Claim 质量评分器，None 则新建
            contradiction_detector: 冲突检测器，None 则新建
            ontology_registry: Ontology 注册表，None 则使用默认 canonical ontology
            promotion_gate: Schema 晋升门，None 则使用默认门控
        """
        self.kb = kb
        self.scorer = quality_scorer or ClaimQualityScorer(kb)
        self.detector = contradiction_detector or ContradictionDetector(kb)
        self.ontology_registry = ontology_registry or OntologyRegistry.default()
        self.promotion_gate = promotion_gate or SchemaPromotionGate(
            min_score=0.7,
            require_review=False,
        )

    # ------------------------------------------------------------------
    # Path-aware promotion (new main API for Alpha Discovery pipeline)
    # ------------------------------------------------------------------

    def promote_from_path_evidence(
        self,
        bundle: PathEvidenceBundle,
        verification: PathVerificationResult,
        quality_report: PathQualityReport,
    ) -> PromotionDecision:
        """基于路径证据生成路径级晋升决策。

        核心规则：
        - 仅当 quality_label 为 PROMOTION_READY_PATH 或 STRONG_ALPHA_PATH 时才允许晋升。
        - 路径必须满足 ontology、evidence、contradiction、path quality 四项检查。
        - 仅 promotion 对应 edge 被 verification.supported_edges 覆盖的 claim。
        - weak/unsupported edges 上的 claim 降级或拒绝。
        - EventClaim 必须先经路径验证，不直接晋升。
        - 与已有 stable claim 冲突时，交给 ContradictionDetector，不覆盖旧 claim。

        Args:
            bundle: 路径证据包。
            verification: 路径验证结果。
            quality_report: 路径质量报告。

        Returns:
            PromotionDecision。
        """
        if not isinstance(bundle, PathEvidenceBundle):
            return PromotionDecision(path_id="", decision_reason="invalid evidence bundle")
        if not isinstance(verification, PathVerificationResult):
            verification = PathVerificationResult(path_id="")
        if not isinstance(quality_report, PathQualityReport):
            return PromotionDecision(path_id="", decision_reason="invalid quality report")

        candidate = bundle.path_candidate if isinstance(bundle.path_candidate, object) else None
        path_id = _text(_field(candidate, "path_id", ""))
        stable_claims = [claim for claim in _items(_field(bundle, "stable_claims", [])) if isinstance(claim, Claim)]

        # 1. Path quality gate
        quality_check = self._check_path_quality(quality_report)
        if not quality_check["passed"]:
            return PromotionDecision(
                path_id=path_id,
                rejected_claims=[_text(c.claim_id) for c in stable_claims],
                decision_reason=quality_check["reason"],
                ontology_check_result={"passed": False, "reason": "path quality gate failed"},
                evidence_check_result={"passed": False, "reason": "path quality gate failed"},
                contradiction_check_result={"passed": False, "reason": "path quality gate failed"},
            )

        # 2. Ontology check
        ontology_check = self._check_ontology(bundle)

        # 3. Evidence check
        evidence_check = self._check_evidence(bundle, verification)

        # 4. Contradiction check
        contradiction_check = self._check_contradiction(bundle, verification)

        promotable_claims: list[str] = []
        rejected_claims: list[str] = []
        downgrade_claims: list[str] = []
        required_evidence: list[dict[str, Any]] = []

        if not ontology_check["passed"]:
            # Ontology incompatible: reject all claims on the path
            rejected_claims = [_text(c.claim_id) for c in stable_claims]
            return PromotionDecision(
                path_id=path_id,
                rejected_claims=rejected_claims,
                required_evidence=[{
                    "reason": "ontology_mismatch",
                    "detail": ontology_check["reason"],
                }],
                ontology_check_result=ontology_check,
                evidence_check_result=evidence_check,
                contradiction_check_result=contradiction_check,
                decision_reason=f"ontology check failed: {ontology_check['reason']}",
            )

        # Decide per-claim status based on verification edge coverage
        supported_edge_set = {
            edge_key for edge in _items(_field(verification, "supported_edges", []))
            for edge_key in [_edge_tuple(edge)] if edge_key is not None
        }
        weak_edge_set = {
            edge_key for edge in _items(_field(verification, "weak_edges", []))
            for edge_key in [_edge_tuple(edge)] if edge_key is not None
        }
        unsupported_edge_set = {
            edge_key for edge in _items(_field(verification, "unsupported_edges", []))
            for edge_key in [_edge_tuple(edge)] if edge_key is not None
        }

        for claim in stable_claims:
            edge_key = (_text(claim.head), _text(claim.relation), _text(claim.tail))

            if edge_key in unsupported_edge_set:
                rejected_claims.append(claim.claim_id)
                continue

            if edge_key in weak_edge_set:
                downgrade_claims.append(claim.claim_id)
                required_evidence.append({
                    "edge_key": list(edge_key),
                    "reason": "weak_edge_requires_stronger_evidence",
                    "preferred_sources": self._preferred_sources_for_relation(claim.relation),
                })
                continue

            if edge_key in supported_edge_set and evidence_check["passed"]:
                # Apply contradiction arbitration
                try:
                    conflict_records = self.detector.detect_for_claim(claim)
                except (AttributeError, KeyError, TypeError, ValueError):
                    conflict_records = []
                if conflict_records:
                    arbitration = self.detector.apply_records(conflict_records)
                    # If the claim is explicitly marked contradicted/rejected by arbitration,
                    # respect it unless it would silently demote a trusted claim to rejected.
                    for record in conflict_records:
                        if record.primary_claim_id == claim.claim_id:
                            suggested = record.suggested_status
                            if claim.status == ClaimStatus.TRUSTED.value and suggested == ClaimStatus.REJECTED.value:
                                continue
                            if suggested in (ClaimStatus.CONTRADICTED.value, ClaimStatus.REJECTED.value):
                                rejected_claims.append(claim.claim_id)
                                break
                            elif suggested == ClaimStatus.DEPRECATED.value:
                                downgrade_claims.append(claim.claim_id)
                                break
                    else:
                        promotable_claims.append(claim.claim_id)
                else:
                    promotable_claims.append(claim.claim_id)
            else:
                downgrade_claims.append(claim.claim_id)

        # Apply actual status changes for promotable claims
        promoted_ids: list[str] = []
        for claim_id in promotable_claims:
            claim = _mapping_get(getattr(self.kb, "claims", {}), claim_id)
            if not claim:
                continue
            score, _ = self.score_claim(claim)
            new_status, reason, _ = self._apply_hysteresis(claim, score)
            # For path-aware promotion we treat verified supported edges as a supporting signal
            if new_status in (ClaimStatus.RAW.value, ClaimStatus.CANDIDATE.value) and score >= 0.55:
                new_status = ClaimStatus.SUPPORTED.value
                reason = "path-aware promotion to supported based on verified edge"
            if new_status in (
                ClaimStatus.SUPPORTED.value,
                ClaimStatus.CANDIDATE.value,
                ClaimStatus.PENDING_VALIDATION.value,
            ) and score >= 0.75:
                new_status = ClaimStatus.TRUSTED.value
                reason = "path-aware promotion to trusted based on verified edge and score"

            if new_status != claim.status:
                self.kb.update_claim_status(
                    claim_id=claim_id,
                    new_status=new_status,
                    reason=reason,
                    trigger_claim_id=path_id,
                    changed_by="path_promotion",
                )
            self.kb.update_edge_confidence((claim.head, claim.relation, claim.tail))
            promoted_ids.append(claim_id)

        # Apply downgrades
        for claim_id in downgrade_claims:
            claim = _mapping_get(getattr(self.kb, "claims", {}), claim_id)
            if not claim:
                continue
            if claim.status in (ClaimStatus.TRUSTED.value, ClaimStatus.SUPPORTED.value):
                self.kb.update_claim_status(
                    claim_id=claim_id,
                    new_status=ClaimStatus.PENDING_DOWNGRADE.value,
                    reason="edge marked weak by path verification",
                    trigger_claim_id=path_id,
                    changed_by="path_promotion",
                )

        # Apply rejections
        for claim_id in rejected_claims:
            claim = _mapping_get(getattr(self.kb, "claims", {}), claim_id)
            if not claim:
                continue
            if claim.status not in (ClaimStatus.TRUSTED.value, ClaimStatus.CONTRADICTED.value):
                self.kb.update_claim_status(
                    claim_id=claim_id,
                    new_status=ClaimStatus.REJECTED.value,
                    reason="edge unsupported by path verification or ontology mismatch",
                    trigger_claim_id=path_id,
                    changed_by="path_promotion",
                )

        decision_reason = self._build_decision_reason(
            quality_report, ontology_check, evidence_check, contradiction_check
        )

        return PromotionDecision(
            path_id=path_id,
            promotable_claims=promoted_ids,
            rejected_claims=rejected_claims,
            downgrade_claims=downgrade_claims,
            required_evidence=required_evidence,
            ontology_check_result=ontology_check,
            evidence_check_result=evidence_check,
            contradiction_check_result=contradiction_check,
            decision_reason=decision_reason,
        )

    def _check_path_quality(self, quality_report: PathQualityReport) -> dict[str, Any]:
        """Check whether the path quality report allows promotion."""
        if not isinstance(quality_report, PathQualityReport):
            return {"passed": False, "reason": "invalid quality report"}
        allowed_labels = {"PROMOTION_READY_PATH", "STRONG_ALPHA_PATH"}
        quality_label = _text(quality_report.quality_label)
        if quality_label not in allowed_labels:
            return {
                "passed": False,
                "reason": f"quality_label={quality_label} not promotable",
            }
        if _score(quality_report.consistency_score) < 0.35:
            return {
                "passed": False,
                "reason": "consistency_score too low",
            }
        return {"passed": True, "reason": "path quality gate passed"}

    def _check_ontology(self, bundle: PathEvidenceBundle) -> dict[str, Any]:
        """Check whether the path is compatible with the stable ontology.

        校验规则：
        - 路径类型必须在 ontology registry 的可晋升集合中（或为 "unknown" 降级处理）。
        - 路径中所有 relation 必须已注册。
        - 路径节点类型不能过半为 generic。
        - 若 promotion_gate 显式拒绝，则整体不通过。
        """
        candidate = bundle.path_candidate

        path_type = _text(_field(candidate, "path_type", "unknown"), "unknown")
        try:
            promotable_path_type = self.ontology_registry.is_promotable_path_type(path_type)
        except (AttributeError, KeyError, TypeError, ValueError):
            promotable_path_type = False
        if path_type != "unknown" and not promotable_path_type:
            return {
                "passed": False,
                "reason": f"path_type={path_type} not in promotable ontology",
            }

        for edge_key in _items(_field(candidate, "edges", None)):
            if not isinstance(edge_key, (list, tuple)) or len(edge_key) != 3:
                return {"passed": False, "reason": "malformed path edge"}
            relation = edge_key[1]
            try:
                valid_relation = self.ontology_registry.is_valid_relation(relation)
            except (AttributeError, KeyError, TypeError, ValueError):
                valid_relation = False
            if not valid_relation:
                return {
                    "passed": False,
                    "reason": f"relation={relation} not in stable ontology",
                }

        # Verify node types are not all generic
        node_types = _items(_field(candidate, "node_types", None))
        if node_types:
            generic_ratio = sum(
                1 for t in node_types if t == "generic"
            ) / len(node_types)
            if generic_ratio > 0.5:
                return {
                    "passed": False,
                    "reason": "too many generic nodes in path",
                }

        return {"passed": True, "reason": "ontology check passed"}

    def _check_evidence(
        self,
        bundle: PathEvidenceBundle,
        verification: PathVerificationResult,
    ) -> dict[str, Any]:
        """Check whether evidence coverage is sufficient for promotion."""
        coverage_score = _score(_field(bundle, "coverage_score", 0.0))
        evidence_strength = _score(_field(verification, "evidence_strength", 0.0))
        supported_edges = _items(_field(verification, "supported_edges", []))
        if coverage_score < 0.5:
            return {
                "passed": False,
                "reason": f"coverage_score={coverage_score} below threshold",
            }
        if evidence_strength < 0.4:
            return {
                "passed": False,
                "reason": f"evidence_strength={evidence_strength} below threshold",
            }
        if not supported_edges:
            return {
                "passed": False,
                "reason": "no supported edges from verifier",
            }
        return {"passed": True, "reason": "evidence check passed"}

    def _check_contradiction(
        self,
        bundle: PathEvidenceBundle,
        verification: PathVerificationResult,
    ) -> dict[str, Any]:
        """Check whether the path contains unresolved contradictions."""
        contradictions = _items(_field(verification, "contradictions", []))
        signals = _items(_field(bundle, "contradiction_signals", []))
        if contradictions:
            return {
                "passed": False,
                "reason": f"verifier found {len(contradictions)} contradictions",
            }
        if signals:
            return {
                "passed": False,
                "reason": f"bundle contains {len(signals)} contradiction signals",
            }
        return {"passed": True, "reason": "no contradictions detected"}

    @staticmethod
    def _build_decision_reason(
        quality_report: PathQualityReport,
        ontology_check: dict[str, Any],
        evidence_check: dict[str, Any],
        contradiction_check: dict[str, Any],
    ) -> str:
        """Build a human-readable decision reason."""
        parts = [
            f"quality_label={_text(quality_report.quality_label)}",
            f"final_score={_score(quality_report.final_quality_score):.2f}",
            f"ontology={'pass' if _mapping(ontology_check).get('passed') else 'fail'}",
            f"evidence={'pass' if _mapping(evidence_check).get('passed') else 'fail'}",
            f"contradiction={'pass' if _mapping(contradiction_check).get('passed') else 'fail'}",
        ]
        return "; ".join(parts)

    @staticmethod
    def _preferred_sources_for_relation(relation: str) -> list[str]:
        """根据关系类型推荐首选数据源。"""
        if relation in ("supplied_by", "import_dependent_on"):
            return ["annual_report", "announcement", "official_website", "wencai"]
        if relation in ("bottleneck_of", "constrained_by"):
            return ["broker_report", "industry_whitepaper", "patent", "standard"]
        if relation == "certified_by":
            return ["announcement", "official_website", "standard"]
        if relation in ("made_of", "contains"):
            return ["broker_report", "industry_whitepaper", "patent"]
        if relation == "substitute_for":
            return ["broker_report", "news", "patent"]
        return ["broker_report", "web_search"]

    # ------------------------------------------------------------------
    # Claim-level scoring and promotion (legacy API, kept for callers)
    # ------------------------------------------------------------------

    def score_claim(self, claim: Claim) -> tuple[float, dict[str, Any]]:
        """计算 claim 综合评分并应用 source floor。

        Args:
            claim: 待评分 claim

        Returns:
            (claim_score, components_dict)
        """
        if not isinstance(claim, Claim):
            return 0.0, {}
        components = self.scorer.compute_components(claim, self.kb)

        # 如果 claim 已被标记冲突惩罚，优先使用
        contradiction_penalty = _score(claim.contradiction_penalty)
        if contradiction_penalty:
            components.contradiction_penalty = contradiction_penalty

        raw_score = components.compute()
        source_weight = get_source_weight(_text(claim.source_type))
        final_score = apply_source_floor(raw_score, source_weight)
        final_score = max(0.0, min(1.0, final_score))

        claim.claim_score = round(final_score, 4)

        components_dict = {
            "source_weight": components.source_weight,
            "extractor_confidence": components.extractor_confidence,
            "evidence_clarity": components.evidence_clarity,
            "relation_clarity": components.relation_clarity,
            "recency_score": components.recency_score,
            "text_quality_score": components.text_quality_score,
            "noise_penalty": components.noise_penalty,
            "contradiction_penalty": components.contradiction_penalty,
        }
        return final_score, components_dict

    def _collect_supporting_claims(self, claim: Claim) -> list[Claim]:
        """收集同 edge 上的其他 supporting claim（不含自己）。"""
        edge_key = (_text(claim.head), _text(claim.relation), _text(claim.tail))
        edge = _mapping_get(getattr(self.kb, "edges", {}), edge_key)
        if not edge:
            return []

        result: list[Claim] = []
        for cid in _items(_field(edge, "claim_ids", None)):
            if cid == claim.claim_id:
                continue
            other = _mapping_get(getattr(self.kb, "claims", {}), cid)
            if other and _field(other, "status", "") not in (
                ClaimStatus.REJECTED.value,
                ClaimStatus.DEPRECATED.value,
            ):
                result.append(other)
        return result

    def _apply_hysteresis(
        self,
        claim: Claim,
        new_score: float,
    ) -> tuple[str, str, dict[str, Any]]:
        """应用滞后机制决定 claim 状态。

        核心规则：
        - 冲突/废弃状态不受单次评分影响。
        - 跨越阈值时先进 pending，满足连续条件才正式晋升/降级。
        - rejected 需要显式人工恢复，评分不自动改变。

        Returns:
            (new_status, reason, context)
        """
        new_score = _score(new_score)
        old_status = _text(claim.status, ClaimStatus.RAW.value)
        base_status = _score_to_status(new_score)
        supporting = self._collect_supporting_claims(claim)
        prev_score = self._get_last_score(claim)

        context: dict[str, Any] = {
            "score": round(new_score, 4),
            "previous_score": prev_score,
            "base_status": base_status,
            "old_status": old_status,
            "supporting_claim_count": len(supporting),
        }

        # 冲突/废弃状态不随评分自动恢复
        if old_status in (ClaimStatus.CONTRADICTED.value, ClaimStatus.DEPRECATED.value):
            return old_status, f"旧状态 {old_status} 不受单次评分影响", context

        # rejected 需要人工复查恢复，评分不自动恢复
        if old_status == ClaimStatus.REJECTED.value:
            return old_status, "rejected claim 需人工复查恢复，评分不自动改变", context

        # pending_downgrade 状态：连续两次低于阈值才正式降级；回升则恢复
        if old_status == ClaimStatus.PENDING_DOWNGRADE.value:
            prev_status = _score_to_status(prev_score or new_score)
            if prev_status != ClaimStatus.PENDING_DOWNGRADE.value and base_status == prev_status:
                return base_status, f"连续评分低于阈值 {new_score:.2f}，正式降级为 {base_status}", context
            if base_status == ClaimStatus.TRUSTED.value:
                return ClaimStatus.TRUSTED.value, f"分数回升至 {new_score:.2f}，恢复 trusted", context
            if base_status == ClaimStatus.SUPPORTED.value and prev_status == ClaimStatus.SUPPORTED.value:
                return ClaimStatus.SUPPORTED.value, f"分数回升至 {new_score:.2f}，恢复 supported", context
            return old_status, f"分数 {new_score:.2f} 仍处于观察期，保持 pending_downgrade", context

        # 降级路径：进入 pending_downgrade
        if old_status == ClaimStatus.TRUSTED.value and base_status != ClaimStatus.TRUSTED.value:
            context["pending"] = True
            return (
                ClaimStatus.PENDING_DOWNGRADE.value,
                f"分数降至 {new_score:.2f}，进入 pending_downgrade 观察",
                context,
            )

        if old_status == ClaimStatus.SUPPORTED.value and base_status in (
            ClaimStatus.RAW.value,
            ClaimStatus.CANDIDATE.value,
        ):
            context["pending"] = True
            return (
                ClaimStatus.PENDING_DOWNGRADE.value,
                f"分数降至 {new_score:.2f}，进入 pending_downgrade 观察",
                context,
            )

        # 晋升路径
        if old_status in (ClaimStatus.RAW.value, ClaimStatus.CANDIDATE.value):
            if base_status == ClaimStatus.SUPPORTED.value:
                if self._can_promote_to_supported(claim, new_score, supporting):
                    return (
                        ClaimStatus.SUPPORTED.value,
                        f"分数 {new_score:.2f} 满足 supported 晋升条件",
                        context,
                    )
                return (
                    ClaimStatus.PENDING_VALIDATION.value,
                    f"分数刚达 supported 阈值 {new_score:.2f}，等待二次确认",
                    context,
                )
            if base_status == ClaimStatus.TRUSTED.value:
                if self._can_promote_to_trusted(claim, new_score, supporting):
                    return (
                        ClaimStatus.TRUSTED.value,
                        f"分数 {new_score:.2f} 满足 trusted 晋升条件",
                        context,
                    )
                return (
                    ClaimStatus.PENDING_VALIDATION.value,
                    f"分数刚达 trusted 阈值 {new_score:.2f}，等待二次确认",
                    context,
                )

        if old_status in (
            ClaimStatus.SUPPORTED.value,
            ClaimStatus.PENDING_VALIDATION.value,
        ) and base_status == ClaimStatus.TRUSTED.value:
            if self._can_promote_to_trusted(claim, new_score, supporting):
                return (
                    ClaimStatus.TRUSTED.value,
                    f"分数 {new_score:.2f} 满足 trusted 晋升条件",
                    context,
                )
            if old_status == ClaimStatus.PENDING_VALIDATION.value:
                return (
                    ClaimStatus.PENDING_VALIDATION.value,
                    f"分数保持 trusted 区间 {new_score:.2f}，等待下一次确认",
                    context,
                )
            return (
                ClaimStatus.PENDING_VALIDATION.value,
                f"分数刚达 trusted 阈值 {new_score:.2f}，等待二次确认",
                context,
            )

        # pending_validation 状态下分数回落
        if old_status == ClaimStatus.PENDING_VALIDATION.value:
            return base_status, f"分数回落至 {new_score:.2f}，回到 {base_status}", context

        # 其他情况保持基础区间状态
        if old_status != base_status:
            return base_status, f"分数 {new_score:.2f} 对应状态 {base_status}", context
        return old_status, "分数保持稳定，状态不变", context

    def _get_last_score(self, claim: Claim) -> float | None:
        """获取 claim 上一次评分（从 metadata pending_scores 历史）。"""
        pending_scores = _items(_mapping(_field(claim, "metadata", None)).get("pending_scores"))
        if pending_scores:
            value = _finite(pending_scores[-1], float("nan"))
            return value if math.isfinite(value) else None
        return None

    def _record_score(self, claim: Claim, score: float) -> None:
        """记录最近两次评分，用于 hysteresis 判断。"""
        metadata = dict(_mapping(_field(claim, "metadata", None)))
        pending_scores = _items(metadata.get("pending_scores"))
        pending_scores.append(round(_score(score), 4))
        metadata["pending_scores"] = pending_scores[-3:]
        claim.metadata = metadata

    def _has_consecutive_scores(
        self,
        claim: Claim,
        threshold: float,
        n: int = 2,
        current_score: float | None = None,
    ) -> bool:
        """检查最近 n 次评分（包含当前评分）是否都超过阈值。"""
        threshold = _score(threshold)
        n = max(1, int(_finite(n, 2)))
        scores = [
            _score(score)
            for score in _items(_mapping(_field(claim, "metadata", None)).get("pending_scores"))
            if math.isfinite(_finite(score, float("nan")))
        ]
        if current_score is not None:
            scores = scores + [_score(current_score)]
        return len(scores) >= n and all(s > threshold for s in scores[-n:])

    def _can_promote_to_supported(
        self,
        claim: Claim,
        new_score: float,
        supporting: list[Claim],
    ) -> bool:
        """判断 candidate 是否可以正式晋升为 supported。"""
        # 连续两次评分 > 0.55（包含当前评分）
        if self._has_consecutive_scores(claim, 0.55, n=2, current_score=new_score):
            return True

        # 新增不同 source_id 的支持 claim
        if claim.source_id:
            for other in supporting:
                if other.source_id and other.source_id != claim.source_id:
                    return True

        # 新增更高 source_weight 的支持 claim
        my_weight = get_source_weight(claim.source_type)
        for other in supporting:
            if get_source_weight(other.source_type) > my_weight:
                return True

        return False

    def _can_promote_to_trusted(
        self,
        claim: Claim,
        new_score: float,
        supporting: list[Claim],
    ) -> bool:
        """判断 supported/pending_validation 是否可以正式晋升为 trusted。"""
        # 连续两次评分 > 0.75（包含当前评分）
        if self._has_consecutive_scores(claim, 0.75, n=2, current_score=new_score):
            return True

        # 新增强来源（source_weight >= 0.85）
        for other in supporting:
            if get_source_weight(other.source_type) >= 0.85:
                return True

        return False

    def process_new_claim(self, claim: Claim) -> dict[str, Any]:
        """处理一条新 claim：评分、状态机、冲突检测、传播。

        Args:
            claim: 新 claim（已加入 kb）

        Returns:
            处理结果字典
        """
        if not isinstance(claim, Claim):
            return {
                "claim_id": "",
                "claim_score": 0.0,
                "status": ClaimStatus.RAW.value,
                "status_reason": "invalid claim",
                "promotion_reason": "invalid claim",
                "contradiction_group_id": None,
                "affected_edge_ids": [],
                "affected_path_ids": [],
                "components": {},
            }
        claim_id = _text(claim.claim_id)
        score, components = self.score_claim(claim)
        self._record_score(claim, score)

        new_status, reason, context = self._apply_hysteresis(claim, score)

        # 冲突检测与仲裁
        try:
            conflict_records = self.detector.detect_for_claim(claim)
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.debug("claim contradiction detection failed", exc_info=True)
            conflict_records = []
        contradiction_group_id: str | None = None
        if conflict_records:
            applied = self.detector.apply_records(conflict_records)
            # 以第一条冲突记录的 group id 作为代表
            contradiction_group_id = conflict_records[0].contradiction_group_id
            # 如果本 claim 被冲突仲裁改变状态，应反映到 new_status
            for record in conflict_records:
                if record.primary_claim_id == claim_id and claim.status != record.suggested_status:
                    # 不允许静默把 trusted 降级为 rejected
                    if claim.status == ClaimStatus.TRUSTED.value and record.suggested_status == ClaimStatus.REJECTED.value:
                        continue
                    new_status = record.suggested_status
                    reason = record.resolution_reason
                for other_id, status in record.suggested_status_for_others.items():
                    if other_id == claim_id and claim.status != status:
                        if claim.status == ClaimStatus.TRUSTED.value and status == ClaimStatus.REJECTED.value:
                            continue
                        new_status = status
                        reason = record.resolution_reason

        # 应用状态变化
        if new_status != claim.status:
            try:
                self.kb.update_claim_status(
                    claim_id=claim_id,
                    new_status=new_status,
                    reason=reason,
                    trigger_claim_id=contradiction_group_id,
                    changed_by="system",
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.debug("claim status update failed: %s", claim_id, exc_info=True)

        # 更新 edge confidence
        edge_key = (_text(claim.head), _text(claim.relation), _text(claim.tail))
        try:
            self.kb.update_edge_confidence(edge_key)
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.debug("edge confidence update failed: %s", edge_key, exc_info=True)

        # 记录本次处理摘要到 metadata，便于追溯
        metadata = dict(_mapping(getattr(claim, "metadata", None)))
        promotion_log = _items(metadata.get("promotion_log"))
        promotion_log.append({
            "score": round(score, 4),
            "status": new_status,
            "reason": reason,
            "contradiction_group_id": contradiction_group_id,
            "changed_at": datetime.now().isoformat(),
        })
        metadata["promotion_log"] = promotion_log[-20:]
        claim.metadata = metadata

        return {
            "claim_id": claim_id,
            "claim_score": score,
            "status": new_status,
            "status_reason": reason,
            "promotion_reason": reason,
            "contradiction_group_id": contradiction_group_id,
            "affected_edge_ids": [edge_key],
            "affected_path_ids": [],  # 由 PathConfidencePropagator 填充
            "components": components,
        }

    def reevaluate_claim(self, claim_id: str) -> dict[str, Any] | None:
        """重新评估已有 claim（用于迁移或新增支持证据后）。"""
        claim = _mapping_get(getattr(self.kb, "claims", {}), claim_id)
        if not claim:
            return None
        return self.process_new_claim(claim)

    def update_edge_and_paths(self, edge_key: tuple[str, str, str]) -> dict[str, Any]:
        """更新边置信度并触发路径传播（后续接入 PathConfidencePropagator）。"""
        normalized_edge_key = _edge_tuple(edge_key)
        if normalized_edge_key is None:
            return {"edge_key": edge_key, "confidence": 0.0, "affected_path_ids": []}
        try:
            edge = self.kb.update_edge_confidence(normalized_edge_key)
        except (AttributeError, KeyError, TypeError, ValueError):
            edge = None
        return {
            "edge_key": normalized_edge_key,
            "confidence": _score(_field(edge, "confidence", 0.0)),
            "affected_path_ids": [],
        }

    def can_promote_event_claim(
        self,
        event_claim: EventClaim,
    ) -> tuple[bool, str]:
        """判断事件 claim 是否可以晋升为稳定图 claim。

        Args:
            event_claim: 待判断的事件 claim。

        Returns:
            (是否可晋升, 拒绝原因)。可晋升时原因返回空字符串。
        """
        if not isinstance(event_claim, EventClaim):
            return False, "invalid event claim"
        if event_claim.status in (
            EventClaimStatus.RETRACTED.value,
            EventClaimStatus.CONTRADICTED.value,
            EventClaimStatus.EXPIRED.value,
        ):
            return False, "status not promotable"

        stable_relation = self.ontology_registry.event_type_to_relation(
            event_claim.event_type
        )
        if not stable_relation:
            return False, "no stable relation mapping"

        if not _text(event_claim.evidence):
            return False, "missing evidence"

        if _score(event_claim.confidence) < 0.5:
            return False, "confidence too low"

        if get_source_weight(_text(event_claim.source_type)) < 0.3:
            return False, "source reliability too low"

        event_edge_key = (
            _text(event_claim.subject),
            _text(event_claim.event_type),
            _text(event_claim.object),
        )
        event_edge = _mapping_get(getattr(self.kb, "event_edges", {}), event_edge_key)
        if event_edge is None or _finite(_field(event_edge, "source_count", 0)) < 2:
            return False, "single source"

        if not _text(event_claim.subject) or not _text(event_claim.object):
            return False, "subject/object empty"

        return True, ""

    def promote_event_claim(self, event_claim: EventClaim) -> Claim | None:
        """将单个事件 claim 晋升为稳定图 claim。

        Args:
            event_claim: 待晋升的事件 claim。

        Returns:
            新创建的稳定 Claim；不可晋升时返回 None。
        """
        can_promote, reason = self.can_promote_event_claim(event_claim)
        if not can_promote:
            if not isinstance(event_claim, EventClaim):
                return None
            logger.debug(
                "EventClaim %s 不可晋升: %s", event_claim.event_id, reason
            )
            return None

        stable_relation = self.ontology_registry.event_type_to_relation(
            event_claim.event_type
        )
        if not stable_relation:
            return None

        try:
            stable_claim = self.kb.add_claim(
                head=event_claim.subject,
                relation=stable_relation,
                tail=event_claim.object,
                evidence=event_claim.evidence,
                source_type=event_claim.source_type,
                source_title=event_claim.source_title,
                timestamp=event_claim.source_time or event_claim.time,
                confidence=event_claim.confidence,
                metadata={
                    "promoted_from_event_claim_id": event_claim.event_id,
                    "raw_event_type": event_claim.raw_event_type,
                },
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.debug("event claim promotion write failed", exc_info=True)
            stable_claim = None

        if stable_claim is not None:
            self.kb.update_event_claim_status(
                event_id=event_claim.event_id,
                new_status=EventClaimStatus.PROMOTED.value,
                reason="promoted to stable claim",
                changed_by="system",
            )
            event_claim.promotion_candidate = False

        return stable_claim

    def process_promotion_candidates(
        self,
        event_claims: list[EventClaim],
    ) -> list[dict[str, Any]]:
        """批量处理事件 claim 晋升候选。

        Args:
            event_claims: 待处理的事件 claim 列表。

        Returns:
            处理结果列表，每项包含 event_claim_id、promoted、stable_claim_id、reason。
        """
        results: list[dict[str, Any]] = []
        for event_claim in _items(event_claims):
            stable_claim = self.promote_event_claim(event_claim)
            event_claim_id = _text(_field(event_claim, "event_id", ""))
            if stable_claim is not None:
                results.append({
                    "event_claim_id": event_claim_id,
                    "promoted": True,
                    "stable_claim_id": stable_claim.claim_id,
                    "reason": "promoted to stable claim",
                })
            else:
                _, reason = self.can_promote_event_claim(event_claim)
                results.append({
                    "event_claim_id": event_claim_id,
                    "promoted": False,
                    "stable_claim_id": None,
                    "reason": reason,
                })
        return results

    def get_promotion_log(
        self,
        claim_id: str | None = None,
        edge_key: tuple[str, str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """获取 claim 或 edge 的晋升/状态变化日志。"""
        logs: list[dict[str, Any]] = []
        claims = getattr(self.kb, "claims", {})
        if claim_id and isinstance(claims, Mapping) and claim_id in claims:
            claim = claims[claim_id]
            for h in _items(_field(claim, "status_history", None)):
                if not isinstance(h, Mapping):
                    continue
                logs.append({
                    "type": "claim_status_change",
                    "claim_id": claim_id,
                    **h,
                })
        normalized_edge_key = _edge_tuple(edge_key) if edge_key else None
        edges = getattr(self.kb, "edges", {})
        if normalized_edge_key and isinstance(edges, Mapping) and normalized_edge_key in edges:
            edge = edges[normalized_edge_key]
            logs.append({
                "type": "edge_confidence_update",
                "edge_key": normalized_edge_key,
                "confidence": _score(_field(edge, "confidence", 0.0)),
                "confidence_reason": _text(_field(edge, "confidence_reason", "")),
                "conflict_status": _text(_field(edge, "conflict_status", "none"), "none"),
            })
        return sorted(logs, key=lambda x: x.get("changed_at", ""))
