"""P1.6 治理台账服务。

组装治理台账（10 个子模块）并校验 3 个硬门禁：
1. 管理层承诺按"原文—时间—目标—兑现—偏差"5 项齐全
2. 人员评价不能由情绪新闻直接生成（detect_emotional_news 检测）
3. 治理风险与财务预测影响分开表达（risk_financial_impact_mixed 不能为 True）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    CapitalAction,
    CapitalAllocationImpact,
    EquityIncentive,
    GAP_KIND_COMMITMENT_INCOMPLETE,
    GAP_KIND_EMOTIONAL_NEWS_ASSESSMENT,
    GAP_KIND_MISSING,
    GAP_KIND_RISK_FINANCIAL_IMPACT_MIXED,
    GAP_SEVERITY_BLOCKER,
    GAP_SEVERITY_WARNING,
    GovernanceGap,
    GovernanceLedger,
    GovernanceRisk,
    ManagementCommitment,
    ManagementPerson,
    PersonnelAssessment,
    RelatedPartyTransaction,
    RiskEvent,
    ShareholderChange,
    detect_emotional_news,
    utc_now,
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
# 请求
# ---------------------------------------------------------------------------


@dataclass
class GovernanceRequest:
    """治理台账服务请求。

    Attributes:
        symbol: 公司股票代码
        company_name: 公司名称
        period: 台账基准期间 YYYY-MM-DD
        available_at: 台账整体可见时间 YYYY-MM-DD
        shareholder_changes: 股东/董监高变更列表
        management_persons: 管理人员列表
        equity_incentives: 股权激励计划列表
        related_party_transactions: 关联交易列表
        risk_events: 风险事件列表
        capital_actions: 资本动作列表
        management_commitments: 管理层承诺列表
        personnel_assessments: 人员评价列表
        governance_risks: 治理风险列表
        capital_allocation_impacts: 资本配置影响列表
        source_trace_id: 来源追溯 ID
    """

    symbol: str = ""
    company_name: str = ""
    period: str = ""
    available_at: str = ""
    shareholder_changes: list[ShareholderChange] = field(default_factory=list)
    management_persons: list[ManagementPerson] = field(default_factory=list)
    equity_incentives: list[EquityIncentive] = field(default_factory=list)
    related_party_transactions: list[RelatedPartyTransaction] = field(default_factory=list)
    risk_events: list[RiskEvent] = field(default_factory=list)
    capital_actions: list[CapitalAction] = field(default_factory=list)
    management_commitments: list[ManagementCommitment] = field(default_factory=list)
    personnel_assessments: list[PersonnelAssessment] = field(default_factory=list)
    governance_risks: list[GovernanceRisk] = field(default_factory=list)
    capital_allocation_impacts: list[CapitalAllocationImpact] = field(default_factory=list)
    source_trace_id: str = ""


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class GovernanceService:
    """治理台账服务。

    组装治理台账 10 个子模块并校验 3 个硬门禁。
    """

    def compute_governance_ledger(self, request: GovernanceRequest) -> GovernanceLedger:
        """计算治理台账。

        Args:
            request: 治理台账请求

        Returns:
            GovernanceLedger（含 gaps）
        """
        if not isinstance(request, GovernanceRequest):
            raise TypeError("request must be GovernanceRequest")

        def items(value: Any, model: type[Any]) -> list[Any]:
            if not isinstance(value, (list, tuple)):
                return []
            return [item for item in value if isinstance(item, model)]

        gaps: list[GovernanceGap] = []

        # 校验 shareholder_changes
        shareholder_changes = self._validate_shareholder_changes(
            items(request.shareholder_changes, ShareholderChange), gaps
        )

        # 校验 management_persons
        management_persons = self._validate_management_persons(
            items(request.management_persons, ManagementPerson), gaps
        )

        # 校验 equity_incentives
        equity_incentives = self._validate_equity_incentives(
            items(request.equity_incentives, EquityIncentive), gaps
        )

        # 校验 related_party_transactions
        related_party_transactions = self._validate_related_party_transactions(
            items(request.related_party_transactions, RelatedPartyTransaction), gaps
        )

        # 校验 risk_events
        risk_events = self._validate_risk_events(items(request.risk_events, RiskEvent), gaps)

        # 校验 capital_actions
        capital_actions = self._validate_capital_actions(
            items(request.capital_actions, CapitalAction), gaps
        )

        # 验收点 1：管理层承诺 5 项齐全
        management_commitments = self._validate_management_commitments(
            items(request.management_commitments, ManagementCommitment), gaps
        )

        # 验收点 2：人员评价不能由情绪新闻直接生成
        personnel_assessments = self._validate_personnel_assessments(
            items(request.personnel_assessments, PersonnelAssessment), gaps
        )

        # 验收点 3：治理风险与财务预测影响分开表达
        governance_risks = self._validate_governance_risks(
            items(request.governance_risks, GovernanceRisk), gaps
        )

        # 校验 capital_allocation_impacts
        capital_allocation_impacts = self._validate_capital_allocation_impacts(
            items(request.capital_allocation_impacts, CapitalAllocationImpact), gaps
        )

        return GovernanceLedger(
            symbol=request.symbol,
            company_name=request.company_name,
            period=request.period,
            available_at=request.available_at,
            shareholder_changes=shareholder_changes,
            management_persons=management_persons,
            equity_incentives=equity_incentives,
            related_party_transactions=related_party_transactions,
            risk_events=risk_events,
            capital_actions=capital_actions,
            management_commitments=management_commitments,
            personnel_assessments=personnel_assessments,
            governance_risks=governance_risks,
            capital_allocation_impacts=capital_allocation_impacts,
            gaps=gaps,
            source_trace_id=request.source_trace_id,
            created_at=utc_now(),
        )

    # ----- 子模块校验 -----

    def _validate_shareholder_changes(
        self, changes: list[ShareholderChange], gaps: list[GovernanceGap]
    ) -> list[ShareholderChange]:
        """校验股东变更列表。"""
        result: list[ShareholderChange] = []
        for idx, change in enumerate(changes):
            if change.change_type:
                validate_shareholder_change_type(change.change_type)
            if not change.source_ref or not change.change_date or not change.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"shareholder_changes[{idx}].source_ref",
                        reason=(
                            f"股东变更 {idx} 缺来源证据、变更日期或可见时间"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(change)
        return result

    def _validate_management_persons(
        self, persons: list[ManagementPerson], gaps: list[GovernanceGap]
    ) -> list[ManagementPerson]:
        """校验管理人员列表。"""
        result: list[ManagementPerson] = []
        for idx, person in enumerate(persons):
            if not person.source_ref or not person.appointment_date or not person.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"management_persons[{idx}].source_ref",
                        reason=(
                            f"管理人员 {person.name or idx} 缺来源证据、任职日期或可见时间"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(person)
        return result

    def _validate_equity_incentives(
        self, incentives: list[EquityIncentive], gaps: list[GovernanceGap]
    ) -> list[EquityIncentive]:
        """校验股权激励计划列表。"""
        result: list[EquityIncentive] = []
        for idx, incentive in enumerate(incentives):
            if incentive.plan_type:
                validate_equity_incentive_type(incentive.plan_type)
            if not incentive.source_ref or not incentive.grant_date or not incentive.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"equity_incentives[{idx}].source_ref",
                        reason=(
                            f"股权激励 {incentive.plan_name or idx} 缺来源证据、授予日期或可见时间"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(incentive)
        return result

    def _validate_related_party_transactions(
        self, transactions: list[RelatedPartyTransaction], gaps: list[GovernanceGap]
    ) -> list[RelatedPartyTransaction]:
        """校验关联交易列表。"""
        result: list[RelatedPartyTransaction] = []
        for idx, tx in enumerate(transactions):
            if tx.transaction_type:
                validate_related_tx_type(tx.transaction_type)
            if tx.pricing_basis:
                validate_pricing_basis(tx.pricing_basis)
            if not tx.source_ref or not tx.period or not tx.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"related_party_transactions[{idx}].source_ref",
                        reason=(
                            f"关联交易 {idx} 缺来源证据、期间或可见时间"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(tx)
        return result

    def _validate_risk_events(
        self, events: list[RiskEvent], gaps: list[GovernanceGap]
    ) -> list[RiskEvent]:
        """校验风险事件列表。"""
        result: list[RiskEvent] = []
        for idx, event in enumerate(events):
            if event.event_type:
                validate_risk_event_type(event.event_type)
            if event.severity:
                validate_risk_severity(event.severity)
            if not event.source_ref or not event.event_date or not event.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"risk_events[{idx}].source_ref",
                        reason=f"风险事件 {idx} 缺来源证据、事件日期或可见时间",
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(event)
        return result

    def _validate_capital_actions(
        self, actions: list[CapitalAction], gaps: list[GovernanceGap]
    ) -> list[CapitalAction]:
        """校验资本动作列表。"""
        result: list[CapitalAction] = []
        for idx, action in enumerate(actions):
            if action.action_type:
                validate_capital_action_type(action.action_type)
            if not action.source_ref or not action.announcement_date or not action.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"capital_actions[{idx}].source_ref",
                        reason=f"资本动作 {idx} 缺来源证据、公告日期或可见时间",
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(action)
        return result

    def _validate_management_commitments(
        self, commitments: list[ManagementCommitment], gaps: list[GovernanceGap]
    ) -> list[ManagementCommitment]:
        """校验管理层承诺列表。

        验收点 1：按"原文—时间—目标—兑现—偏差"5 项齐全保存。
        """
        result: list[ManagementCommitment] = []
        for idx, commitment in enumerate(commitments):
            if commitment.fulfillment_status:
                validate_commitment_status(commitment.fulfillment_status)
            complete, missing_fields = commitment.required_fields_complete()
            if not complete:
                gaps.append(
                    GovernanceGap(
                        field_name=f"management_commitments[{idx}].required_fields",
                        reason=(
                            f"管理层承诺 {idx} 缺 5 项必备字段: {missing_fields}；"
                            "必须按原文—时间—目标—兑现—偏差保存"
                        ),
                        blocker_kind=GAP_KIND_COMMITMENT_INCOMPLETE,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            if not commitment.source_ref or not commitment.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"management_commitments[{idx}].source_ref",
                        reason=f"管理层承诺 {idx} 缺来源证据或可见时间",
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(commitment)
        return result

    def _validate_personnel_assessments(
        self, assessments: list[PersonnelAssessment], gaps: list[GovernanceGap]
    ) -> list[PersonnelAssessment]:
        """校验人员评价列表。

        验收点 2：人员评价不能由情绪新闻直接生成。
        若 is_from_emotional_news=True 或检测到情绪词，触发 blocker。
        """
        result: list[PersonnelAssessment] = []
        for idx, assessment in enumerate(assessments):
            detected_words = detect_emotional_news(assessment.assessment)
            if detected_words and not assessment.emotional_news_detected:
                assessment.emotional_news_detected = detected_words
            if assessment.is_from_emotional_news or assessment.emotional_news_detected:
                if not assessment.is_from_emotional_news:
                    assessment.is_from_emotional_news = True
                gaps.append(
                    GovernanceGap(
                        field_name=f"personnel_assessments[{idx}].is_from_emotional_news",
                        reason=(
                            f"人员评价 {assessment.person_name or idx} 来自情绪新闻，"
                            f"检测到情绪词: {assessment.emotional_news_detected}；"
                            "人员评价不能由情绪新闻直接生成"
                        ),
                        blocker_kind=GAP_KIND_EMOTIONAL_NEWS_ASSESSMENT,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
                continue
            if not assessment.source_ref or not assessment.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"personnel_assessments[{idx}].source_ref",
                        reason=f"人员评价 {assessment.person_name or idx} 缺来源证据或可见时间",
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(assessment)
        return result

    def _validate_governance_risks(
        self, risks: list[GovernanceRisk], gaps: list[GovernanceGap]
    ) -> list[GovernanceRisk]:
        """校验治理风险列表。

        验收点 3：治理风险与财务预测影响分开表达。
        若 risk_financial_impact_mixed=True，触发 blocker。
        """
        result: list[GovernanceRisk] = []
        for idx, risk in enumerate(risks):
            if risk.risk_type:
                validate_governance_risk_type(risk.risk_type)
            if risk.severity:
                validate_risk_severity(risk.severity)
            if risk.risk_financial_impact_mixed:
                gaps.append(
                    GovernanceGap(
                        field_name=f"governance_risks[{idx}].risk_financial_impact_mixed",
                        reason=(
                            f"治理风险 {idx} risk_financial_impact_mixed=True，"
                            "治理风险与财务预测影响必须分开表达；"
                            "potential_financial_impact 仅为描述性字段，不进入预测引擎"
                        ),
                        blocker_kind=GAP_KIND_RISK_FINANCIAL_IMPACT_MIXED,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            if not risk.source_ref or not risk.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=f"governance_risks[{idx}].source_ref",
                        reason=f"治理风险 {idx} 缺来源证据或可见时间",
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            result.append(risk)
        return result

    def _validate_capital_allocation_impacts(
        self, impacts: list[CapitalAllocationImpact], gaps: list[GovernanceGap]
    ) -> list[CapitalAllocationImpact]:
        """校验资本配置影响列表。"""
        result: list[CapitalAllocationImpact] = []
        for idx, impact in enumerate(impacts):
            if not impact.source_ref or not impact.available_at:
                gaps.append(
                    GovernanceGap(
                        field_name=(
                            f"capital_allocation_impacts[{idx}].source_ref"
                            if not impact.source_ref
                            else f"capital_allocation_impacts[{idx}].available_at"
                        ),
                        reason=(
                            f"资本配置影响 {idx} 缺来源证据或可见时间；"
                            "ROIC、每股价值和财务风险数值不得计算"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
            if not impact.action_ref:
                gaps.append(
                    GovernanceGap(
                        field_name=f"capital_allocation_impacts[{idx}].action_ref",
                        reason=f"资本配置影响 {idx} 缺关联资本动作 ID",
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_WARNING,
                    )
                )
            result.append(impact)
        return result


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def compute_governance_ledger(request: GovernanceRequest) -> GovernanceLedger:
    """便捷入口：直接计算治理台账。

    Args:
        request: 治理台账请求

    Returns:
        GovernanceLedger
    """
    return GovernanceService().compute_governance_ledger(request)
