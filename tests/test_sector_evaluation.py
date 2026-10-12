"""Phase 2 板块轮动经济有效性研究框架测试。

覆盖 Task 2.1-2.5：
- 2.1: 旧规则降级为 feature/shadow（feature_version/policy_version 标记）
- 2.2: 每日 PIT 特征持久化
- 2.3: 未来表现标签只在 evaluation 层生成
- 2.4: walk-forward 复用 adaptation.engine + 5 种基准对比
- 2.5: 评价标准通过 policy_versions 配置
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.quant.contracts import SectorAssessment, SectorPhase
from core.quant.sector.evaluation import (
    BASELINE_NAMES,
    EvaluationLabel,
    EvaluationLabelStore,
    PolicyVersion,
    PolicyVersionStore,
    SectorFeatureRecord,
    SectorFeatureStore,
    SectorRotationEvaluator,
)
from core.quant.sector.evaluation.contracts import (
    LEGACY_DAILY_SCREENER_FEATURE_VERSION,
    LEGACY_DAILY_SCREENER_POLICY_VERSION,
    LEGACY_REVERSAL_DETECTOR_FEATURE_VERSION,
    LEGACY_REVERSAL_DETECTOR_POLICY_VERSION,
    LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
    LEGACY_SECTOR_ENGINE_POLICY_VERSION,
    make_legacy_daily_screener_policy,
    make_legacy_reversal_detector_policy,
    make_legacy_sector_engine_policy,
)
from core.quant.sector.sector_engine import SectorRotationEngine
from core.quant.theme.daily_screener import ThemeScreeningResult


# ======================================================================
# Task 2.1: 旧规则降级为 feature/shadow
# ======================================================================


class TestLegacyRuleShadowMarking:
    """旧规则降级为 feature/shadow 测试。"""

    def test_sector_assessment_has_feature_and_policy_version_fields(self):
        """SectorAssessment 包含 feature_version 和 policy_version 字段。"""
        assessment = SectorAssessment(
            sector_id="test",
            feature_version="sector_engine_v1_legacy",
            policy_version="policy_sector_legacy_shadow_v1",
        )
        assert assessment.feature_version == "sector_engine_v1_legacy"
        assert assessment.policy_version == "policy_sector_legacy_shadow_v1"

    def test_theme_screening_result_has_feature_and_policy_version_fields(self):
        """ThemeScreeningResult 包含 feature_version 和 policy_version 字段。"""
        result = ThemeScreeningResult(
            candidate_id="c1",
            feature_version="daily_screener_v1_legacy",
            policy_version="policy_screener_legacy_shadow_v1",
        )
        assert result.feature_version == "daily_screener_v1_legacy"
        assert result.policy_version == "policy_screener_legacy_shadow_v1"

    def test_legacy_sector_engine_policy_is_shadow(self):
        """旧七阶段板块引擎的 PolicyVersion 默认 status='shadow'。"""
        policy = make_legacy_sector_engine_policy()
        assert policy.status == "shadow"
        assert policy.feature_version == LEGACY_SECTOR_ENGINE_FEATURE_VERSION
        assert policy.policy_version == LEGACY_SECTOR_ENGINE_POLICY_VERSION
        # 参数与治理元数据分离
        assert "rally_threshold" in policy.parameters
        assert "min_sample_size" in policy.activation_criteria

    def test_legacy_reversal_detector_policy_is_shadow(self):
        """旧冰点反转检测器的 PolicyVersion 默认 status='shadow'。"""
        policy = make_legacy_reversal_detector_policy()
        assert policy.status == "shadow"
        assert policy.feature_version == LEGACY_REVERSAL_DETECTOR_FEATURE_VERSION
        assert policy.policy_version == LEGACY_REVERSAL_DETECTOR_POLICY_VERSION

    def test_legacy_daily_screener_policy_is_shadow(self):
        """旧主题日线筛选器的 PolicyVersion 默认 status='shadow'。"""
        policy = make_legacy_daily_screener_policy()
        assert policy.status == "shadow"
        assert policy.feature_version == LEGACY_DAILY_SCREENER_FEATURE_VERSION
        assert policy.policy_version == LEGACY_DAILY_SCREENER_POLICY_VERSION

    def test_sector_engine_marks_feature_version_in_assessment(self):
        """sector_engine._classify_phase 输出的 SectorAssessment 标记 feature_version。"""
        # 构造最小可用的 SectorRotationEngine
        engine = SectorRotationEngine(
            sector_map={"测试板块": ["000001", "000002", "000003", "000004", "000005"]},
            min_stocks_per_sector=1,
        )
        # 构造最小 stock_pool_today DataFrame
        import pandas as pd
        stock_pool = pd.DataFrame({
            "code": ["000001", "000002", "000003", "000004", "000005"],
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
            "open": [9.5, 10.5, 11.5, 12.5, 13.5],
            "high": [10.5, 11.5, 12.5, 13.5, 14.5],
            "low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "pre_close": [9.8, 10.8, 11.8, 12.8, 13.8],
            "volume": [1000, 2000, 3000, 4000, 5000],
        })
        results = engine.assess(stock_pool, "20260710")
        assert "测试板块" in results
        assessment = results["测试板块"]
        assert assessment.feature_version == LEGACY_SECTOR_ENGINE_FEATURE_VERSION
        assert assessment.policy_version == LEGACY_SECTOR_ENGINE_POLICY_VERSION


# ======================================================================
# Task 2.2: 每日 PIT 特征持久化
# ======================================================================


class TestSectorFeatureStore:
    """每日 PIT 板块特征持久化测试。"""

    def test_save_and_query_feature(self, tmp_path: Path):
        """保存和查询板块特征。"""
        store = SectorFeatureStore(db_path=tmp_path / "features.db")
        record = SectorFeatureRecord(
            as_of="20260710",
            sector_id="HBM",
            sector_name="HBM 概念",
            phase="startup",
            phase_confidence=0.7,
            momentum_score=0.6,
            breadth_score=0.8,
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            policy_version=LEGACY_SECTOR_ENGINE_POLICY_VERSION,
        )

        assert store.save(record) is True
        # 重复保存幂等跳过
        assert store.save(record) is False

        # PIT 查询：返回 as_of <= requested 的版本
        results = store.get_features_at("20260711", sector_id="HBM")
        assert len(results) == 1
        assert results[0].sector_id == "HBM"
        assert results[0].phase == "startup"

    def test_pit_rejects_future_as_of(self, tmp_path: Path):
        """PIT 检查：不得写入未来日期。"""
        store = SectorFeatureStore(db_path=tmp_path / "features.db")
        # 构造一个未来日期的记录
        from datetime import datetime, timezone, timedelta
        future_date = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y%m%d")
        record = SectorFeatureRecord(
            as_of=future_date,
            sector_id="HBM",
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
        )

        # PIT 检查应拒绝写入
        assert store.save(record) is False

    def test_pit_query_does_not_return_future(self, tmp_path: Path):
        """PIT 查询不返回未来版本。"""
        store = SectorFeatureStore(db_path=tmp_path / "features.db")
        # 保存一个 20260710 的记录
        record = SectorFeatureRecord(
            as_of="20260710",
            sector_id="HBM",
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
        )
        store.save(record)

        # 查询 20260709（早于记录），不应返回
        results = store.get_features_at("20260709", sector_id="HBM")
        assert len(results) == 0

    def test_list_dates(self, tmp_path: Path):
        """列出所有已持久化的日期。"""
        store = SectorFeatureStore(db_path=tmp_path / "features.db")
        for date in ["20260708", "20260709", "20260710"]:
            store.save(SectorFeatureRecord(
                as_of=date,
                sector_id="HBM",
                feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            ))

        dates = store.list_dates()
        assert dates == ["20260708", "20260709", "20260710"]


# ======================================================================
# Task 2.3: 未来表现标签只在 evaluation 层生成
# ======================================================================


class TestEvaluationLabelStore:
    """未来表现标签持久化测试。"""

    def test_save_and_query_label(self, tmp_path: Path):
        """保存和查询未来表现标签。"""
        store = EvaluationLabelStore(db_path=tmp_path / "labels.db")
        label = EvaluationLabel(
            as_of="20260710",
            sector_id="HBM",
            horizon_days=5,
            forward_return=2.5,
            forward_max_drawdown=-1.2,
            benchmark_return=1.0,
            excess_return=1.5,
            generated_at="20260716",  # as_of + 6 天，晚于 horizon_days=5
        )

        assert store.save(label) is True
        # 重复保存幂等跳过
        assert store.save(label) is False

        results = store.get_labels_at("20260710", sector_id="HBM")
        assert len(results) == 1
        assert results[0].forward_return == 2.5
        assert results[0].excess_return == 1.5

    def test_pit_rejects_future_function(self, tmp_path: Path):
        """PIT 检查：generated_at 不得早于 as_of + horizon_days。"""
        store = EvaluationLabelStore(db_path=tmp_path / "labels.db")
        # horizon_days=5，但 generated_at 只比 as_of 晚 3 天 → 未来函数
        label = EvaluationLabel(
            as_of="20260710",
            sector_id="HBM",
            horizon_days=5,
            forward_return=2.5,
            generated_at="20260713",  # as_of + 3 天，早于 horizon_days=5
        )

        assert store.save(label) is False

    def test_label_does_not_modify_feature_record(self, tmp_path: Path):
        """标签保存不修改 SectorFeatureRecord（不回写事实层）。"""
        feature_store = SectorFeatureStore(db_path=tmp_path / "features.db")
        label_store = EvaluationLabelStore(db_path=tmp_path / "labels.db")

        # 保存特征
        feature = SectorFeatureRecord(
            as_of="20260710",
            sector_id="HBM",
            phase="startup",
            momentum_score=0.6,
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
        )
        feature_store.save(feature)

        # 保存标签
        label = EvaluationLabel(
            as_of="20260710",
            sector_id="HBM",
            horizon_days=5,
            forward_return=2.5,
            generated_at="20260716",
        )
        label_store.save(label)

        # 查询特征，确认 phase 和 momentum_score 未被标签修改
        features = feature_store.get_features_at("20260711", sector_id="HBM")
        assert len(features) == 1
        assert features[0].phase == "startup"
        assert features[0].momentum_score == 0.6


# ======================================================================
# Task 2.4: walk-forward + 基准对比
# ======================================================================


class TestSectorRotationEvaluator:
    """walk-forward + 基准对比评价器测试。"""

    def _build_synthetic_data(self, num_days: int = 90) -> tuple[list, list]:
        """构造合成特征和标签数据。"""
        features: list[SectorFeatureRecord] = []
        labels: list[EvaluationLabel] = []
        sectors = ["HBM", "AI", "CPO", "新能源", "医药"]
        for day_offset in range(num_days):
            date = f"2026{day_offset // 30 + 1:02d}{day_offset % 30 + 1:02d}"
            for sector_id in sectors:
                features.append(SectorFeatureRecord(
                    as_of=date,
                    sector_id=sector_id,
                    phase="startup",
                    momentum_score=0.3 + (day_offset % 10) * 0.05,
                    breadth_score=0.4 + (day_offset % 8) * 0.05,
                    feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
                ))
                # 标签生成日期比特征日期晚 horizon_days + 1 天
                gen_day = day_offset + 6
                gen_date = f"2026{gen_day // 30 + 1:02d}{gen_day % 30 + 1:02d}"
                labels.append(EvaluationLabel(
                    as_of=date,
                    sector_id=sector_id,
                    horizon_days=5,
                    forward_return=1.0 + (day_offset % 5) * 0.2,
                    benchmark_return=0.5,
                    excess_return=0.5 + (day_offset % 5) * 0.2,
                    generated_at=gen_date,
                ))
        return features, labels

    def test_walk_forward_reuses_adaptation_engine(self):
        """walk-forward 复用 adaptation.engine.walk_forward_windows()。"""
        from core.research.adaptation.engine import walk_forward_windows
        dates = ["20260101", "20260102", "20260103", "20260104", "20260105",
                 "20260106", "20260107", "20260108", "20260109", "20260110"]
        windows = walk_forward_windows(dates, train_size=5, test_size=2)
        assert len(windows) >= 1
        assert windows[0]["train_start"] == "20260101"
        assert windows[0]["train_end"] == "20260105"
        assert windows[0]["test_start"] == "20260106"

    def test_evaluator_has_five_baselines(self):
        """评价器包含 5 种基准。"""
        # BASELINE_NAMES 应包含至少 5 种基准
        assert len(BASELINE_NAMES) >= 5
        assert "current_theme_rule" in BASELINE_NAMES
        assert "simple_relative_strength" in BASELINE_NAMES
        assert "simple_reversal" in BASELINE_NAMES
        assert "random_equal_weight" in BASELINE_NAMES
        assert "with_research_gate" in BASELINE_NAMES

    def test_evaluate_returns_report_with_disclaimer(self):
        """evaluate 返回包含免责声明的报告。"""
        features, labels = self._build_synthetic_data(num_days=90)
        evaluator = SectorRotationEvaluator(train_size=30, test_size=10)
        report = evaluator.evaluate(
            features, labels,
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            policy_version=LEGACY_SECTOR_ENGINE_POLICY_VERSION,
        )

        assert report.feature_version == LEGACY_SECTOR_ENGINE_FEATURE_VERSION
        assert "模拟评价" in report.disclaimer
        assert "不构成投资建议" in report.disclaimer

    def test_insufficient_sample_output(self):
        """样本不足时输出 insufficient_sample。"""
        # 只提供少量数据
        features, labels = self._build_synthetic_data(num_days=5)
        evaluator = SectorRotationEvaluator(train_size=60, test_size=20)
        report = evaluator.evaluate(features, labels)

        # 样本不足，windows 为空
        assert len(report.windows) == 0

    def test_alpha_evaluation_does_not_modify_claim(self):
        """Alpha 评价不修改 Claim 事实置信度。

        评价器只读取特征和标签，不写入任何 Claim 或事实层。
        """
        features, labels = self._build_synthetic_data(num_days=90)
        evaluator = SectorRotationEvaluator(train_size=30, test_size=10)

        # 评价器不暴露任何修改 Claim 的方法
        assert not hasattr(evaluator, "update_claim")
        assert not hasattr(evaluator, "modify_fact_confidence")

        report = evaluator.evaluate(features, labels)
        # 报告中不包含 Claim 修改记录
        report_dict = report.to_dict()
        assert "claim_updates" not in report_dict
        assert "fact_modifications" not in report_dict


# ======================================================================
# Task 2.5: 评价标准通过 policy_versions 配置
# ======================================================================


class TestPolicyVersionStore:
    """PolicyVersion 配置化存储测试。"""

    def test_bootstrap_legacy_policies(self, tmp_path: Path):
        """bootstrap 初始化旧规则的 shadow PolicyVersion（幂等）。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")

        # 旧规则应被自动 bootstrap
        for policy_version in [
            LEGACY_SECTOR_ENGINE_POLICY_VERSION,
            LEGACY_REVERSAL_DETECTOR_POLICY_VERSION,
            LEGACY_DAILY_SCREENER_POLICY_VERSION,
        ]:
            policy = store.get_by_policy_version(policy_version)
            assert policy is not None
            assert policy.status == "shadow"

        # 重复初始化幂等
        store2 = PolicyVersionStore(db_path=tmp_path / "policy.db")
        policy = store2.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        assert policy is not None

    def test_activate_requires_approved_by_and_test_run_id(self, tmp_path: Path):
        """激活需要 approved_by 和 test_run_id。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        assert policy is not None

        # 缺少 approved_by 应拒绝
        result = store.activate(
            policy.policy_version_id,
            approved_by="",
            test_run_id="tr_001",
        )
        assert result is None

        # 缺少 test_run_id 应拒绝
        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="",
        )
        assert result is None

    def test_activate_success_sets_active(self, tmp_path: Path):
        """激活成功后 status 设为 active。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        assert policy is not None

        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer_001",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )
        assert result is not None
        assert result.status == "active"
        assert result.approved_by == "reviewer_001"
        assert result.test_run_id == "tr_001"
        assert result.activated_at != ""

    def test_activate_rejects_lowered_criteria(self, tmp_path: Path):
        """新参数不能降低自身的接受标准。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")
        # 先激活一个旧规则版本
        old_policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        store.activate(
            old_policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
        )

        # 创建一个降低 activation_criteria 的新版本
        new_policy = PolicyVersion(
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            policy_version="policy_sector_v2_lowered",
            parameters={"rally_threshold": 2.0},
            activation_criteria={
                "min_sample_size": 30.0,  # 从 60.0 降到 30.0
                "min_ic": 0.03,
                "min_sharpe": 0.5,
            },
            status="shadow",
        )
        store.save(new_policy)

        # 激活应被拒绝（降低了 min_sample_size）
        result = store.activate(
            new_policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_002",
        )
        assert result is None

    def test_rollback_requires_previous_version(self, tmp_path: Path):
        """回滚需要 previous_version，无 previous_version 时拒绝。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        assert policy is not None

        # 首次激活后回滚，previous_version 为空，应拒绝
        store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
        )
        result = store.rollback(policy.policy_version_id)
        assert result is None

    def test_rollback_to_previous_version(self, tmp_path: Path):
        """回滚到前一版本。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")
        # 激活第一个版本
        v1 = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        store.activate(
            v1.policy_version_id,
            approved_by="reviewer_001",
            test_run_id="tr_001",
        )

        # 创建并激活第二个版本
        v2 = PolicyVersion(
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            policy_version="policy_sector_v2",
            parameters={"rally_threshold": 2.0},
            activation_criteria={
                "min_sample_size": 60.0,
                "min_ic": 0.05,  # 提高了标准
                "min_sharpe": 0.5,
            },
            status="shadow",
        )
        store.save(v2)
        activated_v2 = store.activate(
            v2.policy_version_id,
            approved_by="reviewer_002",
            test_run_id="tr_002",
        )
        assert activated_v2 is not None

        # 回滚 v2 到 v1
        rolled_back = store.rollback(v2.policy_version_id)
        assert rolled_back is not None
        assert rolled_back.policy_version_id == v1.policy_version_id
        assert rolled_back.status == "active"

    def test_policy_parameters_separated_from_governance(self, tmp_path: Path):
        """PolicyVersion 分离参数与治理元数据。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        assert policy is not None

        # parameters 只包含策略参数，不包含治理元数据
        assert "rally_threshold" in policy.parameters
        assert "approved_by" not in policy.parameters
        assert "test_run_id" not in policy.parameters
        assert "activation_criteria" not in policy.parameters

        # activation_criteria 只包含激活条件
        assert "min_sample_size" in policy.activation_criteria
        assert "rally_threshold" not in policy.activation_criteria

    def test_shadow_does_not_affect_production(self, tmp_path: Path):
        """shadow 状态不影响生产评分。"""
        store = PolicyVersionStore(db_path=tmp_path / "policy.db")
        # 旧规则默认 shadow
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        assert policy.status == "shadow"

        # 查询 active 版本应返回 None（没有 active 版本）
        active = store.get_active_by_feature(LEGACY_SECTOR_ENGINE_FEATURE_VERSION)
        assert active is None
