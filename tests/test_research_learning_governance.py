"""P3.5 受治理的自校准学习闭环测试。

覆盖：
- 候选参数校验（合法/非法领域、bounds 越界、缺少证据）
- 小样本拒绝升级
- 跨域权重隔离
- 失败校准回滚（不污染 active、null control 失败、终态保护）
- 上线流程状态机（完整流程、独立审查人、非法转移、审查拒绝）
- 禁止修改原始事实/Evidence/业务归属等 6 类目标
- 可解释、可重放、可回滚
"""
from __future__ import annotations

import pytest

from core.research.learning.contracts import (
    ALLOWED_CALIBRATION_DOMAINS,
    CalibrationOutcome,
    CalibrationParameter,
    CalibrationStatus,
    FORBIDDEN_MUTATION_TARGETS,
    LearningGovernanceError,
    assert_domain_is_calibratable,
    assert_target_not_forbidden,
    is_status_transition_allowed,
)
from core.research.learning.calibration_engine import CalibrationEngine
from core.research.learning.promotion_policy import (
    CrossDomainFirewall,
    DEFAULT_MINIMUM_SAMPLE_SIZE,
    DOMAIN_MINIMUM_SAMPLE_SIZE,
    PromotionPolicy,
)


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------


def _make_valid_parameter(
    domain: str = "source_validity",
    name: str = "decay_rate",
    candidate_value: float = 0.85,
    current_value: float = 0.70,
    sample_size: int = 100,
    owner: str = "analyst-A",
    minimum_sample_size: int = 30,
    bounds: tuple[float, float] = (0.0, 1.0),
) -> CalibrationParameter:
    """构造合法的候选参数。"""
    return CalibrationParameter(
        domain=domain,
        name=name,
        current_value=current_value,
        candidate_value=candidate_value,
        baseline_value=current_value,
        evidence_refs=["ev-001", "ev-002"],
        sample_size=sample_size,
        minimum_sample_size=minimum_sample_size,
        bounds=bounds,
        status=CalibrationStatus.CANDIDATE,
        owner=owner,
        provenance="script=v1,data=v2",
    )


def _advance_to_monitoring(engine: CalibrationEngine, param_id: str) -> None:
    """把参数推进到 monitoring 状态（升级测试前置）。"""
    engine.run_offline_evaluation(param_id, {"mape": 0.08}, actor="A")
    engine.run_null_control_test(param_id, {"passed": True, "delta": 0.0}, actor="A")
    engine.start_shadow(param_id, {"shadow_drift": 0.02}, actor="A")
    engine.independent_review(param_id, reviewer="reviewer-B", decision="approved", note="ok")
    engine.bounded_rollout(param_id, {"rollout_coverage": 0.1}, actor="A")
    engine.monitor(param_id, {"monitor_drift": 0.01}, actor="A")


# ---------------------------------------------------------------------------
# 契约测试
# ---------------------------------------------------------------------------


class TestContracts:
    """契约、状态机、禁止列表测试。"""

    def test_only_six_domains_are_calibratable(self) -> None:
        """只允许 6 类目标校准。"""
        assert len(ALLOWED_CALIBRATION_DOMAINS) == 6

    def test_six_forbidden_targets_defined(self) -> None:
        """绝对禁止自动修改 6 类目标。"""
        assert len(FORBIDDEN_MUTATION_TARGETS) == 6
        for target in FORBIDDEN_MUTATION_TARGETS:
            with pytest.raises(LearningGovernanceError, match="禁止"):
                assert_target_not_forbidden(target)

    def test_unknown_domain_rejected(self) -> None:
        """未知领域拒绝校准。"""
        with pytest.raises(LearningGovernanceError, match="不在允许校准列表中"):
            assert_domain_is_calibratable("portfolio_weight")

    def test_invalid_status_types_are_safe(self) -> None:
        assert is_status_transition_allowed("monitoring", CalibrationStatus.PROMOTED) is False  # type: ignore[arg-type]

        parameter = CalibrationParameter(
            evidence_refs=None,  # type: ignore[arg-type]
            bounds=None,  # type: ignore[arg-type]
            status="broken",  # type: ignore[arg-type]
        )
        outcome = CalibrationOutcome(
            evaluation_metrics=None,  # type: ignore[arg-type]
            null_control_result=None,  # type: ignore[arg-type]
        )

        assert parameter.to_dict()["evidence_refs"] == []
        assert parameter.to_dict()["bounds"] == []
        assert outcome.to_dict()["null_control_result"] == {}

    @pytest.mark.parametrize(
        "current,target,allowed",
        [
            (CalibrationStatus.CANDIDATE, CalibrationStatus.OFFLINE_EVALUATED, True),
            (CalibrationStatus.OFFLINE_EVALUATED, CalibrationStatus.NULL_CONTROL_PASSED, True),
            (CalibrationStatus.NULL_CONTROL_PASSED, CalibrationStatus.SHADOW, True),
            (CalibrationStatus.SHADOW, CalibrationStatus.INDEPENDENT_REVIEWED, True),
            (CalibrationStatus.INDEPENDENT_REVIEWED, CalibrationStatus.BOUNDED_ROLLOUT, True),
            (CalibrationStatus.BOUNDED_ROLLOUT, CalibrationStatus.MONITORING, True),
            (CalibrationStatus.MONITORING, CalibrationStatus.PROMOTED, True),
            # 非法跨阶段
            (CalibrationStatus.CANDIDATE, CalibrationStatus.PROMOTED, False),
            (CalibrationStatus.CANDIDATE, CalibrationStatus.SHADOW, False),
            (CalibrationStatus.OFFLINE_EVALUATED, CalibrationStatus.PROMOTED, False),
            # 终态不可再转移
            (CalibrationStatus.PROMOTED, CalibrationStatus.ROLLED_BACK, False),
            (CalibrationStatus.ROLLED_BACK, CalibrationStatus.PROMOTED, False),
        ],
    )
    def test_state_machine_transitions(
        self, current: CalibrationStatus, target: CalibrationStatus, allowed: bool
    ) -> None:
        """状态机合法/非法转移。"""
        assert is_status_transition_allowed(current, target) is allowed

    def test_rollback_allowed_from_any_non_terminal_stage(self) -> None:
        """非终态阶段都允许直接回滚。"""
        non_terminal = [
            CalibrationStatus.CANDIDATE,
            CalibrationStatus.OFFLINE_EVALUATED,
            CalibrationStatus.NULL_CONTROL_PASSED,
            CalibrationStatus.SHADOW,
            CalibrationStatus.INDEPENDENT_REVIEWED,
            CalibrationStatus.BOUNDED_ROLLOUT,
            CalibrationStatus.MONITORING,
        ]
        for stage in non_terminal:
            assert is_status_transition_allowed(stage, CalibrationStatus.ROLLED_BACK)


# ---------------------------------------------------------------------------
# 候选参数校验测试
# ---------------------------------------------------------------------------


class TestCandidateValidation:
    """候选参数校验测试。"""

    def test_submit_valid_candidate_succeeds(self) -> None:
        """合法候选参数提交成功。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter()
        param_id = engine.submit_candidate(param)
        assert param_id == param.param_id
        assert engine.get_parameter(param_id) is not None

    def test_duplicate_and_unknown_parameters_are_rejected_without_orphans(self) -> None:
        engine = CalibrationEngine()
        parameter = _make_valid_parameter()
        engine.submit_candidate(parameter)

        with pytest.raises(LearningGovernanceError, match="already exists"):
            engine.submit_candidate(parameter)
        with pytest.raises(LearningGovernanceError, match="param_id=missing"):
            engine.run_offline_evaluation("missing", {})
        assert engine.get_outcome("missing") is None

    def test_string_null_control_result_does_not_pass(self) -> None:
        engine = CalibrationEngine()
        parameter = _make_valid_parameter()
        engine.submit_candidate(parameter)

        with pytest.raises(LearningGovernanceError):
            engine.run_null_control_test(
                parameter.param_id, {"passed": "true"}, actor="A"
            )
        assert engine.get_parameter(parameter.param_id).status is CalibrationStatus.ROLLED_BACK

    def test_submit_candidate_rejects_unknown_domain(self) -> None:
        """未知领域候选参数被拒绝。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(domain="portfolio_weight")
        with pytest.raises(LearningGovernanceError, match="不在允许校准列表中"):
            engine.submit_candidate(param)

    def test_submit_candidate_rejects_out_of_bounds(self) -> None:
        """candidate_value 越界被拒绝。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(candidate_value=1.5, bounds=(0.0, 1.0))
        with pytest.raises(LearningGovernanceError, match="越界"):
            engine.submit_candidate(param)

    def test_submit_candidate_rejects_no_evidence(self) -> None:
        """无证据引用被拒绝。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter()
        param.evidence_refs = []
        with pytest.raises(LearningGovernanceError, match="evidence_refs 为空"):
            engine.submit_candidate(param)


# ---------------------------------------------------------------------------
# 小样本拒绝升级测试
# ---------------------------------------------------------------------------


class TestSmallSampleRejection:
    """小样本拒绝升级测试。"""

    def test_small_sample_blocks_promote(self) -> None:
        """小样本不允许正式升级。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(sample_size=5)  # 远低于 30
        engine.submit_candidate(param)
        _advance_to_monitoring(engine, param.param_id)
        with pytest.raises(LearningGovernanceError, match="小样本拒绝升级"):
            engine.promote(param.param_id, actor="committee")

    def test_domain_specific_minimum_sample_size(self) -> None:
        """不同领域有独立的最小样本量。"""
        assert DOMAIN_MINIMUM_SAMPLE_SIZE["retrieval_hit_rate"] == 50
        assert DOMAIN_MINIMUM_SAMPLE_SIZE["research_budget_priority"] == 20
        assert DEFAULT_MINIMUM_SAMPLE_SIZE == 30


# ---------------------------------------------------------------------------
# 跨域权重隔离测试
# ---------------------------------------------------------------------------


class TestCrossDomainIsolation:
    """跨域权重隔离测试。"""

    def test_cross_domain_propagation_rejected(self) -> None:
        """跨域权重传播被拒绝。"""
        firewall = CrossDomainFirewall()
        with pytest.raises(LearningGovernanceError, match="禁止跨域权重传播"):
            firewall.assert_no_propagation(
                "source_validity", "retrieval_hit_rate", weight=0.3
            )
        assert len(firewall.rejected_attempts) == 1

    def test_same_domain_propagation_allowed(self) -> None:
        """同域内不触发拒绝。"""
        firewall = CrossDomainFirewall()
        firewall.assert_no_propagation(
            "source_validity", "source_validity", weight=0.5
        )
        assert len(firewall.rejected_attempts) == 0

    def test_invalid_policy_configuration_and_inputs_fail_closed(self) -> None:
        policy = PromotionPolicy({"source_validity": -1, "retrieval_hit_rate": "many"})
        assert policy.minimum_sample_size_for("source_validity") == 30
        assert policy.minimum_sample_size_for([]) == DEFAULT_MINIMUM_SAMPLE_SIZE  # type: ignore[arg-type]

        parameter = _make_valid_parameter(sample_size=100)
        parameter.status = CalibrationStatus.MONITORING  # type: ignore[assignment]
        malformed_outcome = CalibrationOutcome(null_control_result={"passed": "true"})

        allowed, reasons = policy.can_promote(parameter, malformed_outcome)

        assert allowed is False
        assert reasons

    def test_malformed_candidate_returns_reasons_instead_of_raising(self) -> None:
        policy = PromotionPolicy()
        parameter = _make_valid_parameter()
        parameter.bounds = ("low", "high")  # type: ignore[assignment]

        allowed, reasons = policy.validate_candidate(parameter)

        assert allowed is False
        assert "finite numbers" in reasons[-1]

    def test_list_parameters_isolated_by_domain(self) -> None:
        """参数列表按域隔离，不混域。"""
        engine = CalibrationEngine()
        engine.submit_candidate(
            _make_valid_parameter(domain="source_validity", name="p1")
        )
        # retrieval_hit_rate 域最小样本量要求为 50，需显式设置
        engine.submit_candidate(
            _make_valid_parameter(
                domain="retrieval_hit_rate", name="p2", candidate_value=0.6,
                minimum_sample_size=50,
            )
        )
        sv_params = engine.list_parameters(domain="source_validity")
        assert len(sv_params) == 1
        assert sv_params[0].name == "p1"
        rh_params = engine.list_parameters(domain="retrieval_hit_rate")
        assert len(rh_params) == 1
        assert rh_params[0].name == "p2"


# ---------------------------------------------------------------------------
# 失败校准回滚测试
# ---------------------------------------------------------------------------


class TestRollback:
    """失败校准回滚测试。"""

    def test_rollback_does_not_pollute_active(self) -> None:
        """失败校准不会污染 active 模型：current_value 保持不变。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(current_value=0.70, candidate_value=0.95)
        engine.submit_candidate(param)
        original_current = param.current_value
        # 推进到 shadow 后失败回滚
        engine.run_offline_evaluation(param.param_id, {"mape": 0.1}, actor="A")
        engine.run_null_control_test(param.param_id, {"passed": True}, actor="A")
        engine.start_shadow(param.param_id, {"shadow_drift": 0.5}, actor="A")
        engine.rollback(param.param_id, actor="A", reason="shadow_drift_too_high")
        rolled = engine.get_parameter(param.param_id)
        assert rolled.status is CalibrationStatus.ROLLED_BACK
        # current_value 必须保持原值（未被 candidate_value 污染）
        assert rolled.current_value == original_current
        # candidate_value 不应被提交为 current_value
        assert rolled.current_value != rolled.candidate_value
        # outcome 记录了回滚原因
        outcome = engine.get_outcome(param.param_id)
        assert outcome is not None
        assert outcome.final_decision == "rolled_back"
        assert outcome.rollback_reason == "shadow_drift_too_high"

    def test_null_control_failure_auto_rolls_back(self) -> None:
        """空白对照测试失败自动回滚。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter()
        engine.submit_candidate(param)
        engine.run_offline_evaluation(param.param_id, {"mape": 0.1}, actor="A")
        with pytest.raises(LearningGovernanceError, match="对照测试未通过"):
            engine.run_null_control_test(
                param.param_id, {"passed": False}, actor="A"
            )
        rolled = engine.get_parameter(param.param_id)
        assert rolled.status is CalibrationStatus.ROLLED_BACK

    def test_terminal_state_cannot_rollback_again(self) -> None:
        """终态不可再次回滚。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter()
        engine.submit_candidate(param)
        engine.rollback(param.param_id, reason="test")
        with pytest.raises(LearningGovernanceError, match="终态"):
            engine.rollback(param.param_id, reason="again")


# ---------------------------------------------------------------------------
# 上线流程状态机测试
# ---------------------------------------------------------------------------


class TestPromotionWorkflow:
    """上线流程状态机测试。"""

    def test_full_promotion_workflow(self) -> None:
        """完整上线流程：candidate → ... → promoted。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(sample_size=100)
        engine.submit_candidate(param)
        pid = param.param_id

        engine.run_offline_evaluation(pid, {"mape": 0.08}, actor="A")
        engine.run_null_control_test(pid, {"passed": True, "delta": 0.0}, actor="A")
        engine.start_shadow(pid, {"shadow_drift": 0.02}, actor="A")
        engine.independent_review(pid, reviewer="reviewer-B", decision="approved", note="ok")
        engine.bounded_rollout(pid, {"rollout_coverage": 0.1}, actor="A")
        engine.monitor(pid, {"monitor_drift": 0.01}, actor="A")
        promoted = engine.promote(pid, actor="committee")

        assert promoted.status is CalibrationStatus.PROMOTED
        # candidate_value 已提交为 current_value
        assert promoted.current_value == param.candidate_value
        # baseline_value 保留升级前的 current_value
        assert promoted.baseline_value == 0.70
        # outcome 已生成
        outcome = engine.get_outcome(pid)
        assert outcome is not None
        assert outcome.final_decision == "promoted"
        assert outcome.null_control_result.get("passed") is True

    def test_independent_review_rejects_same_person(self) -> None:
        """独立审查人不能与 owner 为同一人。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(owner="analyst-A")
        engine.submit_candidate(param)
        engine.run_offline_evaluation(param.param_id, {"mape": 0.08}, actor="A")
        engine.run_null_control_test(param.param_id, {"passed": True}, actor="A")
        engine.start_shadow(param.param_id, {"shadow_drift": 0.02}, actor="A")
        with pytest.raises(LearningGovernanceError, match="不能与候选参数 owner 为同一人"):
            engine.independent_review(
                param.param_id, reviewer="analyst-A", decision="approved"
            )

    def test_invalid_transition_rejected(self) -> None:
        """非法状态转移被拒绝：candidate 不能直接 promote。

        promote 先调用 can_promote 检查状态门禁（必须为 monitoring），
        再由 _advance 检查转移图合法性；两者都会拒绝 candidate → promoted。
        """
        engine = CalibrationEngine()
        param = _make_valid_parameter()
        engine.submit_candidate(param)
        with pytest.raises(LearningGovernanceError, match="升级门禁未通过"):
            engine.promote(param.param_id, actor="A")

    def test_review_rejection_triggers_rollback(self) -> None:
        """独立审查 rejected 触发回滚。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(owner="analyst-A")
        engine.submit_candidate(param)
        engine.run_offline_evaluation(param.param_id, {"mape": 0.08}, actor="A")
        engine.run_null_control_test(param.param_id, {"passed": True}, actor="A")
        engine.start_shadow(param.param_id, {"shadow_drift": 0.02}, actor="A")
        with pytest.raises(LearningGovernanceError, match="独立审查未通过"):
            engine.independent_review(
                param.param_id, reviewer="reviewer-B", decision="rejected", note="证据不足"
            )
        param_after = engine.get_parameter(param.param_id)
        assert param_after.status is CalibrationStatus.ROLLED_BACK


# ---------------------------------------------------------------------------
# 禁止修改原始事实测试
# ---------------------------------------------------------------------------


class TestForbiddenMutations:
    """禁止修改原始事实/Evidence/业务归属等测试。"""

    @pytest.mark.parametrize(
        "target",
        list(FORBIDDEN_MUTATION_TARGETS),
        ids=list(FORBIDDEN_MUTATION_TARGETS),
    )
    def test_forbidden_mutation_rejected_and_recorded(
        self, target: str
    ) -> None:
        """每个禁止目标都拒绝并记录。"""
        engine = CalibrationEngine()
        with pytest.raises(LearningGovernanceError, match="绝对禁止自动修改"):
            engine.attempt_forbidden_mutation(target, attempted_by="bot")
        attempts = engine.list_forbidden_attempts()
        assert len(attempts) == 1
        assert attempts[0].target == target
        assert attempts[0].attempted_by == "bot"

    def test_non_forbidden_target_passes_silently(self) -> None:
        """非禁止目标不触发异常也不记录。"""
        engine = CalibrationEngine()
        engine.attempt_forbidden_mutation(
            "source_validity_decay_rate", attempted_by="A"
        )
        assert engine.list_forbidden_attempts() == []


# ---------------------------------------------------------------------------
# 可解释、可重放、可回滚测试
# ---------------------------------------------------------------------------


class TestReplayability:
    """可解释、可重放、可回滚测试。"""

    def test_history_records_all_transitions(self) -> None:
        """每次状态转移都记录到历史，可重放。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(sample_size=100)
        engine.submit_candidate(param)
        engine.run_offline_evaluation(param.param_id, {"mape": 0.08}, actor="A")
        engine.run_null_control_test(param.param_id, {"passed": True}, actor="A")
        engine.start_shadow(param.param_id, {"shadow_drift": 0.02}, actor="A")
        history = engine.history(param.param_id)
        # submit + offline + null_control + shadow = 至少 4 条
        assert len(history) >= 4
        # 每条都包含可重放所需字段
        for record in history:
            assert record["param_id"] == param.param_id
            assert "action" in record
            assert "status" in record
            assert "current_value" in record
            assert "candidate_value" in record
            assert "baseline_value" in record
            assert "recorded_at" in record

    def test_promote_changes_current_value_and_keeps_baseline(self) -> None:
        """升级提交 candidate_value 为 current_value，baseline 保留。"""
        engine = CalibrationEngine()
        param = _make_valid_parameter(
            current_value=0.70, candidate_value=0.85, sample_size=100
        )
        engine.submit_candidate(param)
        _advance_to_monitoring(engine, param.param_id)
        promoted = engine.promote(param.param_id, actor="committee")
        assert promoted.current_value == 0.85
        assert promoted.baseline_value == 0.70
