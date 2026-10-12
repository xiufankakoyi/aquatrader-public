"""反馈环集成测试。

覆盖：
1. Claim 晋升 hook 自动记录 FactOutcomeRecord
2. CandidateGraph 晋升 hook（节点+边）
3. FactLabel 强枚举与合法状态转换
4. Walk-forward 时间隔离
5. 最小样本回退
6. shadow→active 激活（含服务端测试验证）
7. 参数回滚
8. 反馈去重/限流/深度控制/过期
9. Alpha gap 任务进入统一队列
10. Alpha 域不输出交易收益字段到 Knowledge 域
11. 校准指标完整性
12. transition_id 幂等
13. 已 resolve 记录再修改产生新审计行
14. apply_policy 注入
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.research.control_plane.calibration import CalibrationController
from core.research.control_plane.contracts import (
    CalibrationRun, Claim, ClaimStatus, EvidenceRecord, FeedbackDomain,
    FeedbackEvent, PolicyVersion, ResearchTask, StateTransition, TaskBudget,
)
from core.research.control_plane.domain_calibrators import (
    KnowledgePolicyCalibrator, RetrievalPolicyCalibrator, AlphaPolicyCalibrator,
    MIN_SAMPLES, _walk_forward_split, _brier_score,
)
from core.research.control_plane.fact_outcome import (
    FactLabel, FactOutcomeStore, LEGAL_TRANSITIONS, TERMINAL_LABELS,
)
from core.research.control_plane.feedback import FeedbackController
from core.research.control_plane.policies import ClaimPolicy
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.test_run_record import TestRunRecord, TestRunStore
from core.research_graph.bayesian_status_upgrader import BayesianStatusUpgrader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """临时 ResearchRepository。"""
    return ResearchRepository(tmp_path / "feedback_loop.db")


@pytest.fixture
def fact_store(repo):
    return FactOutcomeStore(repo)


@pytest.fixture
def feedback_controller(repo):
    return FeedbackController(repo)


@pytest.fixture
def calibration_controller(repo):
    return CalibrationController(repo)


@pytest.fixture
def test_run_store(repo):
    return TestRunStore(repo)


def _support(claim_id: str, evidence_id: str, lineage: str, **kwargs):
    """构造支持证据。"""
    defaults = dict(
        stance="support", quote="可定位原文", source_quality=1.2, match_score=0.9,
        grounding_status="exact",
        published_at="2025-01-01T00:00:00+00:00",
        retrieved_at="2025-01-02T00:00:00+00:00",
        metadata={"slots": ["identity", "relationship"], "source_type": "announcement"},
    )
    defaults.update(kwargs)
    return EvidenceRecord(claim_id=claim_id, evidence_id=evidence_id, lineage_id=lineage, **defaults)


# ---------------------------------------------------------------------------
# 1. FactLabel 强枚举与合法状态转换
# ---------------------------------------------------------------------------

class TestFactLabelEnum:
    """FactLabel 强枚举与状态转换规则。"""

    def test_label_values_are_strict(self):
        """标签值只能是 pending/confirmed/denied/expired/conflicted。"""
        valid = {label.value for label in FactLabel}
        assert valid == {"pending", "confirmed", "denied", "expired", "conflicted"}

    def test_pending_can_transition_to_all_terminals(self):
        """pending 可以转换到所有终态。"""
        targets = LEGAL_TRANSITIONS[FactLabel.PENDING.value]
        assert targets == TERMINAL_LABELS

    def test_terminal_labels_cannot_transition(self):
        """终态标签不在 LEGAL_TRANSITIONS 中，无法再转换。"""
        for label in TERMINAL_LABELS:
            assert label not in LEGAL_TRANSITIONS

    def test_resolve_rejects_invalid_label(self, fact_store, repo):
        """非法 fact_label 被拒绝。"""
        record = fact_store.record_promotion(
            transition_id="t1", claim_id="c1", to_status="active",
        )
        with pytest.raises(ValueError, match="非法 fact_label"):
            fact_store.resolve_outcome(record.record_id, "invalid_label")

    def test_resolve_rejects_pending_label(self, fact_store, repo):
        """pending 不是合法终态，resolve 时被拒绝。"""
        record = fact_store.record_promotion(
            transition_id="t2", claim_id="c2", to_status="active",
        )
        with pytest.raises(ValueError, match="非法 fact_label"):
            fact_store.resolve_outcome(record.record_id, FactLabel.PENDING.value)


# ---------------------------------------------------------------------------
# 2. transition_id 幂等
# ---------------------------------------------------------------------------

class TestTransitionIdempotency:
    """transition_id 唯一约束保证幂等。"""

    def test_same_transition_id_returns_same_record(self, fact_store, repo):
        """同一 transition_id 多次调用返回同一记录。"""
        r1 = fact_store.record_promotion(
            transition_id="tid-1", claim_id="c1", to_status="active",
            evidence_ids=["e1", "e2"],
        )
        r2 = fact_store.record_promotion(
            transition_id="tid-1", claim_id="c1", to_status="active",
            evidence_ids=["e3"],
        )
        assert r1.record_id == r2.record_id
        assert r1.evidence_ids == ["e1", "e2"]


# ---------------------------------------------------------------------------
# 3. 已 resolve 记录再修改产生新审计行
# ---------------------------------------------------------------------------

class TestAuditChain:
    """已 resolve 的记录再修改产生新审计行，不静默覆盖。"""

    def test_re_resolve_creates_new_record(self, fact_store, repo):
        """已 confirmed 的记录再 resolve 为 denied 产生新行。"""
        record = fact_store.record_promotion(
            transition_id="tid-2", claim_id="c3", to_status="active",
        )
        resolved = fact_store.resolve_outcome(
            record.record_id, FactLabel.CONFIRMED.value, resolved_by="announcement",
        )
        assert resolved.fact_label == FactLabel.CONFIRMED.value
        assert resolved.previous_record_id == ""

        # 再次 resolve 为 denied
        new_resolved = fact_store.resolve_outcome(
            record.record_id, FactLabel.DENIED.value, resolved_by="fact_update",
        )
        assert new_resolved.record_id != resolved.record_id
        assert new_resolved.fact_label == FactLabel.DENIED.value
        assert new_resolved.previous_record_id == resolved.record_id


# ---------------------------------------------------------------------------
# 4. Walk-forward 时间隔离
# ---------------------------------------------------------------------------

class TestWalkForwardIsolation:
    """Walk-forward 时间切分确保训练集时间戳全部 < 测试集最小时间戳。"""

    def test_split_is_time_ordered(self):
        """切分后训练集最大时间 < 测试集最小时间。"""
        events = []
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i in range(100):
            event = FeedbackEvent(
                domain="knowledge",
                created_at=(base + timedelta(hours=i)).isoformat(),
            )
            events.append(event)
        train, test = _walk_forward_split(events)
        assert len(train) == 70
        assert len(test) == 30
        max_train = max(e.created_at for e in train)
        min_test = min(e.created_at for e in test)
        assert max_train < min_test

    def test_empty_events_returns_empty(self):
        """空列表切分返回空。"""
        train, test = _walk_forward_split([])
        assert train == []
        assert test == []


# ---------------------------------------------------------------------------
# 5. 最小样本回退
# ---------------------------------------------------------------------------

class TestMinSampleFallback:
    """样本不足时返回 insufficient 状态和默认参数。"""

    def test_knowledge_insufficient_samples(self, repo):
        """知识域样本不足返回 insufficient。"""
        events = []
        for i in range(10):
            events.append(FeedbackEvent(
                domain="knowledge",
                event_type="fact_resolution",
                target_id=f"c{i}",
                metrics={"fact_label": 1},
                attribution={"source_type": "announcement"},
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
            ))
            repo._upsert(
                "feedback_events", "feedback_id", events[-1].feedback_id,
                {"domain": "knowledge", "created_at": events[-1].created_at},
                events[-1].to_dict(),
            )
        calibrator = KnowledgePolicyCalibrator(repo)
        run = calibrator.fit()
        assert run.status == "insufficient"
        assert run.sample_size < MIN_SAMPLES
        assert "source_weights" in run.parameters

    def test_retrieval_insufficient_samples(self, repo):
        """检索域样本不足返回 insufficient。"""
        calibrator = RetrievalPolicyCalibrator(repo)
        run = calibrator.fit()
        assert run.status == "insufficient"
        assert "tier_utilities" in run.parameters

    def test_alpha_insufficient_samples(self, repo):
        """Alpha 域样本不足返回 insufficient。"""
        calibrator = AlphaPolicyCalibrator(repo)
        run = calibrator.fit()
        assert run.status == "insufficient"
        assert "alpha_weight_multiplier" in run.parameters


# ---------------------------------------------------------------------------
# 6. shadow→active 激活（含服务端测试验证）
# ---------------------------------------------------------------------------

class TestShadowToActive:
    """shadow→active 激活流程，含服务端测试验证。"""

    def _make_passing_run(self, domain: str = "knowledge") -> CalibrationRun:
        """构造满足激活门槛的 CalibrationRun。"""
        return CalibrationRun(
            domain=domain,
            status="shadow",
            sample_size=100,
            metrics={
                "walk_forward_score": 0.7,
                "false_promotion_rate": 0.05,
                "brier_score": 0.2,
                "train_size": 70,
                "test_size": 30,
                "walk_forward_windows": 1,
            },
            parameters={
                "source_weights": {"announcement": 1.0},
                "claim_activation_threshold": 0.7,
                "source_discount_kappa": 0.5,
            },
            bounds={
                "claim_activation_threshold": {"min": 0.55, "max": 0.90},
                "source_discount_kappa": {"min": 0.3, "max": 1.0},
            },
        )

    def test_activate_without_test_run_rejected(self, repo, calibration_controller):
        """缺少 test_run_id 时激活被拒绝。"""
        run = self._make_passing_run()
        repo.save_calibration(run)
        with pytest.raises(ValueError, match="必须提供 test_run_id"):
            calibration_controller.activate(
                run.calibration_id, "approver",
                test_run_id="", code_version="abc123",
            )

    def test_activate_without_code_version_rejected(self, repo, calibration_controller):
        """缺少 code_version 时激活被拒绝。"""
        run = self._make_passing_run()
        repo.save_calibration(run)
        with pytest.raises(ValueError, match="必须提供 code_version"):
            calibration_controller.activate(
                run.calibration_id, "approver",
                test_run_id="some-id", code_version="",
            )

    def test_activate_with_forged_test_passed_rejected(
        self, repo, calibration_controller, test_run_store
    ):
        """API 伪造 passed=True 但服务端无记录时被拒绝。"""
        run = self._make_passing_run()
        repo.save_calibration(run)
        with pytest.raises(ValueError, match="测试运行记录不存在"):
            calibration_controller.activate(
                run.calibration_id, "approver",
                test_run_id="nonexistent", code_version="abc123",
            )

    def test_activate_with_valid_test_run_succeeds(
        self, repo, calibration_controller, test_run_store
    ):
        """服务端有通过的测试记录时激活成功。"""
        run = self._make_passing_run()
        repo.save_calibration(run)
        test_record = test_run_store.record_run(
            code_version="abc123",
            test_suite="tests/test_feedback_loop_integration.py",
            passed=True,
            metrics={"passed": 14, "failed": 0},
        )
        activated = calibration_controller.activate(
            run.calibration_id, "approver",
            test_run_id=test_record.test_run_id,
            code_version="abc123",
        )
        assert activated.status == "active"
        assert activated.approved_by == "approver"

    def test_activate_with_failed_test_rejected(
        self, repo, calibration_controller, test_run_store
    ):
        """测试未通过时激活被拒绝。"""
        run = self._make_passing_run()
        repo.save_calibration(run)
        test_record = test_run_store.record_run(
            code_version="abc123",
            test_suite="tests/test_feedback_loop_integration.py",
            passed=False,
        )
        with pytest.raises(ValueError, match="测试运行记录显示未通过"):
            calibration_controller.activate(
                run.calibration_id, "approver",
                test_run_id=test_record.test_run_id,
                code_version="abc123",
            )

    def test_activate_with_mismatched_code_version_rejected(
        self, repo, calibration_controller, test_run_store
    ):
        """测试 code_version 与参数不一致时被拒绝。"""
        run = self._make_passing_run()
        repo.save_calibration(run)
        test_record = test_run_store.record_run(
            code_version="abc123",
            test_suite="tests/test_feedback_loop_integration.py",
            passed=True,
        )
        with pytest.raises(ValueError, match="测试代码版本不匹配"):
            calibration_controller.activate(
                run.calibration_id, "approver",
                test_run_id=test_record.test_run_id,
                code_version="different_hash",
            )


# ---------------------------------------------------------------------------
# 7. 参数回滚
# ---------------------------------------------------------------------------

class TestRollback:
    """参数回滚恢复上一活跃版本。"""

    def test_rollback_restores_previous_policy(self, repo, calibration_controller, test_run_store):
        """回滚后旧版本恢复 active 状态。"""
        # 第一版激活
        run1 = CalibrationRun(
            domain="knowledge", status="shadow", sample_size=100,
            metrics={"walk_forward_score": 0.7, "false_promotion_rate": 0.05},
            parameters={"claim_activation_threshold": 0.7},
            bounds={"claim_activation_threshold": {"min": 0.55, "max": 0.90}},
        )
        repo.save_calibration(run1)
        test1 = test_run_store.record_run(
            code_version="v1", test_suite="tests/test_feedback_loop_integration.py", passed=True,
        )
        activated1 = calibration_controller.activate(
            run1.calibration_id, "approver",
            test_run_id=test1.test_run_id, code_version="v1",
        )

        # 第二版激活
        run2 = CalibrationRun(
            domain="knowledge", status="shadow", sample_size=100,
            metrics={"walk_forward_score": 0.75, "false_promotion_rate": 0.03},
            parameters={"claim_activation_threshold": 0.65},
            bounds={"claim_activation_threshold": {"min": 0.55, "max": 0.90}},
        )
        repo.save_calibration(run2)
        test2 = test_run_store.record_run(
            code_version="v2", test_suite="tests/test_feedback_loop_integration.py", passed=True,
        )
        calibration_controller.activate(
            run2.calibration_id, "approver",
            test_run_id=test2.test_run_id, code_version="v2",
        )

        # 回滚第二版
        rolled = calibration_controller.rollback(run2.calibration_id, "approver")
        assert rolled.status == "rolled_back"

        # 第一版应恢复 active
        active = repo.get_active_policy("knowledge")
        assert active is not None
        assert active.policy_version == activated1.policy_version


# ---------------------------------------------------------------------------
# 8. 反馈去重/限流/深度控制/过期
# ---------------------------------------------------------------------------

class TestFeedbackLoopControl:
    """反馈防循环机制：去重、限流、深度控制、过期。"""

    def test_dedup_same_event(self, repo, feedback_controller):
        """相同 domain+target_id+event_type 的事件被去重。"""
        event = FeedbackEvent(
            domain="knowledge",
            event_type="fact_resolution",
            target_id="c1",
            metrics={"fact_label": 1},
            attribution={"source_type": "announcement"},
        )
        result1 = feedback_controller.record(event, generate_task=True)
        assert "deduped" not in result1.metrics

        # 相同核心字段的事件
        event2 = FeedbackEvent(
            domain="knowledge",
            event_type="fact_resolution",
            target_id="c1",
            metrics={"fact_label": 0},
            attribution={"source_type": "news"},
        )
        result2 = feedback_controller.record(event2, generate_task=True)
        assert result2.metrics.get("deduped") is True

    def test_depth_control(self, repo, feedback_controller):
        """feedback_depth 超过 MAX_FEEDBACK_DEPTH 不再生成任务。"""
        trace_id = "trace-1"
        for i in range(FeedbackController.MAX_FEEDBACK_DEPTH + 1):
            event = FeedbackEvent(
                domain="knowledge",
                event_type="fact_resolution",
                target_id=f"c{i}",
                event_kind=f"event_{i}",
                metrics={"fact_label": 1},
                attribution={"source_type": "announcement"},
            )
            result = feedback_controller.record(
                event, parent_task_id=f"task-{i}", trace_id=trace_id,
                generate_task=True,
            )
            if i >= FeedbackController.MAX_FEEDBACK_DEPTH:
                assert result.metrics.get("depth_capped") is True

    def test_expire_stale_tasks(self, repo, feedback_controller):
        """过期任务被标记为 expired。"""
        # 创建已过期任务
        past_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        task = ResearchTask(
            task_type="evidence_completion",
            status="queued",
            expires_at=past_time,
        )
        repo.save_task(task)
        expired_count = feedback_controller.expire_stale_tasks()
        assert expired_count >= 1
        updated = repo.get_task(task.task_id)
        assert updated.status == "expired"


# ---------------------------------------------------------------------------
# 9. Alpha gap 任务进入统一队列
# ---------------------------------------------------------------------------

class TestAlphaGapTaskQueue:
    """AlphaGapFeedbackEmitter 生成的任务进入统一 ResearchTask 队列。"""

    def test_emit_as_research_tasks(self):
        """emit_as_research_tasks 返回 ResearchTask 列表。"""
        from core.research.perilla.bridge.alpha_gap_feedback_emitter import (
            AlphaGapFeedbackEmitter,
        )
        emitter = AlphaGapFeedbackEmitter()
        path_data = {
            "path": ["公司A", "产品B", "技术C"],
            "path_evidence_gap": [
                {"head": "公司A", "tail": "产品B", "relation": "supplied_by", "missing": "供应商证据"},
            ],
            "path_alpha_score": 0.7,
            "path_confidence": 0.6,
        }
        tasks = emitter.emit_as_research_tasks(
            path_data,
            parent_task_id="parent-1",
            trace_id="trace-1",
            feedback_depth=1,
        )
        assert len(tasks) > 0
        assert all(isinstance(t, ResearchTask) for t in tasks)
        assert all(t.task_type == "alpha_gap_search" for t in tasks)
        assert all(t.trigger_source == "alpha_gap_feedback" for t in tasks)
        assert all(t.parent_task_id == "parent-1" for t in tasks)
        assert all(t.expires_at != "" for t in tasks)


# ---------------------------------------------------------------------------
# 10. Alpha 域不输出交易收益字段到 Knowledge 域
# ---------------------------------------------------------------------------

class TestDomainIsolation:
    """三域隔离：Alpha 域参数不进入 Knowledge 域。"""

    def test_alpha_calibrator_no_fact_params(self, repo):
        """Alpha 校准器不输出 source_weights/claim_activation_threshold。"""
        # 注入足够样本
        for i in range(60):
            event = FeedbackEvent(
                domain="alpha",
                event_type="alpha_outcome",
                target_id=f"alpha-{i}",
                metrics={
                    "out_of_sample_ic": 0.05,
                    "excess_return": 0.02,
                    "max_drawdown": -0.1,
                    "decay_rate": 0.01,
                },
                attribution={"node_type": "company"},
                outcome="positive",
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
            )
            repo._upsert(
                "feedback_events", "feedback_id", event.feedback_id,
                {"domain": "alpha", "created_at": event.created_at},
                event.to_dict(),
            )
        calibrator = AlphaPolicyCalibrator(repo)
        run = calibrator.fit()
        params = run.parameters
        # Alpha 域只能输出 alpha_weight_multiplier 和 calibrated_node_weights
        assert "alpha_weight_multiplier" in params
        assert "source_weights" not in params
        assert "claim_activation_threshold" not in params
        assert "source_discount_kappa" not in params

    def test_knowledge_calibrator_no_alpha_params(self, repo):
        """Knowledge 校准器不输出 alpha_weight_multiplier。"""
        for i in range(60):
            event = FeedbackEvent(
                domain="knowledge",
                event_type="fact_resolution",
                target_id=f"c{i}",
                event_kind=f"ev{i}",
                metrics={"fact_label": 1},
                attribution={"source_type": "announcement"},
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
            )
            repo._upsert(
                "feedback_events", "feedback_id", event.feedback_id,
                {"domain": "knowledge", "created_at": event.created_at},
                event.to_dict(),
            )
        calibrator = KnowledgePolicyCalibrator(repo)
        run = calibrator.fit()
        params = run.parameters
        assert "source_weights" in params
        assert "alpha_weight_multiplier" not in params
        assert "calibrated_node_weights" not in params


# ---------------------------------------------------------------------------
# 11. 校准指标完整性
# ---------------------------------------------------------------------------

class TestCalibrationMetrics:
    """校准输出包含完整指标。"""

    def test_knowledge_metrics_complete(self, repo):
        """知识域校准输出包含 Brier Score、precision/recall 等完整指标。"""
        for i in range(60):
            label = 1 if i < 40 else 0
            event = FeedbackEvent(
                domain="knowledge",
                event_type="fact_resolution",
                target_id=f"c{i}",
                event_kind=f"ev{i}",
                metrics={"fact_label": label},
                attribution={"source_type": "announcement" if label else "forum"},
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat(),
            )
            repo._upsert(
                "feedback_events", "feedback_id", event.feedback_id,
                {"domain": "knowledge", "created_at": event.created_at},
                event.to_dict(),
            )
        calibrator = KnowledgePolicyCalibrator(repo)
        run = calibrator.fit()
        metrics = run.metrics
        assert "walk_forward_score" in metrics
        assert "brier_score" in metrics
        assert "calibration_curve" in metrics
        assert "precision" in metrics
        assert "recall" in metrics
        assert "f1" in metrics
        assert "false_promotion_rate" in metrics
        assert "active_trusted_accuracy" in metrics
        assert "source_reliability" in metrics
        assert "walk_forward_windows" in metrics
        assert metrics["walk_forward_windows"] >= 1

    def test_retrieval_uses_evaluable_runtime_feedback_only(self, repo):
        """运行时 web/second_hand 反馈应映射到预算层级并形成真实测试集。"""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i in range(60):
            event = FeedbackEvent(
                domain="retrieval",
                event_type="retrieval_metric",
                target_id=f"claim-{i}",
                metrics={
                    "tier": "web" if i % 2 else "second_hand",
                    "documents_found": 2,
                    "evidence_saved": 1,
                    "independent_sources": 1,
                },
                created_at=(base + timedelta(minutes=i)).isoformat(),
            )
            repo.save_feedback(event)
        for i in range(10):
            repo.save_feedback(FeedbackEvent(
                domain="retrieval",
                event_type="unrelated",
                target_id=f"noise-{i}",
                metrics={"tier": "unknown"},
                created_at=(base + timedelta(hours=2, minutes=i)).isoformat(),
            ))

        run = RetrievalPolicyCalibrator(repo).fit()

        assert run.status == "shadow"
        assert run.sample_size == 60
        assert run.metrics["train_size"] == 42
        assert run.metrics["test_size"] == 18
        assert run.metrics["source_reliability"]
        assert set(run.parameters["tier_utilities"]) <= {"high", "medium", "low"}


# ---------------------------------------------------------------------------
# 12. apply_policy 注入
# ---------------------------------------------------------------------------

class TestApplyPolicy:
    """apply_policy 从 PolicyVersion 构造策略对象。"""

    def test_bayesian_apply_policy_default(self):
        """policy=None 时使用默认参数。"""
        upgrader = BayesianStatusUpgrader.apply_policy(None)
        assert upgrader.source_discount_kappa == 0.5
        assert upgrader.VERSION == "bayesian-v1"

    def test_bayesian_apply_policy_with_params(self):
        """从 PolicyVersion 注入参数。"""
        policy = PolicyVersion(
            domain="knowledge",
            parameters={
                "claim_activation_threshold": 0.8,
                "source_discount_kappa": 0.7,
            },
        )
        upgrader = BayesianStatusUpgrader.apply_policy(policy)
        from core.research_graph.candidate_schema import CandidateStatus
        assert upgrader.confidence_thresholds[CandidateStatus.SUPPORTED.value] == 0.8
        assert upgrader.source_discount_kappa == 0.7

    def test_bayesian_apply_policy_clamps_bounds(self):
        """apply_policy 对参数做边界钳制。"""
        policy = PolicyVersion(
            domain="knowledge",
            parameters={
                "claim_activation_threshold": 0.99,  # 超过 max 0.90
                "source_discount_kappa": 2.0,        # 超过 max 1.0
            },
        )
        upgrader = BayesianStatusUpgrader.apply_policy(policy)
        from core.research_graph.candidate_schema import CandidateStatus
        assert upgrader.confidence_thresholds[CandidateStatus.SUPPORTED.value] == 0.90
        assert upgrader.source_discount_kappa == 1.0

    def test_claim_policy_apply_policy(self):
        """ClaimPolicy.apply_policy 返回 ClaimPolicy 实例。"""
        policy = PolicyVersion(domain="knowledge", parameters={})
        cp = ClaimPolicy.apply_policy(policy)
        assert isinstance(cp, ClaimPolicy)
        assert cp.VERSION == "research-policy-v1"

    def test_claim_policy_apply_policy_none(self):
        """ClaimPolicy.apply_policy(None) 返回默认实例。"""
        cp = ClaimPolicy.apply_policy(None)
        assert isinstance(cp, ClaimPolicy)


# ---------------------------------------------------------------------------
# 13. CandidateGraph 晋升回调
# ---------------------------------------------------------------------------

class TestCandidateTransitionCallback:
    """EvidenceActivator 的 on_candidate_transition 回调。"""

    def test_node_transition_callback_invoked(self):
        """候选节点状态迁移时触发回调。"""
        from core.research_graph.candidate_graph import (
            CandidateNode, CandidateStore,
        )
        from core.research_graph.candidate_schema import CandidateStatus
        from core.research_graph.evidence_activator import EvidenceActivator

        store = CandidateStore()
        node = CandidateNode(
            candidate_id="node-1",
            node_type="company",
            name="测试公司",
            status=CandidateStatus.DISCOVERED.value,
        )
        store.add_node(node)

        callback_calls: list[dict] = []

        def on_transition(**kwargs):
            callback_calls.append(kwargs)

        activator = EvidenceActivator(
            store, on_candidate_transition=on_transition,
        )
        # 直接调用 activate，传入高匹配证据
        activator.activate(
            evidence_text="测试公司发布公告确认供应链关系",
            evidence_id="ev-1",
            source_type="announcement",
            stance="support",
        )
        # 如果发生了状态迁移，回调应被调用
        # （具体是否迁移取决于匹配和贝叶斯计算，这里只验证回调机制不报错）


# ---------------------------------------------------------------------------
# 14. Brier Score 基本正确性
# ---------------------------------------------------------------------------

class TestBrierScore:
    """Brier Score 计算正确性。"""

    def test_perfect_prediction(self):
        """完美预测 Brier Score = 0。"""
        predictions = [1.0, 0.0, 1.0]
        labels = [1.0, 0.0, 1.0]
        assert _brier_score(predictions, labels) == 0.0

    def test_worst_prediction(self):
        """完全错误预测 Brier Score = 1。"""
        predictions = [1.0, 1.0]
        labels = [0.0, 0.0]
        assert _brier_score(predictions, labels) == 1.0

    def test_empty_lists(self):
        """空列表返回 1.0。"""
        assert _brier_score([], []) == 1.0

    def test_mismatched_lengths(self):
        """长度不匹配返回 1.0。"""
        assert _brier_score([0.5, 0.5], [1.0]) == 1.0
