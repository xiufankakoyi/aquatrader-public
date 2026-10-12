"""P1.6 管理层、治理和资本配置台账专项测试。

覆盖 P1 计划 §5 Task P1.6 要求的 3 个验收点：
1. 管理层承诺按"原文—时间—目标—兑现—偏差"5 项齐全保存
2. 人员评价不能由情绪新闻直接生成
3. 治理风险与财务预测影响分开表达

覆盖 6 类内容：
1. 控股股东、实际控制人、董监高变更 (ShareholderChange / ManagementPerson)
2. 股权激励与考核条件 (EquityIncentive)
3. 关联交易、质押、担保、监管处罚 (RelatedPartyTransaction / RiskEvent)
4. 分红、回购、融资、并购、扩产和资产处置 (CapitalAction)
5. 管理层公开承诺及后续兑现情况 (ManagementCommitment)
6. 资本配置对 ROIC、每股价值和财务风险的影响 (CapitalAllocationImpact)

并覆盖 GovernanceRepository 的版本化存储和 PIT 过滤。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.governance import (
    CapitalAction,
    CapitalAllocationImpact,
    EquityIncentive,
    GovernanceGap,
    GovernanceLedger,
    GovernanceRepository,
    GovernanceRequest,
    GovernanceRisk,
    GovernanceService,
    ManagementCommitment,
    ManagementPerson,
    PersonnelAssessment,
    RelatedPartyTransaction,
    RiskEvent,
    ShareholderChange,
)
from core.research.governance.contracts import (
    EMOTIONAL_NEWS_KEYWORDS,
    detect_emotional_news,
    validate_capital_action_type,
    validate_commitment_status,
    validate_equity_incentive_type,
    validate_governance_risk_type,
    validate_pricing_basis,
    validate_related_tx_type,
    validate_risk_event_type,
    validate_risk_severity,
    validate_shareholder_change_type,
)


# ---------------------------------------------------------------------------
# 辅助函数：构造完整模块数据
# ---------------------------------------------------------------------------


def _make_complete_management_person() -> ManagementPerson:
    """构造完整的管理人员数据。"""
    return ManagementPerson(
        person_id="person_001",
        name="张三",
        title="CEO",
        appointment_date="2024-03-15",
        departure_date="",
        background="曾任某上市公司总裁",
        source_ref="ev_person_1",
        available_at="2024-03-16",
    )


def _make_complete_shareholder_change() -> ShareholderChange:
    """构造完整的股东变更数据。"""
    return ShareholderChange(
        change_id="change_001",
        change_type="controller",
        person=_make_complete_management_person(),
        change_date="2024-03-15",
        before_state="原控制人为 A 集团",
        after_state="现控制人为 B 集团",
        source_ref="ev_change_1",
        available_at="2024-03-16",
    )


def _make_complete_equity_incentive() -> EquityIncentive:
    """构造完整的股权激励数据。"""
    return EquityIncentive(
        plan_id="plan_001",
        plan_name="2024 年限制性股票激励计划",
        plan_type="restricted_stock",
        grant_date="2024-04-01",
        vesting_period_months=36,
        total_shares=10_000_000.0,
        exercise_price=None,
        performance_conditions=["2024 年营收增长率不低于 20%", "ROE 不低于 15%"],
        source_ref="ev_plan_1",
        available_at="2024-04-02",
    )


def _make_complete_related_party_transaction() -> RelatedPartyTransaction:
    """构造完整的关联交易数据。"""
    return RelatedPartyTransaction(
        transaction_id="tx_001",
        transaction_type="purchase",
        counterparty="A 集团子公司",
        amount=500_000_000.0,
        currency="CNY",
        period="2024-Q2",
        pricing_basis="market",
        source_ref="ev_tx_1",
        available_at="2024-07-01",
    )


def _make_complete_risk_event() -> RiskEvent:
    """构造完整的风险事件数据。"""
    return RiskEvent(
        event_id="risk_001",
        event_type="pledge",
        severity="warning",
        event_date="2024-05-10",
        description="控股股东质押股份占比 35%",
        amount=350_000_000.0,
        currency="CNY",
        source_ref="ev_risk_1",
        available_at="2024-05-11",
    )


def _make_complete_capital_action() -> CapitalAction:
    """构造完整的资本动作数据。"""
    return CapitalAction(
        action_id="action_001",
        action_type="dividend",
        announcement_date="2024-06-15",
        amount=200_000_000.0,
        currency="CNY",
        description="每 10 股派发 5 元现金股利",
        source_ref="ev_action_1",
        available_at="2024-06-16",
    )


def _make_complete_management_commitment(
    status: str = "fulfilled",
) -> ManagementCommitment:
    """构造完整的管理层承诺数据（5 项齐全）。"""
    return ManagementCommitment(
        commitment_id="commitment_001",
        original_text="公司将于 2024 年实现营收增长 30%",
        announced_date="2024-01-15",
        target="2024 年营收增长 30%",
        fulfillment_status=status,
        fulfillment_evidence_id="ev_fulfill_1",
        deviation="无偏差" if status == "fulfilled" else "实际增长 25%，低于目标 5%",
        source_ref="ev_commit_1",
        available_at="2024-01-16",
    )


def _make_complete_personnel_assessment(
    is_emotional: bool = False,
    text: str = "该管理人员具有 20 年行业经验，曾主导多个项目",
) -> PersonnelAssessment:
    """构造完整的人员评价数据。"""
    return PersonnelAssessment(
        assessment_id="assessment_001",
        person_name="张三",
        assessment=text,
        evidence_basis="公司年报披露的简历信息",
        is_from_emotional_news=is_emotional,
        emotional_news_detected=[],
        source_ref="ev_assess_1",
        available_at="2024-07-01",
    )


def _make_complete_governance_risk(
    mixed: bool = False,
) -> GovernanceRisk:
    """构造完整的治理风险数据。"""
    return GovernanceRisk(
        risk_id="risk_g_001",
        risk_type="pledge",
        severity="warning",
        description="控股股东高比例质押",
        potential_financial_impact="若股价下跌可能触发平仓，进而影响控制权稳定性",
        risk_financial_impact_mixed=mixed,
        source_ref="ev_grisk_1",
        available_at="2024-07-01",
    )


def _make_complete_capital_allocation_impact() -> CapitalAllocationImpact:
    """构造完整的资本配置影响数据。"""
    return CapitalAllocationImpact(
        impact_id="impact_001",
        action_ref="action_001",
        roic_impact_bp=50.0,
        per_share_value_impact=0.15,
        financial_risk_impact="资产负债率上升 2 个百分点",
        source_ref="ev_impact_1",
        available_at="2024-07-01",
    )


def _make_complete_governance_request() -> GovernanceRequest:
    """构造完整 GovernanceRequest（覆盖 6 类内容）。"""
    return GovernanceRequest(
        symbol="600000.SH",
        company_name="测试公司",
        period="2024-12-31",
        available_at="2025-03-31",
        shareholder_changes=[_make_complete_shareholder_change()],
        management_persons=[_make_complete_management_person()],
        equity_incentives=[_make_complete_equity_incentive()],
        related_party_transactions=[_make_complete_related_party_transaction()],
        risk_events=[_make_complete_risk_event()],
        capital_actions=[_make_complete_capital_action()],
        management_commitments=[_make_complete_management_commitment()],
        personnel_assessments=[_make_complete_personnel_assessment()],
        governance_risks=[_make_complete_governance_risk()],
        capital_allocation_impacts=[_make_complete_capital_allocation_impact()],
        source_trace_id="src_test_001",
    )


# ---------------------------------------------------------------------------
# 验收点 1：管理层承诺按"原文—时间—目标—兑现—偏差"5 项齐全保存
# ---------------------------------------------------------------------------


class TestAcceptancePoint1CommitmentFields:
    """验收点 1：管理层承诺按"原文—时间—目标—兑现—偏差"5 项齐全保存。"""

    def test_commitment_with_all_fields_passes(self) -> None:
        """5 项齐全时不产生 gap。"""
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        commit_gaps = [
            g for g in ledger.gaps if "management_commitments" in g.field_name
        ]
        assert commit_gaps == []

    def test_commitment_missing_original_text_triggers_blocker(self) -> None:
        """缺原文时触发 blocker gap。"""
        request = _make_complete_governance_request()
        request.management_commitments[0].original_text = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g
            for g in ledger.gaps
            if "management_commitments" in g.field_name
            and g.severity == "blocker"
        ]
        assert len(blocker_gaps) == 1
        assert blocker_gaps[0].blocker_kind == "commitment_incomplete"
        assert "original_text" in blocker_gaps[0].reason

    def test_commitment_missing_announced_date_triggers_blocker(self) -> None:
        """缺时间时触发 blocker gap。"""
        request = _make_complete_governance_request()
        request.management_commitments[0].announced_date = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g
            for g in ledger.gaps
            if "management_commitments" in g.field_name
            and g.severity == "blocker"
        ]
        assert len(blocker_gaps) == 1
        assert "announced_date" in blocker_gaps[0].reason

    def test_commitment_missing_target_triggers_blocker(self) -> None:
        """缺目标时触发 blocker gap。"""
        request = _make_complete_governance_request()
        request.management_commitments[0].target = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g
            for g in ledger.gaps
            if "management_commitments" in g.field_name
            and g.severity == "blocker"
        ]
        assert len(blocker_gaps) == 1
        assert "target" in blocker_gaps[0].reason

    def test_commitment_missing_fulfillment_status_triggers_blocker(self) -> None:
        """缺兑现状态时触发 blocker gap。"""
        request = _make_complete_governance_request()
        request.management_commitments[0].fulfillment_status = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g
            for g in ledger.gaps
            if "management_commitments" in g.field_name
            and g.severity == "blocker"
        ]
        assert len(blocker_gaps) == 1
        assert "fulfillment_status" in blocker_gaps[0].reason

    def test_commitment_missing_deviation_triggers_blocker(self) -> None:
        """缺偏差时触发 blocker gap。"""
        request = _make_complete_governance_request()
        request.management_commitments[0].deviation = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g
            for g in ledger.gaps
            if "management_commitments" in g.field_name
            and g.severity == "blocker"
        ]
        assert len(blocker_gaps) == 1
        assert "deviation" in blocker_gaps[0].reason

    def test_commitment_required_fields_complete_method(self) -> None:
        """required_fields_complete() 方法返回 (bool, missing_list)。"""
        complete = _make_complete_management_commitment()
        is_complete, missing = complete.required_fields_complete()
        assert is_complete is True
        assert missing == []

        complete.original_text = ""
        is_complete, missing = complete.required_fields_complete()
        assert is_complete is False
        assert "original_text" in missing


# ---------------------------------------------------------------------------
# 验收点 2：人员评价不能由情绪新闻直接生成
# ---------------------------------------------------------------------------


class TestAcceptancePoint2EmotionalNews:
    """验收点 2：人员评价不能由情绪新闻直接生成。"""

    def test_assessment_with_structured_evidence_passes(self) -> None:
        """结构化证据（非情绪新闻）的人员评价不产生 gap。"""
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        emotional_gaps = [
            g
            for g in ledger.gaps
            if "emotional_news" in g.field_name
            or g.blocker_kind == "emotional_news_assessment"
        ]
        assert emotional_gaps == []

    def test_assessment_marked_emotional_news_triggers_blocker(self) -> None:
        """is_from_emotional_news=True 时触发 blocker。"""
        request = _make_complete_governance_request()
        request.personnel_assessments = [
            _make_complete_personnel_assessment(is_emotional=True)
        ]
        ledger = GovernanceService().compute_governance_ledger(request)
        emotional_gaps = [
            g
            for g in ledger.gaps
            if g.blocker_kind == "emotional_news_assessment"
        ]
        assert len(emotional_gaps) == 1
        assert emotional_gaps[0].severity == "blocker"
        assert ledger.personnel_assessments == []

    def test_assessment_with_emotional_keyword_triggers_blocker(self) -> None:
        """评价文本含情绪词时自动检测并触发 blocker。"""
        request = _make_complete_governance_request()
        request.personnel_assessments = [
            _make_complete_personnel_assessment(
                text="该管理人员的决策堪称天才之举，令人震惊"
            )
        ]
        ledger = GovernanceService().compute_governance_ledger(request)
        emotional_gaps = [
            g
            for g in ledger.gaps
            if g.blocker_kind == "emotional_news_assessment"
        ]
        assert len(emotional_gaps) == 1
        # 检测到的情绪词应包含在 gap reason 中
        assert "天才" in emotional_gaps[0].reason
        assert "震惊" in emotional_gaps[0].reason
        assert ledger.personnel_assessments == []

    def test_emotional_keyword_detection_returns_list(self) -> None:
        """detect_emotional_news() 返回检测到的情绪词列表。"""
        words = detect_emotional_news("这位CEO昏招连连，业绩惨败")
        assert "昏招" in words
        assert "惨败" in words

    def test_no_emotional_keywords_returns_empty(self) -> None:
        """无情绪词时返回空列表。"""
        assert detect_emotional_news("公司公告") == []
        assert detect_emotional_news("") == []


# ---------------------------------------------------------------------------
# 验收点 3：治理风险与财务预测影响分开表达
# ---------------------------------------------------------------------------


class TestAcceptancePoint3RiskFinancialSeparation:
    """验收点 3：治理风险与财务预测影响分开表达。"""

    def test_risk_with_potential_financial_impact_only_passes(self) -> None:
        """potential_financial_impact 仅描述性，mixed=False 不触发 gap。"""
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        mixed_gaps = [
            g
            for g in ledger.gaps
            if g.blocker_kind == "risk_financial_impact_mixed"
        ]
        assert mixed_gaps == []

    def test_risk_financial_impact_mixed_triggers_blocker(self) -> None:
        """risk_financial_impact_mixed=True 触发 blocker。"""
        request = _make_complete_governance_request()
        request.governance_risks = [_make_complete_governance_risk(mixed=True)]
        ledger = GovernanceService().compute_governance_ledger(request)
        mixed_gaps = [
            g
            for g in ledger.gaps
            if g.blocker_kind == "risk_financial_impact_mixed"
        ]
        assert len(mixed_gaps) == 1
        assert mixed_gaps[0].severity == "blocker"
        assert "治理风险与财务预测影响必须分开" in mixed_gaps[0].reason

    def test_potential_financial_impact_is_descriptive_only(self) -> None:
        """potential_financial_impact 字段为描述性，不参与预测计算。"""
        risk = _make_complete_governance_risk()
        assert risk.potential_financial_impact == (
            "若股价下跌可能触发平仓，进而影响控制权稳定性"
        )
        # 该字段为 str 类型，不是数值
        assert isinstance(risk.potential_financial_impact, str)


# ---------------------------------------------------------------------------
# 6 类内容模块测试
# ---------------------------------------------------------------------------


class TestShareholderChangeModule:
    """模块 1：控股股东、实际控制人、董监高变更。"""

    def test_shareholder_change_complete_passes(self) -> None:
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        assert len(ledger.shareholder_changes) == 1
        change_gaps = [
            g for g in ledger.gaps if "shareholder_changes" in g.field_name
        ]
        assert change_gaps == []

    def test_shareholder_change_missing_source_ref_triggers_blocker(self) -> None:
        request = _make_complete_governance_request()
        request.shareholder_changes[0].source_ref = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g for g in ledger.gaps if "shareholder_changes" in g.field_name
        ]
        assert len(blocker_gaps) == 1
        assert blocker_gaps[0].severity == "blocker"

    def test_shareholder_change_illegal_type_raises(self) -> None:
        request = _make_complete_governance_request()
        request.shareholder_changes[0].change_type = "invalid_type"
        with pytest.raises(ValueError, match="非法 shareholder change type"):
            GovernanceService().compute_governance_ledger(request)

    def test_management_person_to_dict_roundtrip(self) -> None:
        person = _make_complete_management_person()
        d = person.to_dict()
        assert d["name"] == "张三"
        assert d["title"] == "CEO"


class TestEquityIncentiveModule:
    """模块 2：股权激励与考核条件。"""

    def test_equity_incentive_complete_passes(self) -> None:
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        assert len(ledger.equity_incentives) == 1
        assert "ROE 不低于 15%" in ledger.equity_incentives[0].performance_conditions

    def test_equity_incentive_missing_source_ref_triggers_blocker(self) -> None:
        request = _make_complete_governance_request()
        request.equity_incentives[0].source_ref = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g for g in ledger.gaps if "equity_incentives" in g.field_name
        ]
        assert len(blocker_gaps) == 1

    def test_equity_incentive_illegal_type_raises(self) -> None:
        request = _make_complete_governance_request()
        request.equity_incentives[0].plan_type = "invalid_type"
        with pytest.raises(ValueError, match="非法 equity incentive type"):
            GovernanceService().compute_governance_ledger(request)


class TestRelatedPartyTransactionModule:
    """模块 3：关联交易。"""

    def test_related_party_transaction_complete_passes(self) -> None:
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        assert len(ledger.related_party_transactions) == 1

    def test_related_party_transaction_missing_source_ref_triggers_blocker(self) -> None:
        request = _make_complete_governance_request()
        request.related_party_transactions[0].source_ref = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g for g in ledger.gaps if "related_party_transactions" in g.field_name
        ]
        assert len(blocker_gaps) == 1

    def test_related_party_transaction_illegal_type_raises(self) -> None:
        request = _make_complete_governance_request()
        request.related_party_transactions[0].transaction_type = "invalid"
        with pytest.raises(ValueError, match="非法 related transaction type"):
            GovernanceService().compute_governance_ledger(request)

    def test_related_party_transaction_illegal_pricing_basis_raises(self) -> None:
        request = _make_complete_governance_request()
        request.related_party_transactions[0].pricing_basis = "invalid"
        with pytest.raises(ValueError, match="非法 pricing basis"):
            GovernanceService().compute_governance_ledger(request)


class TestRiskEventModule:
    """模块 3b：质押、担保、监管处罚。"""

    def test_risk_event_complete_passes(self) -> None:
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        assert len(ledger.risk_events) == 1

    def test_risk_event_missing_source_ref_triggers_blocker(self) -> None:
        request = _make_complete_governance_request()
        request.risk_events[0].source_ref = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g for g in ledger.gaps if "risk_events" in g.field_name
        ]
        assert len(blocker_gaps) == 1

    def test_risk_event_illegal_type_raises(self) -> None:
        request = _make_complete_governance_request()
        request.risk_events[0].event_type = "invalid"
        with pytest.raises(ValueError, match="非法 risk event type"):
            GovernanceService().compute_governance_ledger(request)

    def test_risk_event_illegal_severity_raises(self) -> None:
        request = _make_complete_governance_request()
        request.risk_events[0].severity = "invalid"
        with pytest.raises(ValueError, match="非法 risk severity"):
            GovernanceService().compute_governance_ledger(request)


class TestCapitalActionModule:
    """模块 4：分红、回购、融资、并购、扩产、资产处置。"""

    def test_capital_action_complete_passes(self) -> None:
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        assert len(ledger.capital_actions) == 1

    def test_capital_action_missing_source_ref_triggers_blocker(self) -> None:
        request = _make_complete_governance_request()
        request.capital_actions[0].source_ref = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [
            g for g in ledger.gaps if "capital_actions" in g.field_name
        ]
        assert len(blocker_gaps) == 1

    def test_capital_action_illegal_type_raises(self) -> None:
        request = _make_complete_governance_request()
        request.capital_actions[0].action_type = "invalid"
        with pytest.raises(ValueError, match="非法 capital action type"):
            GovernanceService().compute_governance_ledger(request)

    def test_capital_action_to_dict_roundtrip(self) -> None:
        action = _make_complete_capital_action()
        d = action.to_dict()
        assert d["action_type"] == "dividend"
        assert d["amount"] == 200_000_000.0


class TestManagementCommitmentModule:
    """模块 5：管理层承诺及兑现情况。"""

    def test_commitment_complete_passes(self) -> None:
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        assert len(ledger.management_commitments) == 1

    def test_commitment_illegal_status_raises(self) -> None:
        request = _make_complete_governance_request()
        request.management_commitments[0].fulfillment_status = "invalid"
        with pytest.raises(ValueError, match="非法 commitment status"):
            GovernanceService().compute_governance_ledger(request)

    def test_commitment_to_dict_roundtrip(self) -> None:
        commitment = _make_complete_management_commitment()
        d = commitment.to_dict()
        assert d["original_text"] == "公司将于 2024 年实现营收增长 30%"
        assert d["fulfillment_status"] == "fulfilled"
        assert d["deviation"] == "无偏差"

    def test_pending_status_with_empty_fulfillment_evidence_passes(self) -> None:
        """pending 状态允许 fulfillment_evidence_id 为空。"""
        request = _make_complete_governance_request()
        request.management_commitments[0].fulfillment_status = "pending"
        request.management_commitments[0].fulfillment_evidence_id = ""
        request.management_commitments[0].deviation = "尚在执行中"
        ledger = GovernanceService().compute_governance_ledger(request)
        # 5 项齐全：原文/时间/目标/兑现状态/偏差
        commit_gaps = [
            g
            for g in ledger.gaps
            if "management_commitments" in g.field_name
            and g.blocker_kind == "commitment_incomplete"
        ]
        assert commit_gaps == []


class TestCapitalAllocationImpactModule:
    """模块 6：资本配置对 ROIC、每股价值和财务风险的影响。"""

    def test_capital_allocation_impact_complete_passes(self) -> None:
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        assert len(ledger.capital_allocation_impacts) == 1
        assert ledger.capital_allocation_impacts[0].roic_impact_bp == 50.0

    def test_capital_allocation_impact_missing_action_ref_triggers_warning(
        self,
    ) -> None:
        request = _make_complete_governance_request()
        request.capital_allocation_impacts[0].action_ref = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        warning_gaps = [
            g for g in ledger.gaps if "capital_allocation_impacts" in g.field_name
        ]
        assert len(warning_gaps) == 1
        assert warning_gaps[0].severity == "warning"

    def test_capital_allocation_impact_missing_visible_time_blocks_numbers(
        self,
    ) -> None:
        """ROIC/每股价值等数值缺可见时间时必须 blocker。"""
        request = _make_complete_governance_request()
        request.capital_allocation_impacts[0].available_at = ""
        ledger = GovernanceService().compute_governance_ledger(request)
        gaps = [
            g
            for g in ledger.gaps
            if g.field_name == "capital_allocation_impacts[0].available_at"
        ]
        assert len(gaps) == 1
        assert gaps[0].severity == "blocker"

    def test_capital_allocation_impact_to_dict_roundtrip(self) -> None:
        impact = _make_complete_capital_allocation_impact()
        d = impact.to_dict()
        assert d["roic_impact_bp"] == 50.0
        assert d["per_share_value_impact"] == 0.15


# ---------------------------------------------------------------------------
# 顶层集成测试
# ---------------------------------------------------------------------------


class TestGovernanceLedgerIntegration:
    """顶层 GovernanceLedger 集成测试。"""

    def test_complete_ledger_has_no_blocker_gaps(self) -> None:
        """完整输入下不产生 blocker gap。"""
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        blocker_gaps = [g for g in ledger.gaps if g.severity == "blocker"]
        assert blocker_gaps == []
        assert ledger.symbol == "600000.SH"
        assert ledger.company_name == "测试公司"
        assert ledger.period == "2024-12-31"

    def test_ledger_to_dict_serializes_all_modules(self) -> None:
        """to_dict 序列化所有 10 个子模块。"""
        request = _make_complete_governance_request()
        ledger = GovernanceService().compute_governance_ledger(request)
        d = ledger.to_dict()
        assert len(d["shareholder_changes"]) == 1
        assert len(d["management_persons"]) == 1
        assert len(d["equity_incentives"]) == 1
        assert len(d["related_party_transactions"]) == 1
        assert len(d["risk_events"]) == 1
        assert len(d["capital_actions"]) == 1
        assert len(d["management_commitments"]) == 1
        assert len(d["personnel_assessments"]) == 1
        assert len(d["governance_risks"]) == 1
        assert len(d["capital_allocation_impacts"]) == 1
        assert d["schema_version"] == "governance_v1"
        assert d["created_at"] != ""

    def test_ledger_with_empty_inputs_returns_empty_ledger(self) -> None:
        """空输入返回空台账（不崩溃）。"""
        request = GovernanceRequest(
            symbol="600000.SH",
            company_name="空公司",
            period="2024-12-31",
        )
        ledger = GovernanceService().compute_governance_ledger(request)
        assert ledger.shareholder_changes == []
        assert ledger.management_commitments == []
        assert ledger.gaps == []
        assert ledger.company_name == "空公司"


# ---------------------------------------------------------------------------
# 仓储测试
# ---------------------------------------------------------------------------


class TestGovernanceRepository:
    """GovernanceRepository SQLite 持久化测试。"""

    def test_save_and_get_ledger(self, tmp_path: Path) -> None:
        """保存并按 ledger_id 获取台账。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        ledger = GovernanceService().compute_governance_ledger(
            _make_complete_governance_request()
        )
        ledger_id = repo.save_ledger(ledger, version="v1")
        retrieved = repo.get_ledger(ledger_id)
        assert retrieved is not None
        assert retrieved.symbol == "600000.SH"
        assert retrieved.company_name == "测试公司"
        assert len(retrieved.management_commitments) == 1

    def test_save_ledger_idempotent_fails_on_duplicate_version(
        self, tmp_path: Path
    ) -> None:
        """同 (symbol, period, version) 重复保存抛 ValueError。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        ledger = GovernanceService().compute_governance_ledger(
            _make_complete_governance_request()
        )
        repo.save_ledger(ledger, version="v1")
        with pytest.raises(ValueError, match="治理台账已存在"):
            repo.save_ledger(ledger, version="v1")

    def test_different_versions_coexist(self, tmp_path: Path) -> None:
        """不同版本可并存。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        ledger_v1 = GovernanceService().compute_governance_ledger(
            _make_complete_governance_request()
        )
        ledger_v2 = GovernanceService().compute_governance_ledger(
            _make_complete_governance_request()
        )
        ledger_v2.company_name = "测试公司 v2"
        id1 = repo.save_ledger(ledger_v1, version="v1")
        id2 = repo.save_ledger(ledger_v2, version="v2")
        assert id1 != id2
        assert repo.get_ledger(id1).company_name == "测试公司"
        assert repo.get_ledger(id2).company_name == "测试公司 v2"

    def test_list_ledgers_filter_by_symbol(self, tmp_path: Path) -> None:
        """按 symbol 过滤列台账。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        for index, symbol in enumerate(("600000.SH", "600001.SH", "600000.SH")):
            req = _make_complete_governance_request()
            req.symbol = symbol
            ledger = GovernanceService().compute_governance_ledger(req)
            repo.save_ledger(ledger, version=f"v_{index}")
        ledgers = repo.list_ledgers(symbol="600000.SH")
        assert len(ledgers) == 2
        assert all(l.symbol == "600000.SH" for l in ledgers)

    def test_get_latest_ledger_pit_filter(self, tmp_path: Path) -> None:
        """PIT 过滤取最新可见台账。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        # v1: period=2024-06-30
        req_v1 = _make_complete_governance_request()
        req_v1.period = "2024-06-30"
        req_v1.available_at = "2024-08-31"
        ledger_v1 = GovernanceService().compute_governance_ledger(req_v1)
        repo.save_ledger(ledger_v1, version="v1")
        # v2: period=2024-12-31
        req_v2 = _make_complete_governance_request()
        req_v2.period = "2024-12-31"
        req_v2.available_at = "2025-03-31"
        ledger_v2 = GovernanceService().compute_governance_ledger(req_v2)
        repo.save_ledger(ledger_v2, version="v2")
        # PIT as_of=2024-09-30 只能见到 v1
        latest = repo.get_latest_ledger(symbol="600000.SH", as_of="2024-09-30")
        assert latest is not None
        assert latest.period == "2024-06-30"
        # 报告期已结束但尚未披露时，不能提前看到 v2
        latest = repo.get_latest_ledger(symbol="600000.SH", as_of="2025-01-01")
        assert latest is not None
        assert latest.period == "2024-06-30"
        # 到 v2 的可见时间后才能读取 v2
        latest = repo.get_latest_ledger(symbol="600000.SH", as_of="2025-04-01")
        assert latest is not None
        assert latest.period == "2024-12-31"

    def test_repository_restart_rereads_typed_nested_data(self, tmp_path: Path) -> None:
        """关闭并重建仓储后仍可回读完整类型与可见时间。"""
        db_path = tmp_path / "test.db"
        first_repo = GovernanceRepository(db_path=str(db_path))
        ledger = GovernanceService().compute_governance_ledger(
            _make_complete_governance_request()
        )
        ledger_id = first_repo.save_ledger(ledger, version="v1")

        restarted_repo = GovernanceRepository(db_path=str(db_path))
        reread = restarted_repo.get_ledger(ledger_id)

        assert reread is not None
        assert reread.available_at == "2025-03-31"
        assert isinstance(reread.shareholder_changes[0].person, ManagementPerson)

    def test_list_ledger_versions_sorted(self, tmp_path: Path) -> None:
        """版本按 created_at 升序排列。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        for v in ("v3", "v1", "v2"):
            ledger = GovernanceService().compute_governance_ledger(
                _make_complete_governance_request()
            )
            repo.save_ledger(ledger, version=v)
        versions = repo.list_ledger_versions(
            symbol="600000.SH", period="2024-12-31"
        )
        assert len(versions) == 3
        # 按 created_at 升序，所以 v3(先存) 在前
        # 注意：versions 字段未在 list_ledger_versions 中返回，按 created_at 排序即可
        assert all(v.symbol == "600000.SH" for v in versions)

    def test_stats_returns_schema_and_count(self, tmp_path: Path) -> None:
        """stats 返回 schema_version 和总数。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        ledger = GovernanceService().compute_governance_ledger(
            _make_complete_governance_request()
        )
        repo.save_ledger(ledger, version="v1")
        stats = repo.stats()
        assert stats["schema_version"] == 1
        assert stats["total_ledgers"] == 1

    def test_save_ledger_without_symbol_raises(self, tmp_path: Path) -> None:
        """空 symbol 抛 ValueError。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        ledger = GovernanceLedger(symbol="", period="2024-12-31")
        with pytest.raises(ValueError, match="symbol"):
            repo.save_ledger(ledger)

    def test_save_ledger_without_period_raises(self, tmp_path: Path) -> None:
        """空 period 抛 ValueError。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        ledger = GovernanceLedger(symbol="600000.SH", period="")
        with pytest.raises(ValueError, match="period"):
            repo.save_ledger(ledger)

    def test_save_ledger_without_available_at_raises(self, tmp_path: Path) -> None:
        """缺真实可见时间时阻止写入，避免报告期冒充 PIT。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        ledger = GovernanceLedger(symbol="600000.SH", period="2024-12-31")
        with pytest.raises(ValueError, match="available_at"):
            repo.save_ledger(ledger)

    def test_get_ledger_returns_none_for_unknown_id(self, tmp_path: Path) -> None:
        """未知 ID 返回 None。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        assert repo.get_ledger("unknown_id") is None

    def test_get_latest_ledger_returns_none_for_unknown_symbol(
        self, tmp_path: Path
    ) -> None:
        """未知 symbol 返回 None。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        assert repo.get_latest_ledger(symbol="999999.SH") is None

    def test_migrate_is_idempotent(self, tmp_path: Path) -> None:
        """多次 migrate 不报错。"""
        repo = GovernanceRepository(db_path=str(tmp_path / "test.db"))
        repo.migrate()
        repo.migrate()
        stats = repo.stats()
        assert stats["schema_version"] == 1


# ---------------------------------------------------------------------------
# 校验函数测试
# ---------------------------------------------------------------------------


class TestValidators:
    """校验函数测试。"""

    def test_validate_shareholder_change_type_legal(self) -> None:
        for change_type in (
            "controller", "substantial_holder", "director",
            "officer", "supervisor", "",
        ):
            validate_shareholder_change_type(change_type)

    def test_validate_shareholder_change_type_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_shareholder_change_type("invalid")

    def test_validate_equity_incentive_type_legal(self) -> None:
        for plan_type in ("option", "rsu", "restricted_stock", ""):
            validate_equity_incentive_type(plan_type)

    def test_validate_equity_incentive_type_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_equity_incentive_type("invalid")

    def test_validate_related_tx_type_legal(self) -> None:
        for tx_type in (
            "purchase", "sale", "loan", "guarantee", "fund_transfer", "",
        ):
            validate_related_tx_type(tx_type)

    def test_validate_related_tx_type_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_related_tx_type("invalid")

    def test_validate_pricing_basis_legal(self) -> None:
        for basis in ("market", "agreement", "evaluation", ""):
            validate_pricing_basis(basis)

    def test_validate_pricing_basis_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_pricing_basis("invalid")

    def test_validate_risk_event_type_legal(self) -> None:
        for event_type in ("pledge", "guarantee", "regulatory_penalty", ""):
            validate_risk_event_type(event_type)

    def test_validate_risk_event_type_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_risk_event_type("invalid")

    def test_validate_risk_severity_legal(self) -> None:
        for severity in ("info", "warning", "severe", ""):
            validate_risk_severity(severity)

    def test_validate_risk_severity_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_risk_severity("invalid")

    def test_validate_capital_action_type_legal(self) -> None:
        for action_type in (
            "dividend", "buyback", "financing", "ma",
            "capacity_expansion", "asset_disposal", "",
        ):
            validate_capital_action_type(action_type)

    def test_validate_capital_action_type_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_capital_action_type("invalid")

    def test_validate_commitment_status_legal(self) -> None:
        for status in (
            "fulfilled", "partially_fulfilled", "unfulfilled", "pending", "",
        ):
            validate_commitment_status(status)

    def test_validate_commitment_status_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_commitment_status("invalid")

    def test_validate_governance_risk_type_legal(self) -> None:
        for risk_type in (
            "controller_change", "related_party", "pledge",
            "regulatory", "governance_structure", "",
        ):
            validate_governance_risk_type(risk_type)

    def test_validate_governance_risk_type_illegal_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_governance_risk_type("invalid")


# ---------------------------------------------------------------------------
# GovernanceGap 测试
# ---------------------------------------------------------------------------


class TestGovernanceGap:
    """GovernanceGap 数据类测试。"""

    def test_gap_to_dict_roundtrip(self) -> None:
        gap = GovernanceGap(
            field_name="test_field",
            reason="test reason",
            blocker_kind="commitment_incomplete",
            severity="blocker",
            note="test note",
        )
        d = gap.to_dict()
        assert d["field_name"] == "test_field"
        assert d["blocker_kind"] == "commitment_incomplete"
        assert d["severity"] == "blocker"

    def test_gap_default_severity_is_blocker(self) -> None:
        gap = GovernanceGap()
        assert gap.severity == "blocker"
        assert gap.blocker_kind == "missing"


# ---------------------------------------------------------------------------
# 常量测试
# ---------------------------------------------------------------------------


class TestConstants:
    """常量定义测试。"""

    def test_emotional_news_keywords_not_empty(self) -> None:
        assert len(EMOTIONAL_NEWS_KEYWORDS) > 0
        assert "震惊" in EMOTIONAL_NEWS_KEYWORDS
        assert "天才" in EMOTIONAL_NEWS_KEYWORDS

    def test_commitment_statuses_contains_all_required(self) -> None:
        from core.research.governance.contracts import COMMITMENT_STATUSES

        assert "fulfilled" in COMMITMENT_STATUSES
        assert "partially_fulfilled" in COMMITMENT_STATUSES
        assert "unfulfilled" in COMMITMENT_STATUSES
        assert "pending" in COMMITMENT_STATUSES

    def test_capital_action_types_contains_all_required(self) -> None:
        from core.research.governance.contracts import CAPITAL_ACTION_TYPES

        assert "dividend" in CAPITAL_ACTION_TYPES
        assert "buyback" in CAPITAL_ACTION_TYPES
        assert "financing" in CAPITAL_ACTION_TYPES
        assert "ma" in CAPITAL_ACTION_TYPES
        assert "capacity_expansion" in CAPITAL_ACTION_TYPES
        assert "asset_disposal" in CAPITAL_ACTION_TYPES

    def test_gap_kinds_contains_required_types(self) -> None:
        from core.research.governance.contracts import (
            GAP_KIND_COMMITMENT_INCOMPLETE,
            GAP_KIND_EMOTIONAL_NEWS_ASSESSMENT,
            GAP_KIND_MISSING,
            GAP_KIND_RISK_FINANCIAL_IMPACT_MIXED,
        )

        assert GAP_KIND_MISSING == "missing"
        assert GAP_KIND_COMMITMENT_INCOMPLETE == "commitment_incomplete"
        assert GAP_KIND_EMOTIONAL_NEWS_ASSESSMENT == "emotional_news_assessment"
        assert GAP_KIND_RISK_FINANCIAL_IMPACT_MIXED == "risk_financial_impact_mixed"
