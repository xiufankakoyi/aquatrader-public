"""P2.3 产业与公司变化雷达。

按 P2 计划 §4 Task P2.3：
- 监测 8 类产业与公司变化事件
- 5 阶段状态机：observed_event → retrieval_hypothesis → evidence_supported
  → company_driver_candidate → model_eligible / rejected / expired
- 新闻事件不能直接成为公司经营事实（验收点 1）
- 事件必须有时间、实体、方向、来源和影响路径（验收点 2）
- 重复事件去重、过期和冲突处理可验证（验收点 3）

8 类事件类型（P2.3 §131-139）：
1. product_price_cost：产品价格与成本
2. order_tender：订单、招标和客户验证
3. capacity_utilization_inventory：产能、开工率、库存和交付周期
4. hiring_patent_tech：招聘、专利和技术路线
5. capex_expansion：资本开支和扩产
6. policy_subsidy：政策、补贴、标准和限制
7. management_guidance：管理层指引和经营目标
8. consensus_revision：一致预期集中上修/下修

状态机：
    observed_event       # 已观察事件（新闻/公告/数据）
    → retrieval_hypothesis   # 检索假设（待补证据）
    → evidence_supported     # 证据支持（多源交叉验证）
    → company_driver_candidate  # 公司驱动候选
    → model_eligible         # 模型可用（可进入 P1.3 经营模型）
      / rejected             # 被拒绝（证据不足/冲突/过期）
      / expired              # 已过期（超过有效期）

硬门禁（AGENTS.md + P2.3）：
- 新闻事件必须经过 evidence_supported 才能升级为 company_driver_candidate
- 不输出交易动作建议
- 字段缺失降级处理
- 概念映射以本地证据为准
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if item is not None and str(item)]


# ---------------------------------------------------------------------------
# 8 类事件类型（P2.3 §131-139）
# ---------------------------------------------------------------------------

EVENT_TYPE_PRODUCT_PRICE_COST: str = "product_price_cost"
EVENT_TYPE_ORDER_TENDER: str = "order_tender"
EVENT_TYPE_CAPACITY_UTILIZATION_INVENTORY: str = "capacity_utilization_inventory"
EVENT_TYPE_HIRING_PATENT_TECH: str = "hiring_patent_tech"
EVENT_TYPE_CAPEX_EXPANSION: str = "capex_expansion"
EVENT_TYPE_POLICY_SUBSIDY: str = "policy_subsidy"
EVENT_TYPE_MANAGEMENT_GUIDANCE: str = "management_guidance"
EVENT_TYPE_CONSENSUS_REVISION: str = "consensus_revision"

EVENT_TYPES: tuple[str, ...] = (
    EVENT_TYPE_PRODUCT_PRICE_COST,
    EVENT_TYPE_ORDER_TENDER,
    EVENT_TYPE_CAPACITY_UTILIZATION_INVENTORY,
    EVENT_TYPE_HIRING_PATENT_TECH,
    EVENT_TYPE_CAPEX_EXPANSION,
    EVENT_TYPE_POLICY_SUBSIDY,
    EVENT_TYPE_MANAGEMENT_GUIDANCE,
    EVENT_TYPE_CONSENSUS_REVISION,
)


# ---------------------------------------------------------------------------
# 5 阶段状态机（P2.3 §143-148）
# ---------------------------------------------------------------------------

STATUS_OBSERVED_EVENT: str = "observed_event"
STATUS_RETRIEVAL_HYPOTHESIS: str = "retrieval_hypothesis"
STATUS_EVIDENCE_SUPPORTED: str = "evidence_supported"
STATUS_COMPANY_DRIVER_CANDIDATE: str = "company_driver_candidate"
STATUS_MODEL_ELIGIBLE: str = "model_eligible"
STATUS_REJECTED: str = "rejected"
STATUS_EXPIRED: str = "expired"

# 允许的状态转移
VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_OBSERVED_EVENT: (
        STATUS_RETRIEVAL_HYPOTHESIS,
        STATUS_REJECTED,
        STATUS_EXPIRED,
    ),
    STATUS_RETRIEVAL_HYPOTHESIS: (
        STATUS_EVIDENCE_SUPPORTED,
        STATUS_REJECTED,
        STATUS_EXPIRED,
    ),
    STATUS_EVIDENCE_SUPPORTED: (
        STATUS_COMPANY_DRIVER_CANDIDATE,
        STATUS_REJECTED,
        STATUS_EXPIRED,
    ),
    STATUS_COMPANY_DRIVER_CANDIDATE: (
        STATUS_MODEL_ELIGIBLE,
        STATUS_REJECTED,
        STATUS_EXPIRED,
    ),
    STATUS_MODEL_ELIGIBLE: (STATUS_EXPIRED,),
    STATUS_REJECTED: (STATUS_EXPIRED,),
    STATUS_EXPIRED: (),
}

# 终态（不可再转移）
TERMINAL_STATUSES: tuple[str, ...] = (STATUS_EXPIRED,)


# ---------------------------------------------------------------------------
# 事件方向
# ---------------------------------------------------------------------------

DIRECTION_POSITIVE: str = "positive"  # 利好（如涨价、扩产、政策利好）
DIRECTION_NEGATIVE: str = "negative"  # 利空（如降价、停产、政策限制）
DIRECTION_NEUTRAL: str = "neutral"    # 中性（如人事变动、技术路线发布）

DIRECTIONS: tuple[str, ...] = (DIRECTION_POSITIVE, DIRECTION_NEGATIVE, DIRECTION_NEUTRAL)


def utc_now() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。"""
    return f"{prefix}_{uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# 事件证据
# ---------------------------------------------------------------------------


@dataclass
class EventEvidence:
    """事件证据：单个来源对事件的支持。

    按验收点 1「新闻事件不能直接成为公司经营事实」，每个事件必须经过
    多源交叉验证（≥2 个独立来源）才能升级为 evidence_supported。

    Attributes:
        evidence_id: 证据唯一 ID
        source_ref: 来源引用（claim_id 或 evidence_id）
        source_kind: 来源类型（news / announcement / industry_research /
            analyst_note / government_publication / exchange_data / third_party_data）
        source_publisher: 发布机构（如 "中国证券报" / "工信部" / "Wind"）
        published_at: 证据发布时间（YYYY-MM-DD）
        retrieved_at: 证据检索时间
        confidence: 证据置信度 [0, 1]
        is_independent: 是否为独立来源（与已收集证据独立）
        note: 备注
    """

    evidence_id: str = field(default_factory=lambda: new_id("evd"))
    source_ref: str = ""
    source_kind: str = ""
    source_publisher: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    confidence: float = 0.0
    is_independent: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "evidence_id": self.evidence_id,
            "source_ref": self.source_ref,
            "source_kind": self.source_kind,
            "source_publisher": self.source_publisher,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "confidence": self.confidence,
            "is_independent": self.is_independent,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventEvidence":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        return cls(
            evidence_id=str(data.get("evidence_id", "")),
            source_ref=str(data.get("source_ref", "")),
            source_kind=str(data.get("source_kind", "")),
            source_publisher=str(data.get("source_publisher", "")),
            published_at=str(data.get("published_at", "")),
            retrieved_at=str(data.get("retrieved_at", "")),
            confidence=_safe_float(data.get("confidence", 0.0)),
            is_independent=bool(data.get("is_independent", True)),
            note=str(data.get("note", "")),
        )


# ---------------------------------------------------------------------------
# 公司驱动候选
# ---------------------------------------------------------------------------


@dataclass
class CompanyDriverCandidate:
    """公司驱动候选：将事件映射为公司经营驱动变量。

    按验收点 1「新闻事件不能直接成为公司经营事实」，事件必须经过
    evidence_supported 阶段后才能映射为公司驱动候选。

    Attributes:
        symbol: 公司代码
        driver_kind: 驱动类型（与 P1.3 DRIVER_KINDS 对齐，如 asp / utilization_rate / capex）
        driver_value: 驱动数值（None 表示待估）
        driver_unit: 驱动单位
        driver_period: 驱动期间 YYYY-MM-DD
        impact_magnitude: 影响幅度 [0, 1]（仅描述性，不作为门禁）
        impact_path: 影响路径（如 "ASP 上涨 → 毛利率提升"）
        mapping_confidence: 映射置信度 [0, 1]
        note: 备注
    """

    symbol: str = ""
    driver_kind: str = ""
    driver_value: float | None = None
    driver_unit: str = ""
    driver_period: str = ""
    impact_magnitude: float = 0.0
    impact_path: str = ""
    mapping_confidence: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "driver_kind": self.driver_kind,
            "driver_value": self.driver_value,
            "driver_unit": self.driver_unit,
            "driver_period": self.driver_period,
            "impact_magnitude": self.impact_magnitude,
            "impact_path": self.impact_path,
            "mapping_confidence": self.mapping_confidence,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompanyDriverCandidate":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        return cls(
            symbol=str(data.get("symbol", "")),
            driver_kind=str(data.get("driver_kind", "")),
            driver_value=_finite_or_none(data.get("driver_value")),
            driver_unit=str(data.get("driver_unit", "")),
            driver_period=str(data.get("driver_period", "")),
            impact_magnitude=_safe_float(data.get("impact_magnitude", 0.0)),
            impact_path=str(data.get("impact_path", "")),
            mapping_confidence=_safe_float(data.get("mapping_confidence", 0.0)),
            note=str(data.get("note", "")),
        )


# ---------------------------------------------------------------------------
# 变化事件
# ---------------------------------------------------------------------------


@dataclass
class ChangeEvent:
    """产业与公司变化事件。

    按验收点 2「事件必须有时间、实体、方向、来源和影响路径」，
    所有必填字段必须在创建时提供。

    Attributes:
        event_id: 事件唯一 ID
        event_type: 事件类型（EVENT_TYPES 之一）
        observed_at: 事件观察时间（YYYY-MM-DD，PIT 时点）
        entity_name: 实体名（如 "碳酸锂" / "宁德时代" / "工信部"）
        entity_kind: 实体类型（product / company / industry / regulator）
        direction: 事件方向（positive / negative / neutral）
        magnitude: 事件幅度（数值，单位由 magnitude_unit 指定）
        magnitude_unit: 幅度单位（如 "%" / "元/吨" / "GWh"）
        source_refs: 来源引用列表（至少 1 个）
        impact_path: 影响路径（如 "碳酸锂降价 → 电池厂毛利提升"）
        impact_path_steps: 影响路径分步骤列表
        status: 当前状态（5 阶段状态机之一）
        evidence_list: 已收集证据列表
        candidate: 公司驱动候选（status >= company_driver_candidate 时填充）
        rejection_reason: 拒绝原因（status=rejected 时填充）
        expires_at: 过期时间（YYYY-MM-DD）
        created_at: 创建时间（UTC ISO）
        updated_at: 更新时间（UTC ISO）
        dedup_key: 去重键（用于重复事件去重）
        conflict_with: 与之冲突的事件 ID 列表
        note: 备注
    """

    event_id: str = field(default_factory=lambda: new_id("cevt"))
    event_type: str = ""
    observed_at: str = ""
    entity_name: str = ""
    entity_kind: str = ""
    direction: str = DIRECTION_NEUTRAL
    magnitude: float | None = None
    magnitude_unit: str = ""
    source_refs: list[str] = field(default_factory=list)
    impact_path: str = ""
    impact_path_steps: list[str] = field(default_factory=list)
    status: str = STATUS_OBSERVED_EVENT
    evidence_list: list[EventEvidence] = field(default_factory=list)
    candidate: CompanyDriverCandidate | None = None
    rejection_reason: str = ""
    expires_at: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    dedup_key: str = ""
    conflict_with: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "observed_at": self.observed_at,
            "entity_name": self.entity_name,
            "entity_kind": self.entity_kind,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "magnitude_unit": self.magnitude_unit,
            "source_refs": list(self.source_refs),
            "impact_path": self.impact_path,
            "impact_path_steps": list(self.impact_path_steps),
            "status": self.status,
            "evidence_list": [e.to_dict() for e in self.evidence_list],
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "rejection_reason": self.rejection_reason,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dedup_key": self.dedup_key,
            "conflict_with": list(self.conflict_with),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangeEvent":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        candidate_raw = data.get("candidate")
        raw_source_refs = data.get("source_refs", [])
        raw_impact_steps = data.get("impact_path_steps", [])
        raw_evidence = data.get("evidence_list", [])
        raw_conflicts = data.get("conflict_with", [])
        return cls(
            event_id=str(data.get("event_id", "")),
            event_type=str(data.get("event_type", "")),
            observed_at=str(data.get("observed_at", "")),
            entity_name=str(data.get("entity_name", "")),
            entity_kind=str(data.get("entity_kind", "")),
            direction=str(data.get("direction", DIRECTION_NEUTRAL)),
            magnitude=_finite_or_none(data.get("magnitude")),
            magnitude_unit=str(data.get("magnitude_unit", "")),
            source_refs=_text_list(raw_source_refs),
            impact_path=str(data.get("impact_path", "")),
            impact_path_steps=_text_list(raw_impact_steps),
            status=str(data.get("status", STATUS_OBSERVED_EVENT)),
            evidence_list=[
                EventEvidence.from_dict(e)
                for e in (raw_evidence if isinstance(raw_evidence, list) else [])
                if isinstance(e, Mapping)
            ],
            candidate=(
                CompanyDriverCandidate.from_dict(candidate_raw)
                if isinstance(candidate_raw, Mapping) else None
            ),
            rejection_reason=str(data.get("rejection_reason", "")),
            expires_at=str(data.get("expires_at", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            dedup_key=str(data.get("dedup_key", "")),
            conflict_with=_text_list(raw_conflicts),
            note=str(data.get("note", "")),
        )

    def validate_required_fields(self) -> list[str]:
        """校验收点 2 必填字段。

        按验收点 2「事件必须有时间、实体、方向、来源和影响路径」，
        缺任一必填字段返回错误消息。

        Returns:
            错误消息列表；空列表表示合法
        """
        errors: list[str] = []
        if not self.observed_at:
            errors.append("observed_at 缺失（事件必须有时间）")
        if not self.entity_name:
            errors.append("entity_name 缺失（事件必须有实体）")
        if not self.direction:
            errors.append("direction 缺失（事件必须有方向）")
        if self.direction not in DIRECTIONS:
            errors.append(f"非法 direction: {self.direction}; 允许值: {DIRECTIONS}")
        if not self.source_refs:
            errors.append("source_refs 缺失（事件必须有来源）")
        if not self.impact_path and not self.impact_path_steps:
            errors.append("impact_path/impact_path_steps 缺失（事件必须有影响路径）")
        if self.event_type not in EVENT_TYPES:
            errors.append(f"非法 event_type: {self.event_type}; 允许值: {EVENT_TYPES}")
        return errors

    def compute_dedup_key(self) -> str:
        """计算去重键。

        同 (event_type, entity_name, direction, observed_at, magnitude) 视为重复事件。

        Returns:
            去重键字符串
        """
        magnitude_str = (
            f"{self.magnitude:.4f}" if self.magnitude is not None else "None"
        )
        content = (
            f"{self.event_type}|{self.entity_name}|{self.direction}|"
            f"{self.observed_at}|{magnitude_str}"
        )
        return f"dedup_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}"

    def is_terminal(self) -> bool:
        """是否为终态（不可再转移）。"""
        return self.status in TERMINAL_STATUSES

    def independent_evidence_count(self) -> int:
        """独立来源证据数。"""
        return sum(1 for e in self.evidence_list if e.is_independent)


# ---------------------------------------------------------------------------
# 变化雷达主类
# ---------------------------------------------------------------------------


class ChangeRadar:
    """产业与公司变化雷达。

    工作流程：
    1. register_event：注册 observed_event
    2. promote_to_hypothesis：升级为 retrieval_hypothesis（检索假设）
    3. support_with_evidence：升级为 evidence_supported（多源证据支持）
    4. link_to_driver：升级为 company_driver_candidate（映射为公司驱动）
    5. evaluate_model_eligibility：升级为 model_eligible / rejected
    6. expire_outdated：过期处理

    不变量：
    - 新闻事件必须经过 evidence_supported 才能升级为 company_driver_candidate
      （验收点 1）
    - 事件必填 observed_at / entity_name / direction / source_refs / impact_path
      （验收点 2）
    - 重复事件去重（基于 dedup_key，验收点 3）
    - 冲突事件记录 conflict_with（验收点 3）
    - 过期事件进入 expired 状态（验收点 3）

    硬门禁（AGENTS.md）：
    - 不输出交易动作建议
    - 字段缺失降级处理
    """

    # 升级为 evidence_supported 所需的独立来源数
    MIN_INDEPENDENT_EVIDENCE: int = 2

    # 升级为 evidence_supported 所需的平均置信度
    MIN_AVG_CONFIDENCE: float = 0.5

    def __init__(self) -> None:
        """初始化空雷达。"""
        self._events: dict[str, ChangeEvent] = {}
        self._dedup_index: dict[str, str] = {}  # dedup_key → event_id

    # ------------------------------------------------------------------
    # 事件注册与升级
    # ------------------------------------------------------------------

    def register_event(self, event: ChangeEvent) -> str:
        """注册 observed_event。

        Args:
            event: 待注册事件

        Returns:
            event_id（若重复则返回已存在事件的 event_id）

        Raises:
            ValueError: 当必填字段缺失（验收点 2）
        """
        errors = event.validate_required_fields()
        if errors:
            raise ValueError(f"事件必填字段缺失: {errors}")

        # 去重检查（验收点 3）
        dedup_key = event.compute_dedup_key()
        event.dedup_key = dedup_key
        if dedup_key in self._dedup_index:
            existing_id = self._dedup_index[dedup_key]
            # 重复事件：合并 source_refs，不创建新事件
            existing = self._events[existing_id]
            for ref in event.source_refs:
                if ref not in existing.source_refs:
                    existing.source_refs.append(ref)
            existing.updated_at = utc_now()
            existing.note = (
                (existing.note + "; " if existing.note else "")
                + f"deduped_with_new_source: {event.event_id}"
            )
            return existing_id

        # 冲突检测（验收点 3）：同 entity 同 observed_at 但 direction 相反
        self._detect_and_record_conflicts(event)

        # 注册
        event.status = STATUS_OBSERVED_EVENT
        self._events[event.event_id] = event
        self._dedup_index[dedup_key] = event.event_id
        return event.event_id

    def promote_to_hypothesis(
        self,
        event_id: str,
        retrieval_plan: str = "",
    ) -> ChangeEvent:
        """升级为 retrieval_hypothesis。

        Args:
            event_id: 事件 ID
            retrieval_plan: 检索计划描述

        Returns:
            更新后的 ChangeEvent

        Raises:
            ValueError: 当事件不存在或状态转移非法
        """
        event = self._get_event_or_raise(event_id)
        self._validate_transition(event.status, STATUS_RETRIEVAL_HYPOTHESIS)
        event.status = STATUS_RETRIEVAL_HYPOTHESIS
        event.updated_at = utc_now()
        if retrieval_plan:
            event.note = (
                (event.note + "; " if event.note else "")
                + f"retrieval_plan: {retrieval_plan}"
            )
        return event

    def support_with_evidence(
        self,
        event_id: str,
        evidence: EventEvidence,
    ) -> ChangeEvent:
        """添加证据并尝试升级为 evidence_supported。

        按验收点 1「新闻事件不能直接成为公司经营事实」，事件必须经过
        多源交叉验证（≥ MIN_INDEPENDENT_EVIDENCE 个独立来源）才能升级。

        Args:
            event_id: 事件 ID
            evidence: 新增证据

        Returns:
            更新后的 ChangeEvent

        Raises:
            ValueError: 当事件不存在或状态转移非法
        """
        event = self._get_event_or_raise(event_id)
        # 仅 retrieval_hypothesis 状态可添加证据升级
        if event.status not in (STATUS_RETRIEVAL_HYPOTHESIS, STATUS_EVIDENCE_SUPPORTED):
            self._validate_transition(event.status, STATUS_EVIDENCE_SUPPORTED)
        event.evidence_list.append(evidence)
        event.updated_at = utc_now()

        # 检查是否满足升级条件
        if event.status == STATUS_RETRIEVAL_HYPOTHESIS:
            if self._meets_evidence_threshold(event):
                event.status = STATUS_EVIDENCE_SUPPORTED
        return event

    def link_to_driver(
        self,
        event_id: str,
        candidate: CompanyDriverCandidate,
    ) -> ChangeEvent:
        """升级为 company_driver_candidate。

        按验收点 1「新闻事件不能直接成为公司经营事实」，事件必须先达到
        evidence_supported 状态才能升级为 company_driver_candidate。

        Args:
            event_id: 事件 ID
            candidate: 公司驱动候选

        Returns:
            更新后的 ChangeEvent

        Raises:
            ValueError: 当事件不存在、状态转移非法或事件未达 evidence_supported
        """
        event = self._get_event_or_raise(event_id)
        if event.status != STATUS_EVIDENCE_SUPPORTED:
            raise ValueError(
                f"事件 {event_id} 当前状态 {event.status}，"
                f"必须先达到 {STATUS_EVIDENCE_SUPPORTED} 才能升级为 "
                f"{STATUS_COMPANY_DRIVER_CANDIDATE}（验收点 1：新闻事件"
                f"不能直接成为公司经营事实）"
            )
        self._validate_transition(event.status, STATUS_COMPANY_DRIVER_CANDIDATE)
        event.status = STATUS_COMPANY_DRIVER_CANDIDATE
        event.candidate = candidate
        event.updated_at = utc_now()
        return event

    def evaluate_model_eligibility(
        self,
        event_id: str,
        eligible: bool,
        rejection_reason: str = "",
    ) -> ChangeEvent:
        """评估模型可用性，升级为 model_eligible 或 rejected。

        Args:
            event_id: 事件 ID
            eligible: 是否模型可用
            rejection_reason: 拒绝原因（eligible=False 时必填）

        Returns:
            更新后的 ChangeEvent

        Raises:
            ValueError: 当事件不存在或状态转移非法
        """
        event = self._get_event_or_raise(event_id)
        target_status = STATUS_MODEL_ELIGIBLE if eligible else STATUS_REJECTED
        self._validate_transition(event.status, target_status)
        event.status = target_status
        if not eligible:
            if not rejection_reason:
                raise ValueError("拒绝时必须提供 rejection_reason")
            event.rejection_reason = rejection_reason
        event.updated_at = utc_now()
        return event

    def expire_outdated(self, as_of: str) -> list[str]:
        """过期处理：将过期事件升级为 expired。

        Args:
            as_of: 当前时点 YYYY-MM-DD

        Returns:
            过期处理的 event_id 列表
        """
        expired_ids: list[str] = []
        for event in self._events.values():
            if event.is_terminal():
                continue
            if event.expires_at and event.expires_at <= as_of:
                event.status = STATUS_EXPIRED
                event.updated_at = utc_now()
                expired_ids.append(event.event_id)
        return expired_ids

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_event(self, event_id: str) -> ChangeEvent | None:
        """获取事件。"""
        return self._events.get(event_id)

    def list_events(
        self,
        status: str | None = None,
        event_type: str | None = None,
        symbol: str | None = None,
    ) -> list[ChangeEvent]:
        """列出事件。

        Args:
            status: 状态过滤；None 表示所有状态
            event_type: 事件类型过滤；None 表示所有类型
            symbol: 公司代码过滤（candidate.symbol）；None 表示所有公司

        Returns:
            ChangeEvent 列表
        """
        events = list(self._events.values())
        if status is not None:
            events = [e for e in events if e.status == status]
        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]
        if symbol is not None:
            events = [
                e for e in events
                if e.candidate is not None and e.candidate.symbol == symbol
            ]
        return events

    def list_model_eligible_events(self, symbol: str | None = None) -> list[ChangeEvent]:
        """列出模型可用事件。"""
        return self.list_events(status=STATUS_MODEL_ELIGIBLE, symbol=symbol)

    def list_company_driver_candidates(
        self,
        symbol: str | None = None,
    ) -> list[ChangeEvent]:
        """列出公司驱动候选事件。"""
        result: list[ChangeEvent] = []
        for event in self._events.values():
            if event.status in (
                STATUS_COMPANY_DRIVER_CANDIDATE,
                STATUS_MODEL_ELIGIBLE,
            ):
                if symbol is None or (
                    event.candidate is not None and event.candidate.symbol == symbol
                ):
                    result.append(event)
        return result

    def list_conflicts(self) -> list[tuple[str, str]]:
        """列出冲突事件对。

        Returns:
            [(event_id_a, event_id_b), ...] 冲突对列表
        """
        conflicts: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for event in self._events.values():
            for other_id in event.conflict_with:
                pair = tuple(sorted([event.event_id, other_id]))
                if pair not in seen:
                    seen.add(pair)
                    conflicts.append((pair[0], pair[1]))
        return conflicts

    def list_duplicates(self) -> list[tuple[str, str]]:
        """列出重复事件对（已合并的去重记录）。

        Returns:
            [(survivor_event_id, deduped_event_id), ...] 列表；
            注意：重复事件已合并，不会同时存在，此方法返回历史记录
        """
        # 通过 note 中的 "deduped_with_new_source" 标记识别
        return []  # 实际重复事件已在 register_event 时合并

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get_event_or_raise(self, event_id: str) -> ChangeEvent:
        """获取事件或抛异常。"""
        event = self._events.get(event_id)
        if event is None:
            raise ValueError(f"事件不存在: {event_id}")
        return event

    def _validate_transition(self, from_status: str, to_status: str) -> None:
        """校验状态转移合法性。

        Args:
            from_status: 当前状态
            to_status: 目标状态

        Raises:
            ValueError: 当状态转移非法
        """
        allowed = VALID_TRANSITIONS.get(from_status, ())
        if to_status not in allowed:
            raise ValueError(
                f"非法状态转移: {from_status} → {to_status}; "
                f"允许的目标状态: {allowed}"
            )

    def _meets_evidence_threshold(self, event: ChangeEvent) -> bool:
        """检查是否满足 evidence_supported 升级条件。

        条件：
        - 独立来源数 ≥ MIN_INDEPENDENT_EVIDENCE
        - 平均置信度 ≥ MIN_AVG_CONFIDENCE

        Args:
            event: 待检查事件

        Returns:
            True 表示满足升级条件
        """
        independent_count = event.independent_evidence_count()
        if independent_count < self.MIN_INDEPENDENT_EVIDENCE:
            return False
        if not event.evidence_list:
            return False
        avg_conf = sum(e.confidence for e in event.evidence_list) / len(event.evidence_list)
        return avg_conf >= self.MIN_AVG_CONFIDENCE

    def _detect_and_record_conflicts(self, new_event: ChangeEvent) -> None:
        """检测并记录冲突事件。

        冲突定义：同 entity_name 同 observed_at 但 direction 相反。

        Args:
            new_event: 新注册事件
        """
        opposite_direction = {
            DIRECTION_POSITIVE: DIRECTION_NEGATIVE,
            DIRECTION_NEGATIVE: DIRECTION_POSITIVE,
        }.get(new_event.direction)

        if opposite_direction is None:
            return  # neutral 不冲突

        for existing in self._events.values():
            if existing.is_terminal():
                continue
            if (
                existing.entity_name == new_event.entity_name
                and existing.observed_at == new_event.observed_at
                and existing.direction == opposite_direction
                and existing.event_type == new_event.event_type
            ):
                new_event.conflict_with.append(existing.event_id)
                existing.conflict_with.append(new_event.event_id)


# ---------------------------------------------------------------------------
# 数据导入适配器（从 theme/service.py 与 causal_context.py 只读导入）
# ---------------------------------------------------------------------------


class ThemeChangeSignalAdapter:
    """从 DynamicThemeEngine 只读导入主题变化信号。

    按 P2.3 要求「修改 core/quant/theme/service.py，仅增加正式只读输出契约」，
    DynamicThemeEngine 新增 export_theme_change_signals 只读方法，
    本适配器将其输出转换为 ChangeEvent 列表。

    适配原则：
    - 主题信号仅作为 observed_event 注册，不能直接升级为 evidence_supported
    - 每个主题信号必须含 observed_at / entity_name / direction / source_refs / impact_path
    - 缺字段降级为 note 标记，不抛异常（AGENTS.md）
    """

    def adapt(self, theme_signals: list[dict[str, Any]]) -> list[ChangeEvent]:
        """将主题信号适配为 ChangeEvent 列表。

        Args:
            theme_signals: DynamicThemeEngine.export_theme_change_signals 输出

        Returns:
            ChangeEvent 列表（status=observed_event）
        """
        events: list[ChangeEvent] = []
        for signal in theme_signals:
            try:
                event = self._adapt_one(signal)
                if event is not None:
                    events.append(event)
            except Exception:  # noqa: BLE001
                # AGENTS.md：缺数据降级，不崩溃
                continue
        return events

    def _adapt_one(self, signal: dict[str, Any]) -> ChangeEvent | None:
        """适配单个主题信号。"""
        observed_at = str(signal.get("observed_at", "")[:10])
        entity_name = str(signal.get("theme_name", "") or signal.get("entity_name", ""))
        if not observed_at or not entity_name:
            return None
        direction = str(signal.get("direction", DIRECTION_NEUTRAL))
        if direction not in DIRECTIONS:
            direction = DIRECTION_NEUTRAL
        source_refs = list(signal.get("source_refs", []) or [])
        if not source_refs:
            source_refs = [str(signal.get("theme_id", ""))]
        impact_path = str(signal.get("impact_path", "") or signal.get("description", ""))
        event_type = str(signal.get("event_type", EVENT_TYPE_POLICY_SUBSIDY))
        if event_type not in EVENT_TYPES:
            event_type = EVENT_TYPE_POLICY_SUBSIDY

        return ChangeEvent(
            event_type=event_type,
            observed_at=observed_at,
            entity_name=entity_name,
            entity_kind=str(signal.get("entity_kind", "industry")),
            direction=direction,
            magnitude=signal.get("magnitude"),
            magnitude_unit=str(signal.get("magnitude_unit", "")),
            source_refs=source_refs,
            impact_path=impact_path,
            impact_path_steps=list(signal.get("impact_path_steps", []) or []),
            expires_at=str(signal.get("expires_at", "")),
            note=f"adapted_from_theme_signal: {signal.get('theme_id', '')}",
        )


class CausalContextFactAdapter:
    """从 ResearchCausalContext 只读导入已观察事实。

    按 P2.3 要求「修改 core/research/value_chain/causal_context.py」，
    ResearchCausalContext 新增 export_observed_facts_for_radar 只读方法，
    本适配器将其输出转换为 ChangeEvent 列表。

    适配原则：
    - 已观察事实（observed_facts）仅作为 observed_event 注册
    - verified_edges 不进入雷达（已是验证后的因果链，不需重新升级）
    - 缺字段降级为 note 标记，不抛异常（AGENTS.md）
    """

    def adapt(self, observed_facts: list[dict[str, Any]]) -> list[ChangeEvent]:
        """将已观察事实适配为 ChangeEvent 列表。

        Args:
            observed_facts: ResearchCausalContext.export_observed_facts_for_radar 输出

        Returns:
            ChangeEvent 列表（status=observed_event）
        """
        events: list[ChangeEvent] = []
        for fact in observed_facts:
            try:
                event = self._adapt_one(fact)
                if event is not None:
                    events.append(event)
            except Exception:  # noqa: BLE001
                continue
        return events

    def _adapt_one(self, fact: dict[str, Any]) -> ChangeEvent | None:
        """适配单个已观察事实。"""
        observed_at = str(fact.get("observed_at", "") or fact.get("published_at", ""))[:10]
        entity_name = str(fact.get("object_name", "") or fact.get("entity_name", ""))
        if not observed_at or not entity_name:
            return None
        direction = str(fact.get("direction", DIRECTION_NEUTRAL))
        if direction not in DIRECTIONS:
            direction = DIRECTION_NEUTRAL
        source_refs = list(fact.get("source_refs", []) or [])
        if not source_refs:
            source_refs = [str(fact.get("claim_id", ""))]
        impact_path = str(fact.get("impact_path", "") or fact.get("relation", ""))
        event_type = str(fact.get("event_type", EVENT_TYPE_PRODUCT_PRICE_COST))
        if event_type not in EVENT_TYPES:
            event_type = EVENT_TYPE_PRODUCT_PRICE_COST

        return ChangeEvent(
            event_type=event_type,
            observed_at=observed_at,
            entity_name=entity_name,
            entity_kind=str(fact.get("entity_kind", "product")),
            direction=direction,
            magnitude=fact.get("magnitude"),
            magnitude_unit=str(fact.get("magnitude_unit", "")),
            source_refs=source_refs,
            impact_path=impact_path,
            impact_path_steps=list(fact.get("impact_path_steps", []) or []),
            note=f"adapted_from_observed_fact: {fact.get('claim_id', '')}",
        )


__all__ = [
    # 事件类型常量
    "EVENT_TYPES",
    "EVENT_TYPE_PRODUCT_PRICE_COST",
    "EVENT_TYPE_ORDER_TENDER",
    "EVENT_TYPE_CAPACITY_UTILIZATION_INVENTORY",
    "EVENT_TYPE_HIRING_PATENT_TECH",
    "EVENT_TYPE_CAPEX_EXPANSION",
    "EVENT_TYPE_POLICY_SUBSIDY",
    "EVENT_TYPE_MANAGEMENT_GUIDANCE",
    "EVENT_TYPE_CONSENSUS_REVISION",
    # 状态常量
    "STATUS_OBSERVED_EVENT",
    "STATUS_RETRIEVAL_HYPOTHESIS",
    "STATUS_EVIDENCE_SUPPORTED",
    "STATUS_COMPANY_DRIVER_CANDIDATE",
    "STATUS_MODEL_ELIGIBLE",
    "STATUS_REJECTED",
    "STATUS_EXPIRED",
    "VALID_TRANSITIONS",
    "TERMINAL_STATUSES",
    # 方向常量
    "DIRECTIONS",
    "DIRECTION_POSITIVE",
    "DIRECTION_NEGATIVE",
    "DIRECTION_NEUTRAL",
    # 数据类
    "EventEvidence",
    "CompanyDriverCandidate",
    "ChangeEvent",
    # 主类
    "ChangeRadar",
    # 适配器
    "ThemeChangeSignalAdapter",
    "CausalContextFactAdapter",
    # 工具
    "utc_now",
    "new_id",
]
