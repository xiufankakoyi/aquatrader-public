"""动态主题审查处理器。

处理 ``task_type=dynamic_theme_review`` 任务：接收 QUANT 上报的 ThemePacket v1，
执行新闻独立性验证、因果链构建、角色分类、反证检索、新主题合成、历史主题检查，
产出增强后的 ThemePacket v2 与新主题建议列表。

设计原则：
- 不修改入参 ThemePacket（深拷贝后修改）
- 不凭空编造公司业务/概念归属，所有判断基于 ThemePacket 已有字段
- 不输出投资建议（买入/卖出/仓位/止盈/止损）
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.quant.theme.contracts import (
    ThemePacket,
    generate_theme_id,
    generate_theme_run_id,
)
from core.quant.theme.legacy_theme_detector import LegacyThemeDetector

from .contracts import (
    Claim,
    ClaimStatus,
    DocumentVersion,
    EvidenceRecord,
    ResearchTask,
    SourceTrace,
    TaskStatus,
    _as_mapping,
    _safe_bool,
    _safe_float,
    _safe_int,
    _safe_text,
    _text_list,
    utc_now,
)
from .policies import ClaimPolicy, evidence_is_current, evidence_visible_as_of

logger = logging.getLogger(__name__)


# 默认 required_outputs（任务未指定时使用）
DEFAULT_REQUIRED_OUTPUTS: list[str] = [
    "theme_validity",
    "causal_chain",
    "core_companies",
    "counter_evidence",
    "negative_status",
    "new_theme_suggestions",
]

# 核心/领导角色集合（用于 core_members 筛选）
CORE_ROLES: frozenset[str] = frozenset({"core", "leader"})

# 阻断性负面风险阈值
BLOCKING_NEGATIVE_THRESHOLD: float = 0.7
# 警告级负面风险阈值
WARNING_NEGATIVE_THRESHOLD: float = 0.4

# v2 元数据存储键（写入 source_status 字典，避免修改 ThemePacket 结构）
ENHANCED_AT_KEY: str = "enhanced_at"
ENHANCED_BY_KEY: str = "enhanced_by"
ENHANCED_BY_VALUE: str = "research"


@dataclass
class ThemeReviewResult:
    """Theme review 结果。

    Attributes:
        enhanced_packet: Research 增强后的 ThemePacket v2（深拷贝，不修改入参）。
        new_theme_suggestions: 新主题建议列表（最多 max_new_themes 个）。
        review_summary: 审查摘要，含 theme_validity/causal_chain/core_companies/
            counter_evidence/negative_status 等字段。
    """

    enhanced_packet: ThemePacket
    new_theme_suggestions: list[ThemePacket] = field(default_factory=list)
    review_summary: dict[str, Any] = field(default_factory=dict)


class ThemeReviewProcessor:
    """处理 dynamic_theme_review 任务。

    将 QUANT 上报的 ThemePacket v1 经 Research 侧增强为 v2，产出：
    1. 增强后的 ThemePacket v2（携带 enhanced_at/enhanced_by，core_members 重新筛选）
    2. 新主题建议列表（基于 catalysts 独立来源合成，theme_type=research_discovery）
    3. 审查摘要（含新闻独立性、因果链、角色分类、反证、负面状态）

    不输出投资建议，所有判断基于 ThemePacket 已有字段。
    """

    MAX_NEW_THEMES_LIMIT: int = 2
    RETRIEVAL_VERSION: str = "official_page_relationship_grounding_v5"
    # Retrieval-only semantic expansions.  They never establish membership;
    # every resulting quote must still ground company + relation in a source.
    THEME_QUERY_EXPANSIONS: dict[str, list[str]] = {
        "商业航天": ["卫星互联网", "低轨卫星", "星载", "卫星通信", "航天"],
        "AI PC": ["AIPC", "AI电脑", "人工智能电脑"],
        "人工智能": ["AI", "智能计算", "智算", "生成式人工智能"],
    }

    def __init__(
        self,
        legacy_detector: LegacyThemeDetector | None = None,
        max_new_themes: int = 2,
        repository: Any | None = None,
        news_loader: Any | None = None,
        company_name_by_symbol: dict[str, str] | None = None,
        max_claims_per_review: int = 3,
    ) -> None:
        """初始化 ThemeReviewProcessor。

        Args:
            legacy_detector: 历史主题检测器，可选注入。注入后 review_summary 携带 legacy_check。
            max_new_themes: 默认最多合成新主题数，受 MAX_NEW_THEMES_LIMIT 限制。
        """
        self.legacy_detector = legacy_detector
        self.max_new_themes = max(
            0,
            min(
                _safe_int(max_new_themes, self.MAX_NEW_THEMES_LIMIT),
                self.MAX_NEW_THEMES_LIMIT,
            ),
        )
        self.repository = repository
        self.news_loader = news_loader
        self.company_name_by_symbol = (
            dict(company_name_by_symbol) if isinstance(company_name_by_symbol, Mapping) else {}
        )
        self.max_claims_per_review = max(
            1, min(_safe_int(max_claims_per_review, 3), 10)
        )
        self.policy = ClaimPolicy()

    @staticmethod
    def _sanitize_packet(theme_packet: ThemePacket) -> ThemePacket:
        """Copy a packet and keep malformed nested fields from reaching review logic."""
        packet = copy.deepcopy(theme_packet)
        packet.theme_id = _safe_text(packet.theme_id)
        packet.theme_name = _safe_text(packet.theme_name)
        packet.theme_run_id = _safe_text(packet.theme_run_id)
        packet.as_of = _safe_text(packet.as_of)
        packet.trace_id = _safe_text(packet.trace_id)
        packet.aliases = _text_list(packet.aliases)
        packet.source_status = _as_mapping(packet.source_status)
        packet.degradation_reasons = _text_list(packet.degradation_reasons)
        packet.negative_risk_score = min(
            1.0, max(0.0, _safe_float(packet.negative_risk_score))
        )
        packet.catalysts = [
            item
            for item in packet.catalysts
            if item is not None
            and all(
                hasattr(item, name)
                for name in (
                    "event_type", "title", "summary", "source",
                    "source_url", "event_date", "confidence",
                )
            )
        ] if isinstance(packet.catalysts, (list, tuple)) else []
        packet.members = [
            item
            for item in packet.members
            if item is not None
            and all(
                hasattr(item, name)
                for name in (
                    "symbol", "company_id", "company_name", "market_role",
                    "membership_evidence", "market_features",
                )
            )
        ] if isinstance(packet.members, (list, tuple)) else []
        packet.core_members = [
            item
            for item in packet.core_members
            if item is not None
            and all(
                hasattr(item, name)
                for name in (
                    "symbol", "company_id", "company_name", "market_role",
                    "membership_evidence", "market_features",
                )
            )
        ] if isinstance(packet.core_members, (list, tuple)) else []
        for catalyst in packet.catalysts:
            catalyst.event_type = _safe_text(catalyst.event_type).lower()
            catalyst.title = _safe_text(catalyst.title)
            catalyst.summary = _safe_text(catalyst.summary)
            catalyst.source = _safe_text(catalyst.source)
            catalyst.source_url = _safe_text(catalyst.source_url)
            catalyst.event_date = _safe_text(catalyst.event_date)
            catalyst.confidence = _safe_float(catalyst.confidence)
        for member in [*packet.members, *packet.core_members]:
            member.symbol = _safe_text(member.symbol)
            member.company_id = _safe_text(member.company_id)
            member.company_name = _safe_text(member.company_name)
            member.market_role = _safe_text(member.market_role, "secondary")
            member.membership_confidence = _safe_float(member.membership_confidence)
            member.mapping_confidence = _safe_float(member.mapping_confidence)
            member.market_features = _as_mapping(member.market_features)
            evidence = member.membership_evidence
            member.membership_evidence = [
                dict(item) for item in evidence
                if isinstance(item, Mapping)
            ] if isinstance(evidence, (list, tuple)) else []
        packet.market_features = _as_mapping(packet.market_features)
        return packet

    def review(
        self,
        theme_packet: ThemePacket,
        *,
        required_outputs: list[str] | None = None,
        max_new_themes: int = 2,
        as_of: str = "",
    ) -> ThemeReviewResult:
        """执行 dynamic_theme_review。

        Args:
            theme_packet: 输入 ThemePacket v1（来自 QUANT），不会被修改。
            required_outputs: 需要的输出字段列表，None 时使用 DEFAULT_REQUIRED_OUTPUTS。
            max_new_themes: 最多合成新主题数（上限 MAX_NEW_THEMES_LIMIT=2）。
            as_of: PIT 时点，空串使用当前 UTC 时间。

        Returns:
            ThemeReviewResult，含 enhanced_packet (v2) 和 new_theme_suggestions。
        """
        # required_outputs 仅用于校验，当前实现始终产出全部字段（KISS）
        if not isinstance(theme_packet, ThemePacket):
            raise TypeError("theme_packet must be a ThemePacket")
        theme_packet = self._sanitize_packet(theme_packet)
        _ = (
            _text_list(required_outputs)
            if isinstance(required_outputs, (list, tuple))
            else list(DEFAULT_REQUIRED_OUTPUTS)
        )
        effective_max_new = max(
            0,
            min(
                _safe_int(max_new_themes, self.max_new_themes),
                self.MAX_NEW_THEMES_LIMIT,
            ),
        )
        effective_as_of = _safe_text(as_of) or utc_now()

        # 深拷贝 v1，避免修改入参
        enhanced = copy.deepcopy(theme_packet)

        # 1. 验证新闻独立性
        is_valid, independent_sources = self._verify_news_independence(theme_packet)

        # 2. 构建因果链
        causal_chain = self._build_causal_chain(theme_packet)

        # 3. 角色分类（这里只是输入市场角色；严格 Research 角色在下方验证）
        role_classification = self._classify_roles(theme_packet)

        # 4. 反证检索
        counter_evidence = self._search_counter_evidence(
            theme_packet, as_of=effective_as_of
        )

        # 4b. 严格 Research 成员验证。未注入 repository 时维持旧适配行为，
        # 生产 wiring 必须注入 repository 并 fail-closed。
        member_reviews: list[dict[str, Any]] = []
        generated_task_ids: list[str] = []
        if self.repository is not None:
            if counter_evidence["has_blocking_negative"]:
                validated_core = []
                member_reviews = [
                    {
                        "symbol": member.symbol,
                        "status": "blocked_by_negative_evidence",
                    }
                    for member in theme_packet.members
                ]
            else:
                validated_core, member_reviews, generated_task_ids = self._validate_members(
                    theme_packet, as_of=effective_as_of
                )
            enhanced.core_members = validated_core
            core_companies = [member.symbol for member in validated_core]
            if validated_core:
                resolved_reasons = {
                    "no_core_members",
                    "awaiting_research_validation",
                    "market_only_requires_research_catalyst",
                    "no_research_validated_core_members",
                }
                enhanced.degradation_reasons = [
                    reason for reason in enhanced.degradation_reasons
                    if reason not in resolved_reasons
                ]
        else:
            enhanced.core_members = [
                copy.deepcopy(member)
                for member in enhanced.members
                if member.market_role in CORE_ROLES
            ]
            core_companies = role_classification["core"]

        # 5. 新主题合成
        new_theme_suggestions = (
            [] if self.repository is not None else self._synthesize_new_themes(
                theme_packet, effective_max_new, effective_as_of
            )
        )

        # 6. 历史主题检查
        legacy_check = self._check_legacy(theme_packet, effective_as_of)

        # 增强 v2 元数据（写入 source_status，避免修改 ThemePacket 结构）
        enhanced.source_status = {
            **enhanced.source_status,
            ENHANCED_AT_KEY: effective_as_of,
            ENHANCED_BY_KEY: ENHANCED_BY_VALUE,
            "research_gate_status": (
                "rejected"
                if counter_evidence["has_blocking_negative"]
                else "validated" if enhanced.core_members else "pending"
            ),
        }
        enhanced.negative_risk_score = max(
            _safe_float(enhanced.negative_risk_score),
            _safe_float(counter_evidence.get("negative_risk_score")),
        )

        if self.repository is not None and not enhanced.core_members:
            enhanced.degraded = True
            if "no_research_validated_core_members" not in enhanced.degradation_reasons:
                enhanced.degradation_reasons.append("no_research_validated_core_members")

        # 构造 review_summary
        theme_is_valid = bool(
            is_valid and not counter_evidence["has_blocking_negative"]
        )
        review_summary: dict[str, Any] = {
            "theme_validity": {
                "is_valid": theme_is_valid,
                "independent_sources": independent_sources,
                "independent_source_count": len(independent_sources),
            },
            "causal_chain": causal_chain,
            "core_companies": core_companies,
            "counter_evidence": counter_evidence,
            "negative_status": counter_evidence["negative_status"],
            "role_classification": role_classification,
            "member_reviews": member_reviews,
            "generated_task_ids": generated_task_ids,
            "research_gate_status": enhanced.source_status["research_gate_status"],
            "new_theme_count": len(new_theme_suggestions),
        }

        if legacy_check is not None:
            review_summary["legacy_check"] = legacy_check

        return ThemeReviewResult(
            enhanced_packet=enhanced,
            new_theme_suggestions=new_theme_suggestions,
            review_summary=review_summary,
        )

    def process_task(self, task: ResearchTask) -> dict[str, Any]:
        """处理 ResearchTask（task_type=dynamic_theme_review）。

        从 task.params 提取 theme_packet, required_outputs, max_new_themes, as_of，
        调用 review，返回可序列化的字典。

        Args:
            task: ResearchTask 实例，task_type 应为 dynamic_theme_review。

        Returns:
            可序列化的结果字典；任务参数缺失时返回 error 字典而非抛异常。
        """
        if not isinstance(task, ResearchTask):
            raise TypeError("task must be a ResearchTask")
        params = _as_mapping(task.params)
        packet_payload = params.get("theme_packet")
        if not packet_payload:
            return {
                "status": "error",
                "error_code": "MISSING_THEME_PACKET",
                "message": "task.params.theme_packet 缺失",
                "task_id": task.task_id,
            }

        try:
            if isinstance(packet_payload, Mapping):
                theme_packet = ThemePacket.from_dict(dict(packet_payload))
            elif isinstance(packet_payload, ThemePacket):
                theme_packet = packet_payload
            else:
                return {
                    "status": "error",
                    "error_code": "INVALID_THEME_PACKET_TYPE",
                    "message": f"theme_packet 类型非法: {type(packet_payload).__name__}",
                    "task_id": task.task_id,
                }
        except Exception as exc:  # noqa: BLE001 — 防御性：反序列化失败不抛异常
            return {
                "status": "error",
                "error_code": "INVALID_THEME_PACKET",
                "message": f"theme_packet 反序列化失败: {exc}",
                "task_id": task.task_id,
            }

        required_outputs = params.get("required_outputs")
        try:
            max_new_themes = _safe_int(
                params.get("max_new_themes"), self.max_new_themes
            )
        except (TypeError, ValueError):  # 防御性：非法值回退到默认
            max_new_themes = self.max_new_themes
        as_of = _safe_text(params.get("as_of"))

        result = self.review(
            theme_packet,
            required_outputs=required_outputs,
            max_new_themes=max_new_themes,
            as_of=as_of,
        )

        return {
            "status": "ok",
            "task_id": task.task_id,
            "enhanced_packet": result.enhanced_packet.to_dict(),
            "new_theme_suggestions": [p.to_dict() for p in result.new_theme_suggestions],
            "review_summary": result.review_summary,
        }

    # ---- 内部实现 ----

    def _verify_news_independence(
        self, packet: ThemePacket
    ) -> tuple[bool, list[str]]:
        """验证新闻是否真实且独立。

        检查 catalysts 中 news/announcement 类型事件的来源数量，
        拆分合并来源（逗号分隔），≥2 个独立来源视为通过。

        Args:
            packet: 待验证的 ThemePacket。

        Returns:
            (is_valid, independent_sources) 元组，sources 按字典序排序。
        """
        sources: set[str] = set()
        for catalyst in packet.catalysts:
            if _safe_text(catalyst.event_type).lower() in {"news", "announcement"}:
                # 拆分合并来源（逗号分隔）
                for src in str(catalyst.source).split(","):
                    src = src.strip()
                    if src:
                        sources.add(src)
        return len(sources) >= 2, sorted(sources)

    def _build_causal_chain(self, packet: ThemePacket) -> list[str]:
        """基于 catalysts 和 members 构建事件→产业链因果描述。

        每个 catalyst.summary 非空的事件生成一条因果描述，
        受影响成员取 core_members 前 3 个公司名。

        Args:
            packet: ThemePacket。

        Returns:
            因果链描述列表，每条形如「事件「X」→ 影响 A, B, C」。
        """
        chains: list[str] = []
        affected_members = [
            member.company_name
            for member in packet.core_members[:3]
            if member.company_name
        ]
        affected_text = ", ".join(affected_members) if affected_members else "相关产业链"
        for catalyst in packet.catalysts:
            if not catalyst.summary:
                continue
            chains.append(f"事件「{catalyst.title}」→ 影响 {affected_text}")
        return chains

    def _classify_roles(self, packet: ThemePacket) -> dict[str, list[str]]:
        """按 market_role 分类成员。

        Args:
            packet: ThemePacket。

        Returns:
            {"core": [...], "secondary": [...], "diffusion": [...]} 符号列表。
        """
        return {
            "core": [
                member.symbol
                for member in packet.members
                if member.market_role in CORE_ROLES
            ],
            "secondary": [
                member.symbol
                for member in packet.members
                if member.market_role == "secondary"
            ],
            "diffusion": [
                member.symbol
                for member in packet.members
                if member.market_role == "diffusion"
            ],
        }

    @staticmethod
    def _normalized(value: Any) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", str(value or "").lower())

    def _claim_matches_member(self, claim: Claim, packet: ThemePacket, member: Any) -> bool:
        subject_keys = {
            self._normalized(member.symbol),
            self._normalized(member.company_name),
            self._normalized(self.company_name_by_symbol.get(member.symbol, "")),
        }
        claim_subjects = {
            self._normalized(claim.subject_id), self._normalized(claim.subject_name),
        }
        theme_keys = {
            self._normalized(packet.theme_id), self._normalized(packet.theme_name),
            *(self._normalized(alias) for alias in packet.aliases),
        }
        claim_objects = {
            self._normalized(claim.object_id), self._normalized(claim.object_name),
            self._normalized(_as_mapping(claim.attributes).get("theme_id")),
            self._normalized(_as_mapping(claim.attributes).get("theme_name")),
        }
        return bool((subject_keys - {""}) & (claim_subjects - {""})) and bool(
            (theme_keys - {""}) & (claim_objects - {""})
        )

    def _validate_members(
        self, packet: ThemePacket, *, as_of: str
    ) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
        """Validate theme members through the unified Claim/Evidence policy."""
        try:
            claims = [
                item
                for item in (self.repository.list_claims(limit=5000) or [])
                if isinstance(item, Claim)
            ]
        except Exception as exc:
            logger.warning("读取主题 Claim 失败: %s", exc)
            return [], [{"status": "repository_error", "error": str(exc)}], []

        ranked_members = sorted(
            packet.members,
            key=lambda member: (
                -_safe_float(
                    _as_mapping(getattr(member, "market_features", {})).get(
                        "leadership_score"
                    )
                ),
                -_safe_float(getattr(member, "membership_confidence", 0.0)),
                _safe_text(getattr(member, "symbol", "")),
            ),
        )
        selected_for_research = {
            member.symbol for member in ranked_members[: self.max_claims_per_review]
        }
        validated_core: list[Any] = []
        reviews: list[dict[str, Any]] = []
        task_ids: list[str] = []

        for member in ranked_members:
            member_features = _as_mapping(member.market_features)
            matching = [
                claim for claim in claims
                if self._claim_matches_member(claim, packet, member)
            ]
            if member.symbol in selected_for_research:
                try:
                    claim = self._ensure_member_claim(
                        packet, member, matching, as_of=as_of
                    )
                except Exception as exc:
                    logger.warning("主题成员 Claim 处理失败(%s): %s", member.symbol, exc)
                    claim = None
                if claim is not None and all(item.claim_id != claim.claim_id for item in matching):
                    matching.append(claim)
                    claims.append(claim)

            accepted_claim: Claim | None = None
            accepted_decision: Any | None = None
            accepted_evidence: list[EvidenceRecord] = []
            claim_summaries: list[dict[str, Any]] = []
            for claim in matching:
                try:
                    evidence = [
                        item
                        for item in (
                            self.repository.list_evidence(claim.claim_id, limit=1000)
                            or []
                        )
                        if isinstance(item, EvidenceRecord)
                    ]
                except Exception as exc:
                    logger.warning(
                        "读取 Claim 证据失败(%s): %s", claim.claim_id, exc
                    )
                    evidence = []
                try:
                    decision = self.policy.evaluate(
                        claim, evidence, as_of=as_of, historical=False
                    )
                except Exception as exc:
                    logger.warning("Claim policy evaluation failed(%s): %s", claim.claim_id, exc)
                    claim_summaries.append({
                        "claim_id": claim.claim_id,
                        "claim_status": claim.research_status,
                        "status": "policy_error",
                        "error": str(exc),
                    })
                    continue
                claim_summaries.append({
                    "claim_id": claim.claim_id,
                    "claim_status": claim.research_status,
                    "policy_target_status": decision.target_status,
                    "support_count": decision.support_count,
                    "counter_count": decision.counter_count,
                    "independent_sources": decision.independent_sources,
                    "first_party_sources": decision.first_party_sources,
                    "slots_complete": decision.slots_complete,
                    "reasons": decision.reasons,
                })
                if (
                    str(claim.research_status).upper() in {
                        ClaimStatus.ACTIVE.value, ClaimStatus.TRUSTED.value,
                    }
                    and decision.target_status == ClaimStatus.ACTIVE.value
                    and claim.conflict_status in {"", "none", "resolved"}
                    and _safe_float(claim.mapping_confidence) >= 0.6
                ):
                    accepted_claim = claim
                    accepted_decision = decision
                    accepted_evidence = evidence
                    break

            if accepted_claim is not None:
                enhanced_member = copy.deepcopy(member)
                enhanced_member.company_name = (
                    enhanced_member.company_name
                    or accepted_claim.subject_name
                    or self.company_name_by_symbol.get(member.symbol, "")
                )
                enhanced_member.company_id = accepted_claim.subject_id
                enhanced_member.research_claim_ids = [accepted_claim.claim_id]
                enhanced_member.research_claim_status = accepted_claim.research_status
                enhanced_member.mapping_confidence = _safe_float(
                    accepted_claim.mapping_confidence
                )
                visible_times = sorted(
                    str(item.retrieved_at or item.published_at)
                    for item in accepted_evidence
                    if item.retrieved_at or item.published_at
                )
                enhanced_member.evidence_as_of = visible_times[-1] if visible_times else ""
                enhanced_member.research_validated_at = as_of
                attributes = _as_mapping(accepted_claim.attributes)
                declared_role = str(attributes.get("market_role") or "").lower()
                industry_core_score = _safe_float(
                    attributes.get("industry_core_score")
                )
                leader_candidate = (
                    member_features.get("market_role_candidate") == "leader_candidate"
                )
                if declared_role == "core" or industry_core_score >= 0.7:
                    enhanced_member.market_role = "core"
                elif declared_role == "leader" or leader_candidate:
                    enhanced_member.market_role = "leader"
                else:
                    enhanced_member.market_role = "secondary"
                if enhanced_member.market_role in CORE_ROLES:
                    validated_core.append(enhanced_member)
                reviews.append({
                    "symbol": member.symbol,
                    "status": "validated",
                    "claim_id": accepted_claim.claim_id,
                    "claim_status": accepted_claim.research_status,
                    "market_role": enhanced_member.market_role,
                    "policy": accepted_decision.__dict__,
                })
                continue

            generated_task_id = ""
            if member.symbol in selected_for_research and matching:
                generated_task_id = self._ensure_research_task(
                    packet, member, matching[0], as_of=as_of
                )
                if generated_task_id:
                    task_ids.append(generated_task_id)
            reviews.append({
                "symbol": member.symbol,
                "status": "pending_evidence" if matching else "unmapped",
                "claim_summaries": claim_summaries,
                "task_id": generated_task_id,
                "technical_state": member_features.get(
                    "technical_state", "UNKNOWN"
                ),
                "leadership_score": member_features.get(
                    "leadership_score", 0.0
                ),
            })

        return validated_core, reviews, list(dict.fromkeys(task_ids))

    def _ensure_member_claim(
        self,
        packet: ThemePacket,
        member: Any,
        matching: list[Claim],
        *,
        as_of: str,
    ) -> Claim | None:
        member_features = _as_mapping(getattr(member, "market_features", {}))
        claim = matching[0] if matching else None
        if claim is None:
            raw = f"{packet.theme_id}|{member.symbol}|member_of_theme"
            claim = Claim(
                claim_id="claim_theme_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16],
                subject_id=member.symbol,
                subject_name=(
                    member.company_name
                    or self.company_name_by_symbol.get(member.symbol, "")
                    or member.symbol
                ),
                relation="member_of_theme",
                object_id=packet.theme_id,
                object_name=packet.theme_name,
                research_status=ClaimStatus.DISCOVERED.value,
                mapping_confidence=1.0 if member.symbol else 0.0,
                required_slots=["identity", "relationship"],
                support_terms=[
                    " ".join(filter(None, [
                        self.company_name_by_symbol.get(member.symbol, member.symbol),
                        packet.theme_name,
                    ]))
                ],
                counter_terms=[
                    "不涉及", "无相关业务", "否认", "澄清", "终止", "撤回",
                ],
                high_impact=True,
                attributes={
                    "theme_id": packet.theme_id,
                    "theme_name": packet.theme_name,
                    "importance": (
                        "high" if member_features.get("market_role_candidate")
                        == "leader_candidate" else "medium"
                    ),
                    "discovery_theme_run_id": packet.theme_run_id,
                    "market_features": member_features,
                    "object_aliases": list(dict.fromkeys([
                        *packet.aliases,
                        *self.THEME_QUERY_EXPANSIONS.get(packet.theme_name, []),
                    ])),
                },
            )
            try:
                self.repository.save_claim(claim)
            except Exception as exc:
                logger.warning("创建主题 Claim 失败(%s): %s", member.symbol, exc)
                return None

        self._ingest_structured_membership(packet, member, claim, as_of=as_of)
        attributes = _as_mapping(claim.attributes)
        existing_aliases = _text_list(attributes.get("object_aliases"))
        aliases = list(dict.fromkeys([
            *existing_aliases,
            *packet.aliases,
            *self.THEME_QUERY_EXPANSIONS.get(packet.theme_name, []),
        ]))
        if aliases != existing_aliases:
            claim.attributes = {**attributes, "object_aliases": aliases}
            claim.high_impact = True
            try:
                self.repository.save_claim(claim)
            except Exception as exc:
                logger.warning("保存主题别名失败(%s): %s", claim.claim_id, exc)
        precise_term = " ".join(filter(None, [
            claim.subject_name,
            "".join(ch for ch in str(claim.subject_id) if ch.isdigit())[:6],
            packet.theme_name,
        ]))
        if precise_term and precise_term not in claim.support_terms:
            claim.support_terms.insert(0, precise_term)
            try:
                self.repository.save_claim(claim)
            except Exception as exc:
                logger.warning("保存主题检索词失败(%s): %s", claim.claim_id, exc)
        try:
            evidence = [
                item
                for item in (
                    self.repository.list_evidence(claim.claim_id, limit=1000) or []
                )
                if isinstance(item, EvidenceRecord)
            ]
        except Exception as exc:
            logger.warning("读取主题 Claim 证据失败(%s): %s", claim.claim_id, exc)
            evidence = []
        decision = self.policy.evaluate(claim, evidence, as_of=as_of, historical=False)
        forward = {
            ClaimStatus.DISCOVERED.value: 0,
            ClaimStatus.CANDIDATE.value: 1,
            ClaimStatus.SUPPORTED.value: 2,
            ClaimStatus.ACTIVE.value: 3,
            ClaimStatus.TRUSTED.value: 4,
        }
        current_rank = forward.get(str(claim.research_status).upper(), -1)
        target_rank = forward.get(str(decision.target_status).upper(), -1)
        if target_rank > current_rank:
            claim.evidence_confidence = decision.confidence
            claim.research_status = decision.target_status
            try:
                self.repository.save_claim(claim)
            except Exception as exc:
                logger.warning("更新主题 Claim 状态失败(%s): %s", claim.claim_id, exc)
        return claim

    def _ingest_structured_membership(
        self, packet: ThemePacket, member: Any, claim: Claim, *, as_of: str
    ) -> None:
        for item in list(member.membership_evidence or []):
            provider = _safe_text(item.get("provider")).lower()
            if provider not in {"wencai", "eastmoney", "ths"}:
                continue
            observed_at = _safe_text(item.get("fetched_at")) or as_of or utc_now()
            raw_hash = _safe_text(item.get("raw_response_hash"))
            lineage_raw = f"{provider}|{packet.theme_name}|{raw_hash}"
            lineage_id = hashlib.sha1(lineage_raw.encode("utf-8")).hexdigest()[:20]
            source_trace_id = "src_theme_" + lineage_id[:16]
            source = SourceTrace(
                source_trace_id=source_trace_id,
                original_source=f"{provider}:concept_membership",
                lineage_id=lineage_id,
                provider=provider,
                source_type="structured_concept_membership",
                availability_status="ok",
                retrieved_at=observed_at,
                metadata={
                    "theme_name": packet.theme_name,
                    "source_snapshot_id": packet.source_snapshot_id,
                    "raw_response_hash": raw_hash,
                    "members_truncated": _safe_bool(item.get("members_truncated")),
                },
            )
            quote = f"{member.symbol} 所属概念 {packet.theme_name}"
            document_id = "doc_theme_" + hashlib.sha1(
                f"{claim.claim_id}|{provider}|{raw_hash}".encode("utf-8")
            ).hexdigest()[:16]
            document = DocumentVersion(
                document_version_id="dv_" + document_id.removeprefix("doc_"),
                document_id=document_id,
                title=f"{provider} 概念成员结构化结果",
                content_text=quote,
                source_trace_id=source_trace_id,
                retrieved_at=observed_at,
                content_available=True,
                parse_status="ok",
                metadata={"structured_provider": provider},
            )
            evidence = EvidenceRecord(
                evidence_id="ev_theme_" + hashlib.sha1(
                    f"{claim.claim_id}|{provider}|{raw_hash}|{member.symbol}".encode("utf-8")
                ).hexdigest()[:16],
                claim_id=claim.claim_id,
                document_version_id=document.document_version_id,
                source_trace_id=source_trace_id,
                lineage_id=lineage_id,
                stance="support",
                evidence_kind="structured_provider",
                quote=quote,
                locator={"field": "所属概念", "symbol": member.symbol},
                source_quality=0.6 if provider == "wencai" else 0.55,
                match_score=1.0,
                retrieved_at=observed_at,
                extraction_method="structured_provider_adapter",
                grounding_status="structured",
                is_first_party=False,
                metadata={
                    "slots": ["identity", "relationship"],
                    "theme_id": packet.theme_id,
                    "source_snapshot_id": packet.source_snapshot_id,
                },
            )
            try:
                self.repository.save_source_trace(source)
                self.repository.save_document(document)
                saved = self.repository.save_evidence(evidence)
                if saved is False:
                    logger.warning("结构化主题证据未保存(%s)", member.symbol)
            except Exception as exc:
                logger.warning("保存结构化主题证据失败(%s): %s", member.symbol, exc)

    def _ensure_research_task(
        self, packet: ThemePacket, member: Any, claim: Claim, *, as_of: str
    ) -> str:
        # The task key contains the day and evidence gap, but ownership is by
        # Claim.  Reuse an active owner before deriving a new daily key.
        active_task_id_to_reuse = ""
        try:
            from .task_resumer import TaskResumer

            active_task_id = TaskResumer(self.repository).active_task_id_for_claim(
                claim.claim_id
            )
        except Exception as exc:
            logger.warning("检查 Claim 活跃任务失败(%s): %s", claim.claim_id, exc)
            active_task_id = ""
        if active_task_id:
            active_task = self.repository.get_task(active_task_id)
            if active_task is None:
                active_task_id = ""
            else:
                active_version = _safe_text(
                    _as_mapping(active_task.params).get("retrieval_version")
                )
                if not active_version or active_version == self.RETRIEVAL_VERSION:
                    active_task_id_to_reuse = active_task_id
                else:
                    active_task.params = {
                        **_as_mapping(active_task.params),
                        "retrieval_version": self.RETRIEVAL_VERSION,
                    }
                    active_task.retry_count = 0
                    active_task.status = TaskStatus.QUEUED.value
                    active_task.finished_at = ""
                    active_task.failure = {
                        "code": "RETRIEVAL_ADAPTER_UPGRADED",
                        "message": "retrieval adapter upgraded; task requeued",
                    }
                    active_task.progress = {"percent": 0, "message": "等待新版补证"}
                    self.repository.save_task(active_task)
                    return active_task.task_id
        day = (_safe_text(as_of) or utc_now())[:10]
        try:
            existing_evidence = [
                item
                for item in (
                    self.repository.list_evidence(claim.claim_id, limit=1000) or []
                )
                if isinstance(item, EvidenceRecord)
            ]
        except Exception as exc:
            logger.warning("读取任务前置证据失败(%s): %s", claim.claim_id, exc)
            existing_evidence = []
        needs_first_party = bool(
            claim.high_impact and not any(item.is_first_party for item in existing_evidence)
        )
        gap_key = "first_party" if needs_first_party else "general"
        task_id = "rt_theme_" + hashlib.sha1(
            f"{claim.claim_id}|{day}|{gap_key}".encode("utf-8")
        ).hexdigest()[:16]
        existing = self.repository.get_task(task_id)
        if existing is not None:
            if _safe_text(
                _as_mapping(existing.params).get("retrieval_version")
            ) != self.RETRIEVAL_VERSION:
                existing.params = {
                    **_as_mapping(existing.params),
                    "retrieval_version": self.RETRIEVAL_VERSION,
                }
                existing.retry_count = 0
                existing.status = TaskStatus.QUEUED.value
                existing.finished_at = ""
                existing.failure = {
                    "code": "RETRIEVAL_ADAPTER_UPGRADED",
                    "message": "官方文档原文定位适配器升级，重新执行补证",
                }
                existing.progress = {"percent": 0, "message": "等待新版补证"}
                self.repository.save_task(existing)
                return existing.task_id
            if existing.status in {
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.RETRYING.value,
                TaskStatus.SUCCEEDED.value,
            }:
                return existing.task_id
            if (
                existing.status in {TaskStatus.PARTIAL.value, TaskStatus.FAILED.value}
                and existing.retry_count < existing.max_retries
            ):
                existing.retry_count += 1
                existing.status = TaskStatus.QUEUED.value
                existing.finished_at = ""
                existing.failure = {
                    "code": "EVIDENCE_STILL_INSUFFICIENT",
                    "message": "按更新后的实体/关系 grounding 规则重试",
                }
                existing.progress = {"percent": 0, "message": "等待补证重试"}
                self.repository.save_task(existing)
                return existing.task_id
            return existing.task_id
        if active_task_id_to_reuse:
            return active_task_id_to_reuse
        task = ResearchTask(
            task_id=task_id,
            task_type="evidence_completion",
            trigger_source="quant_dynamic_theme",
            target_claim_id=claim.claim_id,
            priority=max(
                0.5,
                _safe_float(
                    _as_mapping(member.market_features).get("leadership_score")
                ),
            ),
            trace_id=packet.trace_id or "",
            budget={
                "max_requests": 8,
                "max_seconds": 60.0,
                "max_tokens": 8000,
                "max_cost": 2.0,
                "max_documents": 16,
            },
            params={
                "theme_id": packet.theme_id,
                "theme_run_id": packet.theme_run_id,
                "theme_name": packet.theme_name,
                "workflow": "dynamic_theme_review",
                "symbol": member.symbol,
                "company_name": self.company_name_by_symbol.get(member.symbol, ""),
                "as_of": as_of,
                "required_outputs": [
                    "relationship_evidence", "counter_evidence", "source_independence",
                ],
                "required_source_type": "first_party" if needs_first_party else "independent",
                "retrieval_version": self.RETRIEVAL_VERSION,
            },
        )
        self.repository.save_task(task)
        return task.task_id

    def _search_counter_evidence(
        self, packet: ThemePacket, *, as_of: str = ""
    ) -> dict[str, Any]:
        """检索反证和重大负面信息。

        只把实际催化剂/本地 Evidence/已执行新闻检索中的显式负面语义
        计为反证；没有执行检索时返回 unknown，绝不伪装为 clear。

        Args:
            packet: ThemePacket。

        Returns:
            含 has_blocking_negative/counter_evidence_count/negative_status 的字典。
        """
        negative_risk = min(
            1.0, max(0.0, _safe_float(packet.negative_risk_score))
        )
        blocking_terms = (
            "立案调查", "行政处罚", "误导投资者", "财务造假", "重大违法",
            "退市", "不存在相关业务", "无相关业务", "终止", "撤回",
            "被罚", "罚单", "误导性陈述",
        )
        warning_terms = (
            "澄清", "风险提示", "减持", "亏损", "否认", "不涉及", "尚未",
            "不确定", "涉嫌", "误导",
        )
        items: list[dict[str, Any]] = []
        errors: list[str] = []

        def inspect(
            title: str, summary: str, *, source: str = "", url: str = "", event_date: str = ""
        ) -> None:
            text = f"{title} {summary}"
            matched_blocking = [term for term in blocking_terms if term in text]
            matched_warning = [term for term in warning_terms if term in text]
            if not matched_blocking and not matched_warning:
                return
            items.append({
                "title": title,
                "summary": summary[:300],
                "source": source,
                "source_url": url,
                "event_date": event_date,
                "severity": "blocking" if matched_blocking else "warning",
                "matched_terms": matched_blocking or matched_warning,
            })

        for catalyst in packet.catalysts:
            inspect(
                catalyst.title, catalyst.summary, source=catalyst.source,
                url=catalyst.source_url, event_date=catalyst.event_date,
            )

        search_executed = False
        if callable(self.news_loader):
            try:
                raw_news = self.news_loader()
                if isinstance(raw_news, Mapping):
                    news_items = [raw_news]
                elif isinstance(raw_news, (list, tuple)):
                    news_items = list(raw_news)
                else:
                    news_items = []
                search_executed = True
                theme_key = self._normalized(packet.theme_name)
                for raw in news_items:
                    if not isinstance(raw, Mapping):
                        continue
                    title = str(raw.get("title") or raw.get("新闻标题") or raw.get("标题") or "")
                    summary = str(
                        raw.get("content") or raw.get("summary") or raw.get("digest")
                        or raw.get("description") or raw.get("摘要") or ""
                    )
                    if theme_key and theme_key not in self._normalized(f"{title} {summary}"):
                        continue
                    inspect(
                        title, summary,
                        source=str(raw.get("source") or raw.get("provider") or ""),
                        url=str(raw.get("url") or raw.get("link") or ""),
                        event_date=str(
                            raw.get("date") or raw.get("published_at") or raw.get("time")
                            or raw.get("showTime") or ""
                        ),
                    )
            except Exception as exc:
                errors.append(str(exc))

        if self.repository is not None:
            try:
                for claim in self.repository.list_claims(limit=5000) or []:
                    if not isinstance(claim, Claim):
                        continue
                    theme_values = {
                        self._normalized(claim.object_id),
                        self._normalized(claim.object_name),
                        self._normalized(
                            _as_mapping(claim.attributes).get("theme_id")
                        ),
                    }
                    if self._normalized(packet.theme_id) not in theme_values and self._normalized(packet.theme_name) not in theme_values:
                        continue
                    if claim.counter_search_executed:
                        search_executed = True
                    try:
                        evidence_rows = self.repository.list_evidence(
                            claim.claim_id, limit=1000
                        ) or []
                    except Exception as exc:
                        errors.append(str(exc))
                        continue
                    for evidence in evidence_rows:
                        if not isinstance(evidence, EvidenceRecord):
                            continue
                        if (
                            evidence.stance != "counter"
                            or not evidence.can_affect_confidence
                            or not evidence_is_current(evidence, as_of)
                            or not evidence_visible_as_of(evidence, as_of, historical=False)
                        ):
                            continue
                        items.append({
                            "title": f"本地反证 {evidence.evidence_id}",
                            "summary": evidence.quote[:300],
                            "source": evidence.source_trace_id,
                            "source_url": str((evidence.locator or {}).get("url") or ""),
                            "event_date": evidence.published_at or evidence.retrieved_at,
                            # A member-level denial is handled by that Claim's
                            # state policy. It must not invalidate every other
                            # company in the theme.
                            "severity": "member_only",
                            "matched_terms": ["structured_counter_evidence"],
                            "claim_id": claim.claim_id,
                        })
            except Exception as exc:
                errors.append(str(exc))

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            key = hashlib.sha1(
                f"{item.get('source_url')}|{item.get('title')}|{item.get('summary')}".encode("utf-8")
            ).hexdigest()
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        is_blocking = any(item.get("severity") == "blocking" for item in deduped)
        is_warning = any(
            item.get("severity") in {"blocking", "warning"} for item in deduped
        ) or negative_risk >= WARNING_NEGATIVE_THRESHOLD
        blocking_flag = bool(
            is_blocking or negative_risk >= BLOCKING_NEGATIVE_THRESHOLD
        )
        if blocking_flag:
            status = "blocking"
            risk_score = max(0.85, negative_risk)
        elif is_warning:
            status = "warning"
            risk_score = max(0.5, negative_risk)
        elif search_executed or (self.repository is None and self.news_loader is None):
            # Legacy adapter compatibility only.  Production wiring injects
            # the repository/news loader and therefore never reaches this
            # branch without an executed search.
            status = "clear"
            risk_score = 0.0
        else:
            status = "unknown"
            risk_score = negative_risk
        return {
            "has_blocking_negative": blocking_flag,
            "counter_evidence_count": len(deduped),
            "negative_status": status,
            "negative_risk_score": round(risk_score, 4),
            "search_executed": search_executed,
            "items": deduped,
            "errors": errors,
            "as_of": as_of,
        }

    def _synthesize_new_themes(
        self, packet: ThemePacket, max_new_themes: int, as_of: str
    ) -> list[ThemePacket]:
        """从 catalysts 提取事件实体合成新主题。

        每个独立的 catalyst.source 生成一个新主题候选。
        限制：最多 max_new_themes 个（且不超过 MAX_NEW_THEMES_LIMIT=2）。

        Args:
            packet: 源 ThemePacket。
            max_new_themes: 最多合成新主题数。
            as_of: PIT 时点。

        Returns:
            新主题 ThemePacket 列表，theme_type=research_discovery。
        """
        effective_limit = max(
            0,
            min(
                _safe_int(max_new_themes, self.max_new_themes),
                self.MAX_NEW_THEMES_LIMIT,
            ),
        )
        if effective_limit <= 0:
            return []

        # 收集独立来源（去重，保持首次出现顺序）
        seen_sources: set[str] = set()
        candidate_sources: list[str] = []
        for catalyst in packet.catalysts:
            src = str(catalyst.source).strip()
            if not src or src in seen_sources:
                continue
            seen_sources.add(src)
            candidate_sources.append(src)

        # 限制数量（双重限制：参数 + MAX_NEW_THEMES_LIMIT）
        candidate_sources = candidate_sources[:effective_limit]

        new_themes: list[ThemePacket] = []
        for source in candidate_sources:
            # 找到该来源对应的 catalysts
            source_catalysts = [
                catalyst
                for catalyst in packet.catalysts
                if str(catalyst.source).strip() == source
            ]
            if not source_catalysts:
                continue

            # 提取事件实体（使用首个 catalyst 的 title 作为主题关键词）
            theme_keywords = source_catalysts[0].title or source
            event_entities = source

            # 提取核心股票（仅 core/leader 角色，用于生成 theme_id）
            core_symbols = [
                member.symbol
                for member in packet.members
                if member.market_role in CORE_ROLES and member.symbol
            ]

            # 生成新 theme_id 与 theme_run_id
            new_theme_id = generate_theme_id(
                theme_keywords, event_entities, core_symbols
            )
            new_run_id = generate_theme_run_id(new_theme_id, as_of)

            # 深拷贝相关 catalysts 和 members，避免共享引用
            new_catalysts = [copy.deepcopy(c) for c in source_catalysts]
            new_members = [copy.deepcopy(m) for m in packet.members]
            new_core_members = [
                copy.deepcopy(member)
                for member in packet.members
                if member.market_role in CORE_ROLES
            ]

            new_packet = ThemePacket(
                theme_run_id=new_run_id,
                theme_id=new_theme_id,
                theme_name=theme_keywords,
                theme_type="research_discovery",
                detected_at=as_of,
                as_of=as_of,
                catalysts=new_catalysts,
                members=new_members,
                core_members=new_core_members,
                trace_id=packet.trace_id,
                strategy_version=packet.strategy_version,
            )
            new_themes.append(new_packet)

        return new_themes

    def _check_legacy(
        self, packet: ThemePacket, as_of: str
    ) -> dict[str, Any] | None:
        """检查历史主题是否重新出现研究价值。

        如果 legacy_detector 注入，调用 detect；否则返回 None。

        Args:
            packet: 当前 ThemePacket。
            as_of: PIT 时点。

        Returns:
            含 detected/reentry_packet 的字典，或 None（未注入检测器）。
        """
        if self.legacy_detector is None:
            return None
        try:
            reentry = self.legacy_detector.detect(
                current_packet=packet, as_of=as_of
            )
        except Exception as exc:  # noqa: BLE001 — 防御性：检测器异常不阻断 review
            logger.warning("legacy_detector.detect failed: %s", exc)
            return {
                "detected": False,
                "reentry_packet": None,
                "error": str(exc),
            }
        if reentry is None:
            return {"detected": False, "reentry_packet": None}
        try:
            return {"detected": True, "reentry_packet": reentry.to_dict()}
        except Exception as exc:
            logger.warning("legacy_detector result serialization failed: %s", exc)
            return {
                "detected": False,
                "reentry_packet": None,
                "error": str(exc),
            }
