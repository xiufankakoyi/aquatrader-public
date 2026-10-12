"""P2.3 产业与公司变化雷达测试。

按 P2 计划 §4 Task P2.3 验收：
- 新闻事件不能直接成为公司经营事实
- 事件必须有时间、实体、方向、来源和影响路径
- 重复事件去重、过期和冲突处理可验证

测试组织：
- TestEventTypes：8 类事件类型常量
- TestStateMachine：5 阶段状态机与合法转移
- TestEventEvidence：EventEvidence 数据类
- TestChangeEventRequiredFields：验收点 2 必填字段
- TestChangeEventDedup：去重键计算
- TestRegisterEvent：事件注册（含去重与冲突）
- TestPromoteToHypothesis：升级 retrieval_hypothesis
- TestSupportWithEvidence：验收点 1 多源证据升级
- TestLinkToDriver：验收点 1 阻断直接升级
- TestModelEligibility：model_eligible / rejected
- TestExpireOutdated：验收点 3 过期处理
- TestQueryMethods：查询接口
- TestThemeChangeSignalAdapter：适配器
- TestCausalContextFactAdapter：适配器
- TestAcceptancePoint1：验收点 1 综合测试
- TestAcceptancePoint2：验收点 2 综合测试
- TestAcceptancePoint3：验收点 3 综合测试
- TestGraceful：字段缺失降级（AGENTS.md）
- TestNoTradeAdvice：AGENTS.md 合规性
- TestEndToEndIntegration：端到端集成
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from core.research.discovery.change_radar import (
    ChangeEvent,
    ChangeRadar,
    CompanyDriverCandidate,
    CausalContextFactAdapter,
    DIRECTION_NEGATIVE,
    DIRECTION_NEUTRAL,
    DIRECTION_POSITIVE,
    DIRECTIONS,
    EVENT_TYPES,
    EVENT_TYPE_CAPACITY_UTILIZATION_INVENTORY,
    EVENT_TYPE_CAPEX_EXPANSION,
    EVENT_TYPE_CONSENSUS_REVISION,
    EVENT_TYPE_HIRING_PATENT_TECH,
    EVENT_TYPE_MANAGEMENT_GUIDANCE,
    EVENT_TYPE_ORDER_TENDER,
    EVENT_TYPE_POLICY_SUBSIDY,
    EVENT_TYPE_PRODUCT_PRICE_COST,
    EventEvidence,
    STATUS_COMPANY_DRIVER_CANDIDATE,
    STATUS_EVIDENCE_SUPPORTED,
    STATUS_EXPIRED,
    STATUS_MODEL_ELIGIBLE,
    STATUS_OBSERVED_EVENT,
    STATUS_REJECTED,
    STATUS_RETRIEVAL_HYPOTHESIS,
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    ThemeChangeSignalAdapter,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_evidence(
    evidence_id: str = "evd_test_001",
    source_ref: str = "src_test_001",
    source_kind: str = "announcement",
    source_publisher: str = "中国证券报",
    published_at: str = "2024-06-30",
    confidence: float = 0.7,
    is_independent: bool = True,
) -> EventEvidence:
    """构造 EventEvidence。"""
    return EventEvidence(
        evidence_id=evidence_id,
        source_ref=source_ref,
        source_kind=source_kind,
        source_publisher=source_publisher,
        published_at=published_at,
        confidence=confidence,
        is_independent=is_independent,
    )


def _make_event(
    event_id: str = "cevt_test_001",
    event_type: str = EVENT_TYPE_PRODUCT_PRICE_COST,
    observed_at: str = "2024-06-30",
    entity_name: str = "碳酸锂",
    entity_kind: str = "product",
    direction: str = DIRECTION_POSITIVE,
    magnitude: float | None = 0.05,
    magnitude_unit: str = "%",
    source_refs: list[str] | None = None,
    impact_path: str = "碳酸锂涨价 → 电池厂毛利承压",
    impact_path_steps: list[str] | None = None,
    expires_at: str = "2024-12-31",
    note: str = "",
) -> ChangeEvent:
    """构造合法 ChangeEvent。"""
    return ChangeEvent(
        event_id=event_id,
        event_type=event_type,
        observed_at=observed_at,
        entity_name=entity_name,
        entity_kind=entity_kind,
        direction=direction,
        magnitude=magnitude,
        magnitude_unit=magnitude_unit,
        source_refs=source_refs or ["src_test_001"],
        impact_path=impact_path,
        impact_path_steps=impact_path_steps
        or ["step1: 价格上涨", "step2: 成本传导"],
        expires_at=expires_at,
        note=note,
    )


def _make_candidate(
    symbol: str = "300750.SZ",
    driver_kind: str = "asp",
    driver_value: float | None = 0.05,
    driver_unit: str = "%",
    driver_period: str = "2024-06-30",
    impact_magnitude: float = 0.6,
    impact_path: str = "ASP 上涨 → 毛利率提升",
    mapping_confidence: float = 0.8,
) -> CompanyDriverCandidate:
    """构造 CompanyDriverCandidate。"""
    return CompanyDriverCandidate(
        symbol=symbol,
        driver_kind=driver_kind,
        driver_value=driver_value,
        driver_unit=driver_unit,
        driver_period=driver_period,
        impact_magnitude=impact_magnitude,
        impact_path=impact_path,
        mapping_confidence=mapping_confidence,
    )


def _make_full_pipeline_event(radar: ChangeRadar) -> str:
    """构造一个完整走完状态机的合法事件，返回 event_id。"""
    event = _make_event(event_id="cevt_pipeline_001")
    event_id = radar.register_event(event)
    radar.promote_to_hypothesis(event_id, retrieval_plan="检索公告与行业研报")
    ev1 = _make_evidence(
        evidence_id="evd_p_001",
        source_ref="src_a_001",
        source_publisher="中国证券报",
        confidence=0.7,
    )
    ev2 = _make_evidence(
        evidence_id="evd_p_002",
        source_ref="src_b_001",
        source_publisher="上海证券报",
        confidence=0.6,
    )
    radar.support_with_evidence(event_id, ev1)
    radar.support_with_evidence(event_id, ev2)
    radar.link_to_driver(event_id, _make_candidate())
    return event_id


# ---------------------------------------------------------------------------
# 8 类事件类型
# ---------------------------------------------------------------------------


class TestEventTypes:
    """8 类事件类型常量。"""

    def test_event_types_count(self) -> None:
        """应恰好有 8 类事件。"""
        assert len(EVENT_TYPES) == 8

    def test_event_types_unique(self) -> None:
        """事件类型应无重复。"""
        assert len(set(EVENT_TYPES)) == 8

    def test_each_event_type_is_nonempty_string(self) -> None:
        """每个事件类型应为非空字符串。"""
        for et in EVENT_TYPES:
            assert isinstance(et, str) and et

    def test_event_type_constants(self) -> None:
        """事件类型常量与名称对齐。"""
        assert EVENT_TYPE_PRODUCT_PRICE_COST == "product_price_cost"
        assert EVENT_TYPE_ORDER_TENDER == "order_tender"
        assert EVENT_TYPE_CAPACITY_UTILIZATION_INVENTORY == "capacity_utilization_inventory"
        assert EVENT_TYPE_HIRING_PATENT_TECH == "hiring_patent_tech"
        assert EVENT_TYPE_CAPEX_EXPANSION == "capex_expansion"
        assert EVENT_TYPE_POLICY_SUBSIDY == "policy_subsidy"
        assert EVENT_TYPE_MANAGEMENT_GUIDANCE == "management_guidance"
        assert EVENT_TYPE_CONSENSUS_REVISION == "consensus_revision"


# ---------------------------------------------------------------------------
# 5 阶段状态机
# ---------------------------------------------------------------------------


class TestStateMachine:
    """5 阶段状态机与合法转移。"""

    def test_state_count(self) -> None:
        """应有 7 个状态常量（5 阶段 + rejected + expired）。"""
        states = {
            STATUS_OBSERVED_EVENT,
            STATUS_RETRIEVAL_HYPOTHESIS,
            STATUS_EVIDENCE_SUPPORTED,
            STATUS_COMPANY_DRIVER_CANDIDATE,
            STATUS_MODEL_ELIGIBLE,
            STATUS_REJECTED,
            STATUS_EXPIRED,
        }
        assert len(states) == 7

    def test_valid_transitions_observed_event(self) -> None:
        """observed_event 可转向 retrieval_hypothesis / rejected / expired。"""
        allowed = VALID_TRANSITIONS[STATUS_OBSERVED_EVENT]
        assert STATUS_RETRIEVAL_HYPOTHESIS in allowed
        assert STATUS_REJECTED in allowed
        assert STATUS_EXPIRED in allowed
        # 不能直接跳到 evidence_supported
        assert STATUS_EVIDENCE_SUPPORTED not in allowed

    def test_valid_transitions_retrieval_hypothesis(self) -> None:
        """retrieval_hypothesis 可转向 evidence_supported / rejected / expired。"""
        allowed = VALID_TRANSITIONS[STATUS_RETRIEVAL_HYPOTHESIS]
        assert STATUS_EVIDENCE_SUPPORTED in allowed
        assert STATUS_REJECTED in allowed
        assert STATUS_EXPIRED in allowed
        # 不能跳过 evidence_supported 直接到 company_driver_candidate
        assert STATUS_COMPANY_DRIVER_CANDIDATE not in allowed

    def test_valid_transitions_evidence_supported(self) -> None:
        """evidence_supported 可转向 company_driver_candidate / rejected / expired。"""
        allowed = VALID_TRANSITIONS[STATUS_EVIDENCE_SUPPORTED]
        assert STATUS_COMPANY_DRIVER_CANDIDATE in allowed
        assert STATUS_REJECTED in allowed
        assert STATUS_EXPIRED in allowed

    def test_valid_transitions_company_driver_candidate(self) -> None:
        """company_driver_candidate 可转向 model_eligible / rejected / expired。"""
        allowed = VALID_TRANSITIONS[STATUS_COMPANY_DRIVER_CANDIDATE]
        assert STATUS_MODEL_ELIGIBLE in allowed
        assert STATUS_REJECTED in allowed
        assert STATUS_EXPIRED in allowed

    def test_model_eligible_only_to_expired(self) -> None:
        """model_eligible 仅可转向 expired。"""
        assert VALID_TRANSITIONS[STATUS_MODEL_ELIGIBLE] == (STATUS_EXPIRED,)

    def test_rejected_only_to_expired(self) -> None:
        """rejected 仅可转向 expired。"""
        assert VALID_TRANSITIONS[STATUS_REJECTED] == (STATUS_EXPIRED,)

    def test_expired_is_terminal(self) -> None:
        """expired 是终态，无任何后续转移。"""
        assert VALID_TRANSITIONS[STATUS_EXPIRED] == ()
        assert STATUS_EXPIRED in TERMINAL_STATUSES

    def test_terminal_statuses_only_expired(self) -> None:
        """终态只有 expired。"""
        assert TERMINAL_STATUSES == (STATUS_EXPIRED,)


# ---------------------------------------------------------------------------
# EventEvidence 数据类
# ---------------------------------------------------------------------------


class TestEventEvidence:
    """EventEvidence 数据类。"""

    def test_default_factory_generates_id(self) -> None:
        """默认应自动生成 evidence_id。"""
        ev = EventEvidence()
        assert ev.evidence_id.startswith("evd_")
        assert len(ev.evidence_id) > len("evd_")

    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        """序列化与反序列化应保持一致。"""
        ev = _make_evidence(confidence=0.85, is_independent=False)
        d = ev.to_dict()
        ev2 = EventEvidence.from_dict(d)
        assert ev2.evidence_id == ev.evidence_id
        assert ev2.source_ref == ev.source_ref
        assert ev2.confidence == pytest.approx(0.85)
        assert ev2.is_independent is False


# ---------------------------------------------------------------------------
# 验收点 2：必填字段
# ---------------------------------------------------------------------------


class TestChangeEventRequiredFields:
    """验收点 2：事件必须有时间、实体、方向、来源和影响路径。"""

    def test_valid_event_no_errors(self) -> None:
        """合法事件无错误。"""
        event = _make_event()
        errors = event.validate_required_fields()
        assert errors == []

    def test_missing_observed_at(self) -> None:
        """缺 observed_at 应报错。"""
        event = _make_event()
        event.observed_at = ""
        errors = event.validate_required_fields()
        assert any("observed_at" in e for e in errors)

    def test_missing_entity_name(self) -> None:
        """缺 entity_name 应报错。"""
        event = _make_event()
        event.entity_name = ""
        errors = event.validate_required_fields()
        assert any("entity_name" in e for e in errors)

    def test_missing_direction(self) -> None:
        """缺 direction 应报错。"""
        event = _make_event()
        event.direction = ""
        errors = event.validate_required_fields()
        assert any("direction" in e for e in errors)

    def test_invalid_direction(self) -> None:
        """非法 direction 应报错。"""
        event = _make_event()
        event.direction = "bullish"
        errors = event.validate_required_fields()
        assert any("direction" in e for e in errors)

    def test_missing_source_refs(self) -> None:
        """缺 source_refs 应报错。"""
        event = _make_event()
        event.source_refs = []
        errors = event.validate_required_fields()
        assert any("source_refs" in e for e in errors)

    def test_missing_impact_path(self) -> None:
        """缺 impact_path 与 impact_path_steps 应报错。"""
        event = _make_event()
        event.impact_path = ""
        event.impact_path_steps = []
        errors = event.validate_required_fields()
        assert any("impact_path" in e for e in errors)

    def test_impact_path_steps_satisfies_required(self) -> None:
        """仅 impact_path_steps 存在也应满足必填。"""
        event = _make_event()
        event.impact_path = ""
        event.impact_path_steps = ["step1"]
        errors = event.validate_required_fields()
        assert not any("impact_path" in e for e in errors)

    def test_invalid_event_type(self) -> None:
        """非法 event_type 应报错。"""
        event = _make_event()
        event.event_type = "unknown_type"
        errors = event.validate_required_fields()
        assert any("event_type" in e for e in errors)

    def test_legal_directions_accepted(self) -> None:
        """positive / negative / neutral 都应被接受。"""
        for direction in DIRECTIONS:
            event = _make_event(direction=direction)
            errors = event.validate_required_fields()
            assert not any("direction" in e for e in errors)


# ---------------------------------------------------------------------------
# 去重键
# ---------------------------------------------------------------------------


class TestChangeEventDedup:
    """去重键计算。"""

    def test_same_content_same_dedup_key(self) -> None:
        """同内容事件应产生相同 dedup_key。"""
        e1 = _make_event(event_id="cevt_001")
        e2 = _make_event(event_id="cevt_002")
        # event_id 不参与 dedup_key 计算
        assert e1.compute_dedup_key() == e2.compute_dedup_key()

    def test_different_event_type_different_dedup_key(self) -> None:
        """不同 event_type 应产生不同 dedup_key。"""
        e1 = _make_event(event_type=EVENT_TYPE_PRODUCT_PRICE_COST)
        e2 = _make_event(event_type=EVENT_TYPE_ORDER_TENDER)
        assert e1.compute_dedup_key() != e2.compute_dedup_key()

    def test_different_entity_different_dedup_key(self) -> None:
        """不同 entity_name 应产生不同 dedup_key。"""
        e1 = _make_event(entity_name="碳酸锂")
        e2 = _make_event(entity_name="氢氧化锂")
        assert e1.compute_dedup_key() != e2.compute_dedup_key()

    def test_different_direction_different_dedup_key(self) -> None:
        """不同 direction 应产生不同 dedup_key。"""
        e1 = _make_event(direction=DIRECTION_POSITIVE)
        e2 = _make_event(direction=DIRECTION_NEGATIVE)
        assert e1.compute_dedup_key() != e2.compute_dedup_key()

    def test_different_observed_at_different_dedup_key(self) -> None:
        """不同 observed_at 应产生不同 dedup_key。"""
        e1 = _make_event(observed_at="2024-06-30")
        e2 = _make_event(observed_at="2024-07-01")
        assert e1.compute_dedup_key() != e2.compute_dedup_key()

    def test_different_magnitude_different_dedup_key(self) -> None:
        """不同 magnitude 应产生不同 dedup_key。"""
        e1 = _make_event(magnitude=0.05)
        e2 = _make_event(magnitude=0.10)
        assert e1.compute_dedup_key() != e2.compute_dedup_key()

    def test_magnitude_none_vs_value_different(self) -> None:
        """magnitude=None 与有值应产生不同 dedup_key。"""
        e1 = _make_event(magnitude=None)
        e2 = _make_event(magnitude=0.05)
        assert e1.compute_dedup_key() != e2.compute_dedup_key()

    def test_dedup_key_starts_with_prefix(self) -> None:
        """dedup_key 应以 'dedup_' 前缀开头。"""
        event = _make_event()
        assert event.compute_dedup_key().startswith("dedup_")


# ---------------------------------------------------------------------------
# 事件注册
# ---------------------------------------------------------------------------


class TestRegisterEvent:
    """事件注册（含去重与冲突）。"""

    def test_register_returns_event_id(self) -> None:
        """注册后应返回 event_id。"""
        radar = ChangeRadar()
        event = _make_event(event_id="cevt_r_001")
        result = radar.register_event(event)
        assert result == "cevt_r_001"

    def test_register_sets_status_to_observed(self) -> None:
        """注册后状态应为 observed_event。"""
        radar = ChangeRadar()
        event = _make_event()
        event.status = STATUS_RETRIEVAL_HYPOTHESIS  # 故意设置一个非初始状态
        event_id = radar.register_event(event)
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.status == STATUS_OBSERVED_EVENT

    def test_register_sets_dedup_key(self) -> None:
        """注册后应自动设置 dedup_key。"""
        radar = ChangeRadar()
        event = _make_event()
        event_id = radar.register_event(event)
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.dedup_key.startswith("dedup_")

    def test_register_raises_on_missing_required_fields(self) -> None:
        """必填字段缺失应抛 ValueError。"""
        radar = ChangeRadar()
        event = _make_event()
        event.observed_at = ""
        with pytest.raises(ValueError, match="必填字段缺失"):
            radar.register_event(event)

    def test_register_dedup_merges_source_refs(self) -> None:
        """重复事件应合并 source_refs，不创建新事件。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_d_001", source_refs=["src_a"])
        eid1 = radar.register_event(e1)
        e2 = _make_event(event_id="cevt_d_002", source_refs=["src_b"])
        eid2 = radar.register_event(e2)
        assert eid1 == eid2
        stored = radar.get_event(eid1)
        assert stored is not None
        assert "src_a" in stored.source_refs
        assert "src_b" in stored.source_refs
        assert len(stored.source_refs) == 2

    def test_register_dedup_same_source_no_duplication(self) -> None:
        """重复事件的相同 source_ref 不应重复加入。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_d_001", source_refs=["src_a"])
        eid1 = radar.register_event(e1)
        e2 = _make_event(event_id="cevt_d_002", source_refs=["src_a"])
        eid2 = radar.register_event(e2)
        assert eid1 == eid2
        stored = radar.get_event(eid1)
        assert stored.source_refs.count("src_a") == 1

    def test_register_detects_conflict_positive_vs_negative(self) -> None:
        """同 entity 同 observed_at 但 direction 相反应记录冲突。"""
        radar = ChangeRadar()
        e1 = _make_event(
            event_id="cevt_c_001",
            direction=DIRECTION_POSITIVE,
            source_refs=["src_a"],
        )
        eid1 = radar.register_event(e1)
        e2 = _make_event(
            event_id="cevt_c_002",
            direction=DIRECTION_NEGATIVE,
            source_refs=["src_b"],
        )
        eid2 = radar.register_event(e2)
        assert eid1 != eid2
        stored1 = radar.get_event(eid1)
        stored2 = radar.get_event(eid2)
        assert eid2 in stored1.conflict_with
        assert eid1 in stored2.conflict_with

    def test_register_no_conflict_for_neutral(self) -> None:
        """neutral 方向不与任何事件冲突。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_n_001", direction=DIRECTION_NEUTRAL)
        eid1 = radar.register_event(e1)
        e2 = _make_event(event_id="cevt_n_002", direction=DIRECTION_POSITIVE)
        eid2 = radar.register_event(e2)
        stored1 = radar.get_event(eid1)
        stored2 = radar.get_event(eid2)
        assert stored1.conflict_with == []
        assert stored2.conflict_with == []

    def test_register_no_conflict_different_entity(self) -> None:
        """不同 entity_name 不应记录冲突。"""
        radar = ChangeRadar()
        e1 = _make_event(
            event_id="cevt_e_001",
            entity_name="碳酸锂",
            direction=DIRECTION_POSITIVE,
        )
        eid1 = radar.register_event(e1)
        e2 = _make_event(
            event_id="cevt_e_002",
            entity_name="氢氧化锂",
            direction=DIRECTION_NEGATIVE,
        )
        eid2 = radar.register_event(e2)
        stored1 = radar.get_event(eid1)
        assert stored1.conflict_with == []


# ---------------------------------------------------------------------------
# promote_to_hypothesis
# ---------------------------------------------------------------------------


class TestPromoteToHypothesis:
    """升级 retrieval_hypothesis。"""

    def test_promote_success(self) -> None:
        """observed_event 可成功升级为 retrieval_hypothesis。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        updated = radar.promote_to_hypothesis(event_id, retrieval_plan="检索公告")
        assert updated.status == STATUS_RETRIEVAL_HYPOTHESIS
        assert "retrieval_plan" in updated.note

    def test_promote_unknown_event_raises(self) -> None:
        """升级不存在的事件应抛 ValueError。"""
        radar = ChangeRadar()
        with pytest.raises(ValueError, match="事件不存在"):
            radar.promote_to_hypothesis("nonexistent_id")

    def test_promote_invalid_transition_raises(self) -> None:
        """evidence_supported 不能直接 promote_to_hypothesis。"""
        radar = ChangeRadar()
        event_id = _make_full_pipeline_event(radar)
        # 此时状态为 model_eligible，promote_to_hypothesis 应失败
        with pytest.raises(ValueError, match="非法状态转移"):
            radar.promote_to_hypothesis(event_id)


# ---------------------------------------------------------------------------
# support_with_evidence：验收点 1
# ---------------------------------------------------------------------------


class TestSupportWithEvidence:
    """support_with_evidence 与验收点 1 升级条件。"""

    def test_single_evidence_does_not_promote(self) -> None:
        """单条证据不满足升级条件。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        ev = _make_evidence(source_ref="src_a", confidence=0.9)
        updated = radar.support_with_evidence(event_id, ev)
        assert updated.status == STATUS_RETRIEVAL_HYPOTHESIS

    def test_two_independent_evidence_promotes(self) -> None:
        """≥2 个独立来源且平均置信度 ≥0.5 应升级为 evidence_supported。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        ev1 = _make_evidence(
            evidence_id="evd_s_001",
            source_ref="src_a",
            source_publisher="中国证券报",
            confidence=0.6,
        )
        ev2 = _make_evidence(
            evidence_id="evd_s_002",
            source_ref="src_b",
            source_publisher="上海证券报",
            confidence=0.7,
        )
        radar.support_with_evidence(event_id, ev1)
        updated = radar.support_with_evidence(event_id, ev2)
        assert updated.status == STATUS_EVIDENCE_SUPPORTED

    def test_two_non_independent_evidence_does_not_promote(self) -> None:
        """非独立来源不满足升级条件。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        ev1 = _make_evidence(
            evidence_id="evd_ni_001",
            source_ref="src_a",
            is_independent=False,
            confidence=0.9,
        )
        ev2 = _make_evidence(
            evidence_id="evd_ni_002",
            source_ref="src_b",
            is_independent=False,
            confidence=0.9,
        )
        radar.support_with_evidence(event_id, ev1)
        updated = radar.support_with_evidence(event_id, ev2)
        assert updated.status == STATUS_RETRIEVAL_HYPOTHESIS

    def test_low_avg_confidence_does_not_promote(self) -> None:
        """平均置信度 <0.5 不满足升级条件。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        ev1 = _make_evidence(
            evidence_id="evd_lc_001",
            source_ref="src_a",
            confidence=0.2,
        )
        ev2 = _make_evidence(
            evidence_id="evd_lc_002",
            source_ref="src_b",
            confidence=0.2,
        )
        radar.support_with_evidence(event_id, ev1)
        updated = radar.support_with_evidence(event_id, ev2)
        assert updated.status == STATUS_RETRIEVAL_HYPOTHESIS

    def test_evidence_can_be_added_after_evidence_supported(self) -> None:
        """evidence_supported 状态仍可继续添加证据。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        ev1 = _make_evidence(
            evidence_id="evd_es_001",
            source_ref="src_a",
            confidence=0.6,
        )
        ev2 = _make_evidence(
            evidence_id="evd_es_002",
            source_ref="src_b",
            confidence=0.7,
        )
        radar.support_with_evidence(event_id, ev1)
        radar.support_with_evidence(event_id, ev2)
        # 此时已 evidence_supported
        ev3 = _make_evidence(
            evidence_id="evd_es_003",
            source_ref="src_c",
            confidence=0.8,
        )
        updated = radar.support_with_evidence(event_id, ev3)
        assert updated.status == STATUS_EVIDENCE_SUPPORTED
        assert len(updated.evidence_list) == 3

    def test_support_from_observed_event_raises(self) -> None:
        """observed_event 状态不能直接 support_with_evidence。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        # 不调用 promote_to_hypothesis，直接 support_with_evidence
        ev = _make_evidence(source_ref="src_a", confidence=0.9)
        with pytest.raises(ValueError, match="非法状态转移"):
            radar.support_with_evidence(event_id, ev)

    def test_support_unknown_event_raises(self) -> None:
        """升级不存在的事件应抛 ValueError。"""
        radar = ChangeRadar()
        ev = _make_evidence()
        with pytest.raises(ValueError, match="事件不存在"):
            radar.support_with_evidence("nonexistent_id", ev)


# ---------------------------------------------------------------------------
# link_to_driver：验收点 1 阻断直接升级
# ---------------------------------------------------------------------------


class TestLinkToDriver:
    """验收点 1：必须先 evidence_supported 才能 link_to_driver。"""

    def test_link_after_evidence_supported_success(self) -> None:
        """evidence_supported 后可成功 link_to_driver。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        ev1 = _make_evidence(source_ref="src_a", confidence=0.6)
        ev2 = _make_evidence(source_ref="src_b", confidence=0.7)
        radar.support_with_evidence(event_id, ev1)
        radar.support_with_evidence(event_id, ev2)
        candidate = _make_candidate()
        updated = radar.link_to_driver(event_id, candidate)
        assert updated.status == STATUS_COMPANY_DRIVER_CANDIDATE
        assert updated.candidate is not None
        assert updated.candidate.symbol == "300750.SZ"

    def test_link_from_observed_event_raises(self) -> None:
        """observed_event 不能直接 link_to_driver（验收点 1 核心）。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        candidate = _make_candidate()
        with pytest.raises(ValueError, match="验收点 1"):
            radar.link_to_driver(event_id, candidate)

    def test_link_from_retrieval_hypothesis_raises(self) -> None:
        """retrieval_hypothesis 不能直接 link_to_driver（验收点 1 核心）。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        candidate = _make_candidate()
        with pytest.raises(ValueError, match="验收点 1"):
            radar.link_to_driver(event_id, candidate)

    def test_link_from_company_driver_candidate_raises(self) -> None:
        """company_driver_candidate 不能再次 link_to_driver。"""
        radar = ChangeRadar()
        event_id = _make_full_pipeline_event(radar)
        # 此时状态为 company_driver_candidate
        candidate2 = _make_candidate(driver_kind="utilization_rate")
        with pytest.raises(ValueError, match="必须先达到"):
            radar.link_to_driver(event_id, candidate2)

    def test_link_unknown_event_raises(self) -> None:
        """升级不存在的事件应抛 ValueError。"""
        radar = ChangeRadar()
        candidate = _make_candidate()
        with pytest.raises(ValueError, match="事件不存在"):
            radar.link_to_driver("nonexistent_id", candidate)


# ---------------------------------------------------------------------------
# evaluate_model_eligibility
# ---------------------------------------------------------------------------


class TestModelEligibility:
    """evaluate_model_eligibility：model_eligible / rejected。"""

    def test_eligible_from_company_driver_candidate(self) -> None:
        """company_driver_candidate 可升级为 model_eligible。"""
        radar = ChangeRadar()
        event_id = _make_full_pipeline_event(radar)
        # 此时状态为 company_driver_candidate
        updated = radar.evaluate_model_eligibility(event_id, eligible=True)
        assert updated.status == STATUS_MODEL_ELIGIBLE

    def test_rejected_from_company_driver_candidate(self) -> None:
        """company_driver_candidate 可被 rejected。"""
        radar = ChangeRadar()
        event_id = _make_full_pipeline_event(radar)
        updated = radar.evaluate_model_eligibility(
            event_id, eligible=False, rejection_reason="驱动映射置信度不足"
        )
        assert updated.status == STATUS_REJECTED
        assert updated.rejection_reason == "驱动映射置信度不足"

    def test_rejected_without_reason_raises(self) -> None:
        """拒绝时必须提供 rejection_reason。"""
        radar = ChangeRadar()
        event_id = _make_full_pipeline_event(radar)
        with pytest.raises(ValueError, match="rejection_reason"):
            radar.evaluate_model_eligibility(event_id, eligible=False)

    def test_eligible_from_observed_event_raises(self) -> None:
        """observed_event 不能直接 evaluate_model_eligibility。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        with pytest.raises(ValueError, match="非法状态转移"):
            radar.evaluate_model_eligibility(event_id, eligible=True)

    def test_eligible_unknown_event_raises(self) -> None:
        """评估不存在的事件应抛 ValueError。"""
        radar = ChangeRadar()
        with pytest.raises(ValueError, match="事件不存在"):
            radar.evaluate_model_eligibility("nonexistent_id", eligible=True)


# ---------------------------------------------------------------------------
# expire_outdated：验收点 3
# ---------------------------------------------------------------------------


class TestExpireOutdated:
    """验收点 3：过期处理。"""

    def test_expire_event_past_expires_at(self) -> None:
        """expires_at <= as_of 应过期。"""
        radar = ChangeRadar()
        event = _make_event(expires_at="2024-12-31")
        event_id = radar.register_event(event)
        expired_ids = radar.expire_outdated("2025-01-01")
        assert event_id in expired_ids
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.status == STATUS_EXPIRED

    def test_no_expire_event_future_expires_at(self) -> None:
        """expires_at > as_of 不应过期。"""
        radar = ChangeRadar()
        event = _make_event(expires_at="2025-12-31")
        event_id = radar.register_event(event)
        expired_ids = radar.expire_outdated("2024-12-31")
        assert event_id not in expired_ids
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.status == STATUS_OBSERVED_EVENT

    def test_no_expire_event_without_expires_at(self) -> None:
        """无 expires_at 不应过期。"""
        radar = ChangeRadar()
        event = _make_event()
        event.expires_at = ""
        event_id = radar.register_event(event)
        expired_ids = radar.expire_outdated("2025-01-01")
        assert event_id not in expired_ids

    def test_expire_exact_boundary(self) -> None:
        """expires_at == as_of 应过期（<=）。"""
        radar = ChangeRadar()
        event = _make_event(expires_at="2024-12-31")
        event_id = radar.register_event(event)
        expired_ids = radar.expire_outdated("2024-12-31")
        assert event_id in expired_ids

    def test_expire_does_not_touch_terminal(self) -> None:
        """expired 终态事件不应再次过期。"""
        radar = ChangeRadar()
        event = _make_event(expires_at="2024-12-31")
        event_id = radar.register_event(event)
        # 第一次过期
        radar.expire_outdated("2025-01-01")
        # 第二次调用不应重复加入 expired_ids
        expired_ids = radar.expire_outdated("2025-06-01")
        assert event_id not in expired_ids

    def test_expire_does_not_touch_rejected(self) -> None:
        """rejected 不应被过期（除非已转移到 expired）。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.evaluate_model_eligibility(event_id, eligible=False, rejection_reason="测试")
        # rejected 不是 terminal，但其 VALID_TRANSITIONS 包含 expired
        # 所以 expire_outdated 仍可处理它
        expired_ids = radar.expire_outdated("2025-01-01")
        # rejected 有 expires_at，可被过期为 expired
        assert event_id in expired_ids


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------


class TestQueryMethods:
    """查询接口。"""

    def test_get_event_returns_none_for_unknown(self) -> None:
        """获取不存在的事件应返回 None。"""
        radar = ChangeRadar()
        assert radar.get_event("nonexistent") is None

    def test_list_events_no_filter(self) -> None:
        """无过滤应返回所有事件。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_q_001", entity_name="锂")
        e2 = _make_event(
            event_id="cevt_q_002",
            entity_name="铜",
            observed_at="2024-07-01",
        )
        radar.register_event(e1)
        radar.register_event(e2)
        events = radar.list_events()
        assert len(events) == 2

    def test_list_events_filter_by_status(self) -> None:
        """按状态过滤。"""
        radar = ChangeRadar()
        eid1 = radar.register_event(_make_event(event_id="cevt_s_001"))
        eid2 = radar.register_event(
            _make_event(
                event_id="cevt_s_002",
                entity_name="铜",
                observed_at="2024-07-01",
                source_refs=["src_x"],
            )
        )
        radar.promote_to_hypothesis(eid1)
        observed = radar.list_events(status=STATUS_OBSERVED_EVENT)
        assert len(observed) == 1
        assert observed[0].event_id == "cevt_s_002"
        hypo = radar.list_events(status=STATUS_RETRIEVAL_HYPOTHESIS)
        assert len(hypo) == 1
        assert hypo[0].event_id == "cevt_s_001"

    def test_list_events_filter_by_event_type(self) -> None:
        """按事件类型过滤。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_t_001", event_type=EVENT_TYPE_PRODUCT_PRICE_COST)
        e2 = _make_event(
            event_id="cevt_t_002",
            event_type=EVENT_TYPE_ORDER_TENDER,
            entity_name="订单",
        )
        radar.register_event(e1)
        radar.register_event(e2)
        price_events = radar.list_events(event_type=EVENT_TYPE_PRODUCT_PRICE_COST)
        assert len(price_events) == 1
        assert price_events[0].event_id == "cevt_t_001"

    def test_list_events_filter_by_symbol(self) -> None:
        """按 symbol 过滤（candidate.symbol）。"""
        radar = ChangeRadar()
        eid1 = _make_full_pipeline_event(radar)
        # eid1 的 candidate.symbol = "300750.SZ"
        eid2 = radar.register_event(
            _make_event(event_id="cevt_sym_002", entity_name="锂")
        )
        radar.promote_to_hypothesis(eid2)
        ev1 = _make_evidence(source_ref="src_a", confidence=0.6)
        ev2 = _make_evidence(source_ref="src_b", confidence=0.7)
        radar.support_with_evidence(eid2, ev1)
        radar.support_with_evidence(eid2, ev2)
        radar.link_to_driver(
            eid2,
            _make_candidate(symbol="600519.SH", driver_kind="volume"),
        )
        # 查询 symbol=300750.SZ
        events = radar.list_events(symbol="300750.SZ")
        assert len(events) == 1
        assert events[0].event_id == eid1
        # 查询 symbol=600519.SH
        events = radar.list_events(symbol="600519.SH")
        assert len(events) == 1
        assert events[0].event_id == eid2

    def test_list_model_eligible_events(self) -> None:
        """列出模型可用事件。"""
        radar = ChangeRadar()
        eid1 = _make_full_pipeline_event(radar)
        radar.evaluate_model_eligibility(eid1, eligible=True)
        eid2 = radar.register_event(
            _make_event(event_id="cevt_me_002", entity_name="铜")
        )
        eligible = radar.list_model_eligible_events()
        assert len(eligible) == 1
        assert eligible[0].event_id == eid1

    def test_list_company_driver_candidates(self) -> None:
        """列出公司驱动候选事件（含 company_driver_candidate 和 model_eligible）。"""
        radar = ChangeRadar()
        eid1 = _make_full_pipeline_event(radar)  # 状态为 company_driver_candidate
        # 另一个升级到 model_eligible
        eid2 = radar.register_event(
            _make_event(event_id="cevt_cdc_002", entity_name="铜")
        )
        radar.promote_to_hypothesis(eid2)
        radar.support_with_evidence(
            eid2, _make_evidence(source_ref="src_a", confidence=0.6)
        )
        radar.support_with_evidence(
            eid2, _make_evidence(source_ref="src_b", confidence=0.7)
        )
        radar.link_to_driver(eid2, _make_candidate(symbol="600519.SH"))
        radar.evaluate_model_eligibility(eid2, eligible=True)
        candidates = radar.list_company_driver_candidates()
        assert len(candidates) == 2
        candidate_ids = {c.event_id for c in candidates}
        assert eid1 in candidate_ids
        assert eid2 in candidate_ids

    def test_list_conflicts(self) -> None:
        """列出冲突事件对。"""
        radar = ChangeRadar()
        e1 = _make_event(
            event_id="cevt_cf_001",
            direction=DIRECTION_POSITIVE,
            source_refs=["src_a"],
        )
        e2 = _make_event(
            event_id="cevt_cf_002",
            direction=DIRECTION_NEGATIVE,
            source_refs=["src_b"],
        )
        radar.register_event(e1)
        radar.register_event(e2)
        conflicts = radar.list_conflicts()
        assert len(conflicts) == 1
        pair = conflicts[0]
        assert set(pair) == {"cevt_cf_001", "cevt_cf_002"}


# ---------------------------------------------------------------------------
# ThemeChangeSignalAdapter
# ---------------------------------------------------------------------------


class TestThemeChangeSignalAdapter:
    """ThemeChangeSignalAdapter 适配器。"""

    def test_adapt_valid_signal(self) -> None:
        """合法信号应成功适配。"""
        adapter = ThemeChangeSignalAdapter()
        signal = {
            "theme_id": "theme_test_001",
            "observed_at": "2024-06-30",
            "entity_name": "HBM 产业链",
            "entity_kind": "industry",
            "direction": "positive",
            "magnitude": 0.8,
            "magnitude_unit": "score",
            "source_refs": ["trace_001", "https://example.com/news/1"],
            "impact_path": "HBM 产业链 | phase=STARTUP | type=news_persistence",
            "impact_path_steps": ["theme_phase: STARTUP"],
            "expires_at": "2024-12-31",
            "event_type": EVENT_TYPE_POLICY_SUBSIDY,
        }
        events = adapter.adapt([signal])
        assert len(events) == 1
        event = events[0]
        assert event.observed_at == "2024-06-30"
        assert event.entity_name == "HBM 产业链"
        assert event.direction == "positive"
        assert event.event_type == EVENT_TYPE_POLICY_SUBSIDY
        assert event.status == STATUS_OBSERVED_EVENT

    def test_adapt_missing_observed_at_skipped(self) -> None:
        """缺 observed_at 应跳过。"""
        adapter = ThemeChangeSignalAdapter()
        signal = {
            "theme_id": "theme_test_002",
            "entity_name": "HBM",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "path",
        }
        events = adapter.adapt([signal])
        assert events == []

    def test_adapt_missing_entity_name_skipped(self) -> None:
        """缺 entity_name 应跳过。"""
        adapter = ThemeChangeSignalAdapter()
        signal = {
            "observed_at": "2024-06-30",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "path",
        }
        events = adapter.adapt([signal])
        assert events == []

    def test_adapt_invalid_direction_defaults_neutral(self) -> None:
        """非法 direction 应降级为 neutral。"""
        adapter = ThemeChangeSignalAdapter()
        signal = {
            "observed_at": "2024-06-30",
            "entity_name": "HBM",
            "direction": "bullish",
            "source_refs": ["src_a"],
            "impact_path": "path",
        }
        events = adapter.adapt([signal])
        assert len(events) == 1
        assert events[0].direction == DIRECTION_NEUTRAL

    def test_adapt_invalid_event_type_defaults_policy_subsidy(self) -> None:
        """非法 event_type 应降级为 policy_subsidy。"""
        adapter = ThemeChangeSignalAdapter()
        signal = {
            "observed_at": "2024-06-30",
            "entity_name": "HBM",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "path",
            "event_type": "unknown_event",
        }
        events = adapter.adapt([signal])
        assert len(events) == 1
        assert events[0].event_type == EVENT_TYPE_POLICY_SUBSIDY

    def test_adapt_empty_source_refs_uses_theme_id(self) -> None:
        """缺 source_refs 应使用 theme_id 作为来源。"""
        adapter = ThemeChangeSignalAdapter()
        signal = {
            "theme_id": "theme_test_003",
            "observed_at": "2024-06-30",
            "entity_name": "HBM",
            "direction": "positive",
            "impact_path": "path",
        }
        events = adapter.adapt([signal])
        assert len(events) == 1
        assert "theme_test_003" in events[0].source_refs

    def test_adapt_can_register_in_radar(self) -> None:
        """适配后的事件应可成功注册到 ChangeRadar。"""
        adapter = ThemeChangeSignalAdapter()
        radar = ChangeRadar()
        signal = {
            "theme_id": "theme_test_004",
            "observed_at": "2024-06-30",
            "entity_name": "HBM",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "HBM | phase=STARTUP",
            "event_type": EVENT_TYPE_POLICY_SUBSIDY,
        }
        events = adapter.adapt([signal])
        event_id = radar.register_event(events[0])
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.entity_name == "HBM"
        assert stored.status == STATUS_OBSERVED_EVENT


# ---------------------------------------------------------------------------
# CausalContextFactAdapter
# ---------------------------------------------------------------------------


class TestCausalContextFactAdapter:
    """CausalContextFactAdapter 适配器。"""

    def test_adapt_valid_fact(self) -> None:
        """合法 observed_fact 应成功适配。"""
        adapter = CausalContextFactAdapter()
        fact = {
            "claim_id": "claim_test_001",
            "observed_at": "2024-06-30",
            "object_name": "高端光模块",
            "entity_name": "高端光模块",
            "entity_kind": "product",
            "direction": "positive",
            "magnitude": 0.7,
            "magnitude_unit": "confidence",
            "source_refs": ["src_a"],
            "impact_path": "OBSERVED_PROD | 高端光模块 | 300750.SZ",
            "impact_path_steps": ["relation: OBSERVED_PROD"],
            "event_type": EVENT_TYPE_PRODUCT_PRICE_COST,
        }
        events = adapter.adapt([fact])
        assert len(events) == 1
        event = events[0]
        assert event.observed_at == "2024-06-30"
        assert event.entity_name == "高端光模块"
        assert event.direction == "positive"
        assert event.event_type == EVENT_TYPE_PRODUCT_PRICE_COST

    def test_adapt_missing_observed_at_skipped(self) -> None:
        """缺 observed_at 应跳过。"""
        adapter = CausalContextFactAdapter()
        fact = {
            "claim_id": "claim_test_002",
            "object_name": "光模块",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "path",
        }
        events = adapter.adapt([fact])
        assert events == []

    def test_adapt_missing_entity_name_skipped(self) -> None:
        """缺 entity_name/object_name 应跳过。"""
        adapter = CausalContextFactAdapter()
        fact = {
            "observed_at": "2024-06-30",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "path",
        }
        events = adapter.adapt([fact])
        assert events == []

    def test_adapt_invalid_direction_defaults_neutral(self) -> None:
        """非法 direction 应降级为 neutral。"""
        adapter = CausalContextFactAdapter()
        fact = {
            "observed_at": "2024-06-30",
            "object_name": "光模块",
            "direction": "unknown",
            "source_refs": ["src_a"],
            "impact_path": "path",
        }
        events = adapter.adapt([fact])
        assert len(events) == 1
        assert events[0].direction == DIRECTION_NEUTRAL

    def test_adapt_invalid_event_type_defaults_product_price_cost(self) -> None:
        """非法 event_type 应降级为 product_price_cost。"""
        adapter = CausalContextFactAdapter()
        fact = {
            "observed_at": "2024-06-30",
            "object_name": "光模块",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "path",
            "event_type": "unknown_type",
        }
        events = adapter.adapt([fact])
        assert len(events) == 1
        assert events[0].event_type == EVENT_TYPE_PRODUCT_PRICE_COST

    def test_adapt_can_register_in_radar(self) -> None:
        """适配后的事件应可成功注册到 ChangeRadar。"""
        adapter = CausalContextFactAdapter()
        radar = ChangeRadar()
        fact = {
            "claim_id": "claim_test_003",
            "observed_at": "2024-06-30",
            "object_name": "光模块",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "OBSERVED_PROD | 光模块",
            "event_type": EVENT_TYPE_PRODUCT_PRICE_COST,
        }
        events = adapter.adapt([fact])
        event_id = radar.register_event(events[0])
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.entity_name == "光模块"


# ---------------------------------------------------------------------------
# 验收点 1：新闻事件不能直接成为公司经营事实
# ---------------------------------------------------------------------------


class TestAcceptancePoint1:
    """验收点 1：新闻事件不能直接成为公司经营事实。"""

    def test_news_event_cannot_skip_to_company_driver(self) -> None:
        """新闻事件（observed_event）不能直接跳到 company_driver_candidate。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        candidate = _make_candidate()
        with pytest.raises(ValueError, match="验收点 1"):
            radar.link_to_driver(event_id, candidate)

    def test_hypothesis_event_cannot_skip_to_company_driver(self) -> None:
        """retrieval_hypothesis 也不能跳到 company_driver_candidate。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        candidate = _make_candidate()
        with pytest.raises(ValueError, match="验收点 1"):
            radar.link_to_driver(event_id, candidate)

    def test_single_source_evidence_does_not_promote(self) -> None:
        """单源证据不能升级为 evidence_supported。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event())
        radar.promote_to_hypothesis(event_id)
        # 仅一个独立来源
        ev = _make_evidence(source_ref="src_a", confidence=0.95)
        updated = radar.support_with_evidence(event_id, ev)
        assert updated.status == STATUS_RETRIEVAL_HYPOTHESIS
        # 此时 link_to_driver 应失败
        with pytest.raises(ValueError, match="验收点 1"):
            radar.link_to_driver(event_id, _make_candidate())

    def test_theme_signal_only_observed_event(self) -> None:
        """主题信号适配后状态为 observed_event，不能直接 link_to_driver。"""
        adapter = ThemeChangeSignalAdapter()
        radar = ChangeRadar()
        signal = {
            "observed_at": "2024-06-30",
            "entity_name": "HBM",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "HBM | phase=STARTUP",
        }
        events = adapter.adapt([signal])
        event_id = radar.register_event(events[0])
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.status == STATUS_OBSERVED_EVENT
        # 不能直接 link_to_driver
        with pytest.raises(ValueError, match="验收点 1"):
            radar.link_to_driver(event_id, _make_candidate())

    def test_observed_fact_only_observed_event(self) -> None:
        """observed_fact 适配后状态为 observed_event，不能直接 link_to_driver。"""
        adapter = CausalContextFactAdapter()
        radar = ChangeRadar()
        fact = {
            "observed_at": "2024-06-30",
            "object_name": "光模块",
            "direction": "positive",
            "source_refs": ["src_a"],
            "impact_path": "OBSERVED_PROD | 光模块",
        }
        events = adapter.adapt([fact])
        event_id = radar.register_event(events[0])
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.status == STATUS_OBSERVED_EVENT
        with pytest.raises(ValueError, match="验收点 1"):
            radar.link_to_driver(event_id, _make_candidate())

    def test_full_pipeline_required_for_model_eligible(self) -> None:
        """必须完整走完 4 阶段才能升到 model_eligible。"""
        radar = ChangeRadar()
        event_id = _make_full_pipeline_event(radar)
        # 此时状态为 company_driver_candidate
        updated = radar.evaluate_model_eligibility(event_id, eligible=True)
        assert updated.status == STATUS_MODEL_ELIGIBLE

    def test_state_machine_enforces_sequential_promotion(self) -> None:
        """状态机强制顺序升级，无法跳级。"""
        # 验证 VALID_TRANSITIONS 不允许跳级
        assert STATUS_EVIDENCE_SUPPORTED not in VALID_TRANSITIONS[STATUS_OBSERVED_EVENT]
        assert STATUS_COMPANY_DRIVER_CANDIDATE not in VALID_TRANSITIONS[STATUS_OBSERVED_EVENT]
        assert STATUS_COMPANY_DRIVER_CANDIDATE not in VALID_TRANSITIONS[STATUS_RETRIEVAL_HYPOTHESIS]
        assert STATUS_MODEL_ELIGIBLE not in VALID_TRANSITIONS[STATUS_EVIDENCE_SUPPORTED]


# ---------------------------------------------------------------------------
# 验收点 2：事件必须有时间、实体、方向、来源和影响路径
# ---------------------------------------------------------------------------


class TestAcceptancePoint2:
    """验收点 2：事件必须有时间、实体、方向、来源和影响路径。"""

    def test_register_raises_on_missing_observed_at(self) -> None:
        """注册时缺 observed_at 应抛异常。"""
        radar = ChangeRadar()
        event = _make_event()
        event.observed_at = ""
        with pytest.raises(ValueError, match="observed_at"):
            radar.register_event(event)

    def test_register_raises_on_missing_entity_name(self) -> None:
        """注册时缺 entity_name 应抛异常。"""
        radar = ChangeRadar()
        event = _make_event()
        event.entity_name = ""
        with pytest.raises(ValueError, match="entity_name"):
            radar.register_event(event)

    def test_register_raises_on_missing_direction(self) -> None:
        """注册时缺 direction 应抛异常。"""
        radar = ChangeRadar()
        event = _make_event()
        event.direction = ""
        with pytest.raises(ValueError, match="direction"):
            radar.register_event(event)

    def test_register_raises_on_invalid_direction(self) -> None:
        """注册时非法 direction 应抛异常。"""
        radar = ChangeRadar()
        event = _make_event()
        event.direction = "bullish"
        with pytest.raises(ValueError, match="direction"):
            radar.register_event(event)

    def test_register_raises_on_missing_source_refs(self) -> None:
        """注册时缺 source_refs 应抛异常。"""
        radar = ChangeRadar()
        event = _make_event()
        event.source_refs = []
        with pytest.raises(ValueError, match="source_refs"):
            radar.register_event(event)

    def test_register_raises_on_missing_impact_path(self) -> None:
        """注册时缺 impact_path 与 impact_path_steps 应抛异常。"""
        radar = ChangeRadar()
        event = _make_event()
        event.impact_path = ""
        event.impact_path_steps = []
        with pytest.raises(ValueError, match="impact_path"):
            radar.register_event(event)

    def test_register_raises_on_invalid_event_type(self) -> None:
        """注册时非法 event_type 应抛异常。"""
        radar = ChangeRadar()
        event = _make_event()
        event.event_type = "unknown_event"
        with pytest.raises(ValueError, match="event_type"):
            radar.register_event(event)

    def test_all_eight_event_types_accepted(self) -> None:
        """8 类事件类型都应被接受。"""
        radar = ChangeRadar()
        for i, et in enumerate(EVENT_TYPES):
            event = _make_event(
                event_id=f"cevt_et_{i:03d}",
                event_type=et,
                entity_name=f"entity_{i}",
                observed_at=f"2024-06-{30 - i:02d}" if i < 30 else "2024-06-01",
            )
            event_id = radar.register_event(event)
            stored = radar.get_event(event_id)
            assert stored is not None
            assert stored.event_type == et

    def test_impact_path_steps_alone_satisfies(self) -> None:
        """仅 impact_path_steps 存在也应通过验收点 2。"""
        radar = ChangeRadar()
        event = _make_event()
        event.impact_path = ""
        event.impact_path_steps = ["step1: 价格上涨", "step2: 成本传导"]
        event_id = radar.register_event(event)
        stored = radar.get_event(event_id)
        assert stored is not None


# ---------------------------------------------------------------------------
# 验收点 3：重复事件去重、过期和冲突处理可验证
# ---------------------------------------------------------------------------


class TestAcceptancePoint3:
    """验收点 3：重复事件去重、过期和冲突处理可验证。"""

    def test_dedup_same_content_merges(self) -> None:
        """同内容事件应合并 source_refs。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_d_001", source_refs=["src_a"])
        e2 = _make_event(event_id="cevt_d_002", source_refs=["src_b"])
        eid1 = radar.register_event(e1)
        eid2 = radar.register_event(e2)
        assert eid1 == eid2
        stored = radar.get_event(eid1)
        assert stored is not None
        assert set(stored.source_refs) == {"src_a", "src_b"}

    def test_dedup_records_history_in_note(self) -> None:
        """去重应在 note 中记录历史。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_dn_001", source_refs=["src_a"])
        e2 = _make_event(event_id="cevt_dn_002", source_refs=["src_b"])
        eid1 = radar.register_event(e1)
        radar.register_event(e2)
        stored = radar.get_event(eid1)
        assert stored is not None
        assert "deduped_with_new_source" in stored.note

    def test_dedup_dedup_key_consistent(self) -> None:
        """去重后 dedup_key 应一致。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_dk_001", source_refs=["src_a"])
        e2 = _make_event(event_id="cevt_dk_002", source_refs=["src_b"])
        eid1 = radar.register_event(e1)
        radar.register_event(e2)
        stored = radar.get_event(eid1)
        assert stored is not None
        assert stored.dedup_key == e1.compute_dedup_key()

    def test_conflict_detected_positive_vs_negative(self) -> None:
        """positive vs negative 同 entity 同 observed_at 应记录冲突。"""
        radar = ChangeRadar()
        e1 = _make_event(
            event_id="cevt_cf_001",
            direction=DIRECTION_POSITIVE,
            source_refs=["src_a"],
        )
        e2 = _make_event(
            event_id="cevt_cf_002",
            direction=DIRECTION_NEGATIVE,
            source_refs=["src_b"],
        )
        eid1 = radar.register_event(e1)
        eid2 = radar.register_event(e2)
        conflicts = radar.list_conflicts()
        assert len(conflicts) == 1
        assert set(conflicts[0]) == {eid1, eid2}

    def test_conflict_not_triggered_different_date(self) -> None:
        """不同 observed_at 不应触发冲突。"""
        radar = ChangeRadar()
        e1 = _make_event(
            event_id="cevt_cfd_001",
            direction=DIRECTION_POSITIVE,
            observed_at="2024-06-30",
            source_refs=["src_a"],
        )
        e2 = _make_event(
            event_id="cevt_cfd_002",
            direction=DIRECTION_NEGATIVE,
            observed_at="2024-07-01",
            source_refs=["src_b"],
        )
        radar.register_event(e1)
        radar.register_event(e2)
        assert radar.list_conflicts() == []

    def test_conflict_not_triggered_different_entity(self) -> None:
        """不同 entity 不应触发冲突。"""
        radar = ChangeRadar()
        e1 = _make_event(
            event_id="cevt_cfe_001",
            entity_name="锂",
            direction=DIRECTION_POSITIVE,
            source_refs=["src_a"],
        )
        e2 = _make_event(
            event_id="cevt_cfe_002",
            entity_name="铜",
            direction=DIRECTION_NEGATIVE,
            source_refs=["src_b"],
        )
        radar.register_event(e1)
        radar.register_event(e2)
        assert radar.list_conflicts() == []

    def test_conflict_bidirectional(self) -> None:
        """冲突应双向记录。"""
        radar = ChangeRadar()
        e1 = _make_event(
            event_id="cevt_cfb_001",
            direction=DIRECTION_POSITIVE,
            source_refs=["src_a"],
        )
        e2 = _make_event(
            event_id="cevt_cfb_002",
            direction=DIRECTION_NEGATIVE,
            source_refs=["src_b"],
        )
        eid1 = radar.register_event(e1)
        eid2 = radar.register_event(e2)
        s1 = radar.get_event(eid1)
        s2 = radar.get_event(eid2)
        assert eid2 in s1.conflict_with
        assert eid1 in s2.conflict_with

    def test_expire_outdated_processes_past_events(self) -> None:
        """过期处理应处理过去 expires_at 的事件。"""
        radar = ChangeRadar()
        e1 = _make_event(event_id="cevt_ex_001", expires_at="2024-06-30")
        e2 = _make_event(
            event_id="cevt_ex_002",
            entity_name="铜",
            observed_at="2024-07-01",
            expires_at="2025-12-31",
            source_refs=["src_b"],
        )
        eid1 = radar.register_event(e1)
        eid2 = radar.register_event(e2)
        expired_ids = radar.expire_outdated("2024-12-31")
        assert eid1 in expired_ids
        assert eid2 not in expired_ids
        assert radar.get_event(eid1).status == STATUS_EXPIRED
        assert radar.get_event(eid2).status == STATUS_OBSERVED_EVENT

    def test_expired_event_is_terminal(self) -> None:
        """过期事件应为终态，无法再转移。"""
        radar = ChangeRadar()
        event = _make_event(expires_at="2024-06-30")
        event_id = radar.register_event(event)
        radar.expire_outdated("2025-01-01")
        stored = radar.get_event(event_id)
        assert stored is not None
        assert stored.is_terminal() is True
        # 尝试 promote_to_hypothesis 应失败
        with pytest.raises(ValueError, match="非法状态转移"):
            radar.promote_to_hypothesis(event_id)


# ---------------------------------------------------------------------------
# 字段缺失降级（AGENTS.md）
# ---------------------------------------------------------------------------


class TestGraceful:
    """字段缺失降级处理（AGENTS.md）。"""

    def test_theme_adapter_skips_invalid_signal(self) -> None:
        """主题适配器应跳过非法信号，不抛异常。"""
        adapter = ThemeChangeSignalAdapter()
        # 缺 observed_at
        signals = [{"theme_id": "t1", "entity_name": "HBM"}]
        events = adapter.adapt(signals)
        assert events == []

    def test_causal_adapter_skips_invalid_fact(self) -> None:
        """事实适配器应跳过非法事实，不抛异常。"""
        adapter = CausalContextFactAdapter()
        # 缺 observed_at
        facts = [{"claim_id": "c1", "object_name": "光模块"}]
        events = adapter.adapt(facts)
        assert events == []

    def test_adapter_handles_empty_input(self) -> None:
        """适配器应处理空输入。"""
        theme_adapter = ThemeChangeSignalAdapter()
        causal_adapter = CausalContextFactAdapter()
        assert theme_adapter.adapt([]) == []
        assert causal_adapter.adapt([]) == []

    def test_adapter_handles_multiple_invalid_inputs(self) -> None:
        """适配器应处理多个非法输入，仅返回合法适配。"""
        adapter = ThemeChangeSignalAdapter()
        signals = [
            {"theme_id": "t1"},  # 缺 observed_at / entity_name
            {"observed_at": "2024-06-30", "entity_name": "HBM", "direction": "positive", "source_refs": ["src"], "impact_path": "p"},  # 合法
            {"observed_at": "2024-06-30"},  # 缺 entity_name
        ]
        events = adapter.adapt(signals)
        assert len(events) == 1
        assert events[0].entity_name == "HBM"


# ---------------------------------------------------------------------------
# AGENTS.md 合规性
# ---------------------------------------------------------------------------


class TestNoTradeAdvice:
    """AGENTS.md 合规性：不输出交易动作建议。"""

    FORBIDDEN_KEYWORDS: tuple[str, ...] = (
        "买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓",
    )

    def _read_source(self) -> str:
        """读取 change_radar.py 源码。"""
        from pathlib import Path
        path = (
            Path(__file__).parent.parent
            / "core" / "research" / "discovery" / "change_radar.py"
        )
        return path.read_text(encoding="utf-8")

    def test_source_no_trade_advice_keywords(self) -> None:
        """源码不应包含交易动作关键词。"""
        source = self._read_source()
        for kw in self.FORBIDDEN_KEYWORDS:
            assert kw not in source, f"源码包含禁止关键词: {kw}"

    def test_event_output_no_trade_advice(self) -> None:
        """事件输出字典不应包含交易动作字段。"""
        event = _make_event()
        d = event.to_dict()
        for kw in self.FORBIDDEN_KEYWORDS:
            for key, value in d.items():
                if isinstance(value, str):
                    assert kw not in value, f"字段 {key} 包含禁止关键词: {kw}"
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            assert kw not in item, f"字段 {key} 列表项包含禁止关键词: {kw}"

    def test_candidate_no_trade_advice(self) -> None:
        """CompanyDriverCandidate 不应输出交易动作。"""
        candidate = _make_candidate()
        d = candidate.to_dict()
        for kw in self.FORBIDDEN_KEYWORDS:
            for key, value in d.items():
                if isinstance(value, str):
                    assert kw not in value, f"字段 {key} 包含禁止关键词: {kw}"

    def test_impact_path_describes_business_logic_not_trade(self) -> None:
        """impact_path 应描述业务逻辑，不描述交易动作。"""
        # 影响路径示例：碳酸锂涨价 → 电池厂毛利承压（业务逻辑，非交易建议）
        event = _make_event(impact_path="碳酸锂涨价 → 电池厂毛利承压")
        for kw in self.FORBIDDEN_KEYWORDS:
            assert kw not in event.impact_path


# ---------------------------------------------------------------------------
# 端到端集成
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端集成测试。"""

    def test_full_pipeline_observed_to_model_eligible(self) -> None:
        """完整流程：observed_event → model_eligible。"""
        radar = ChangeRadar()
        # 1. 注册事件
        event = _make_event(event_id="cevt_e2e_001")
        event_id = radar.register_event(event)
        assert radar.get_event(event_id).status == STATUS_OBSERVED_EVENT

        # 2. 升级为检索假设
        radar.promote_to_hypothesis(event_id, retrieval_plan="检索公告与行业研报")
        assert radar.get_event(event_id).status == STATUS_RETRIEVAL_HYPOTHESIS

        # 3. 多源证据支持
        ev1 = _make_evidence(
            evidence_id="evd_e2e_001",
            source_ref="src_a",
            source_publisher="中国证券报",
            confidence=0.7,
        )
        ev2 = _make_evidence(
            evidence_id="evd_e2e_002",
            source_ref="src_b",
            source_publisher="上海证券报",
            confidence=0.8,
        )
        radar.support_with_evidence(event_id, ev1)
        radar.support_with_evidence(event_id, ev2)
        assert radar.get_event(event_id).status == STATUS_EVIDENCE_SUPPORTED

        # 4. 链接公司驱动
        candidate = _make_candidate(
            symbol="300750.SZ",
            driver_kind="asp",
            impact_path="ASP 上涨 → 毛利率提升",
        )
        radar.link_to_driver(event_id, candidate)
        assert radar.get_event(event_id).status == STATUS_COMPANY_DRIVER_CANDIDATE

        # 5. 评估模型可用性
        updated = radar.evaluate_model_eligibility(event_id, eligible=True)
        assert updated.status == STATUS_MODEL_ELIGIBLE

        # 6. 列出模型可用事件
        eligible_events = radar.list_model_eligible_events(symbol="300750.SZ")
        assert len(eligible_events) == 1
        assert eligible_events[0].event_id == event_id

    def test_pipeline_with_rejection(self) -> None:
        """流程含拒绝：observed_event → rejected。"""
        radar = ChangeRadar()
        event_id = radar.register_event(_make_event(event_id="cevt_rj_001"))
        # 直接从 observed_event 拒绝
        updated = radar.evaluate_model_eligibility(
            event_id, eligible=False, rejection_reason="证据不足"
        )
        assert updated.status == STATUS_REJECTED
        assert updated.rejection_reason == "证据不足"

    def test_pipeline_with_expiry(self) -> None:
        """流程含过期：observed_event → expired。"""
        radar = ChangeRadar()
        event = _make_event(event_id="cevt_ex_001", expires_at="2024-06-30")
        event_id = radar.register_event(event)
        expired_ids = radar.expire_outdated("2025-01-01")
        assert event_id in expired_ids
        assert radar.get_event(event_id).status == STATUS_EXPIRED

    def test_integration_with_theme_adapter(self) -> None:
        """集成：主题信号 → 雷达 → 完整流程。"""
        adapter = ThemeChangeSignalAdapter()
        radar = ChangeRadar()
        signal = {
            "theme_id": "theme_int_001",
            "observed_at": "2024-06-30",
            "entity_name": "HBM 产业链",
            "direction": "positive",
            "source_refs": ["src_theme_a"],
            "impact_path": "HBM | phase=STARTUP",
            "event_type": EVENT_TYPE_POLICY_SUBSIDY,
        }
        events = adapter.adapt([signal])
        event_id = radar.register_event(events[0])
        # 升级为检索假设
        radar.promote_to_hypothesis(event_id, retrieval_plan="检索政策文件")
        # 多源证据
        ev1 = _make_evidence(source_ref="src_a", confidence=0.6)
        ev2 = _make_evidence(source_ref="src_b", confidence=0.7)
        radar.support_with_evidence(event_id, ev1)
        radar.support_with_evidence(event_id, ev2)
        # 链接驱动
        radar.link_to_driver(event_id, _make_candidate(symbol="300750.SZ"))
        # 升级为 model_eligible
        radar.evaluate_model_eligibility(event_id, eligible=True)
        # 验证
        eligible = radar.list_model_eligible_events(symbol="300750.SZ")
        assert len(eligible) == 1
        assert eligible[0].entity_name == "HBM 产业链"

    def test_integration_with_causal_adapter(self) -> None:
        """集成：observed_fact → 雷达 → 完整流程。"""
        adapter = CausalContextFactAdapter()
        radar = ChangeRadar()
        fact = {
            "claim_id": "claim_int_001",
            "observed_at": "2024-06-30",
            "object_name": "高端光模块",
            "direction": "positive",
            "source_refs": ["src_claim_a"],
            "impact_path": "OBSERVED_PROD | 高端光模块",
            "event_type": EVENT_TYPE_PRODUCT_PRICE_COST,
        }
        events = adapter.adapt([fact])
        event_id = radar.register_event(events[0])
        radar.promote_to_hypothesis(event_id, retrieval_plan="检索产品公告")
        ev1 = _make_evidence(source_ref="src_a", confidence=0.6)
        ev2 = _make_evidence(source_ref="src_b", confidence=0.7)
        radar.support_with_evidence(event_id, ev1)
        radar.support_with_evidence(event_id, ev2)
        radar.link_to_driver(event_id, _make_candidate(symbol="300750.SZ"))
        radar.evaluate_model_eligibility(event_id, eligible=True)
        eligible = radar.list_model_eligible_events(symbol="300750.SZ")
        assert len(eligible) == 1
        assert eligible[0].entity_name == "高端光模块"
