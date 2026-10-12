"""Task 4: 分层算法筛选测试.

覆盖:
1. 配置加载与 hash
2. StageConfig 不可变
3. Smoke/Pilot/Calibration 阶段淘汰规则
4. 冻结决策（主方法 + 敏感性方法）
5. checkpoint 重启恢复
6. seed 互斥
7. 指标记录与统计计算

所有测试用固定 seed, 不写入 data/, 临时文件用 tmp_path.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import numpy as np
import pytest

from experiments.causal_mechanism_routing.algorithm_selector import (
    CALIBRATION_SEED_BASE,
    FROZEN_NULL_SEED_BASE,
    PILOT_SEED_BASE,
    SMOKE_SEED_BASE,
    CandidateEvaluation,
    StageConfig,
    StageReport,
    compute_p_value_tail_stats,
    fails_pilot_criteria,
    fails_smoke_criteria,
    load_profiles,
    run_stage_calibration,
    run_stage_pilot,
    run_stage_smoke,
    select_frozen_methods,
)
from experiments.causal_mechanism_routing.finite_sample_inference import (
    HC3NormalMethod,
    InferenceResult,
)
from experiments.causal_mechanism_routing.experiment_scheduler import (
    ExperimentScheduler,
    ResourceLimits,
    SchedulerState,
    ChunkCheckpoint,
    StageResult,
    PAUSED_STATES,
    TERMINAL_STATES,
    PRODUCTION_PATHS,
    PROFILE_STAGES,
    _compute_max_threads,
    _parse_hhmm,
)

import json
import os
import signal
import time as _time_module
from datetime import datetime as _real_datetime, timedelta as _timedelta


# ========== Mock 方法（仅用于测试） ==========


class _CrashingMethod:
    """总是抛异常的 mock 方法（用于测试 crash 淘汰）."""

    method_id = "X_crash"

    def infer(self, env):  # noqa: ANN001
        raise RuntimeError("always crash")


class _AlwaysRejectMethod:
    """总是返回小 p-value 的 mock 方法（反保守, 高功效）."""

    method_id = "X_always_reject"

    def infer(self, env):  # noqa: ANN001
        m = env.source_theta.shape[0]
        return InferenceResult(
            method_id=self.method_id,
            p_values=np.full(m, 0.001),
            elapsed_seconds=0.001,
        )


class _NeverRejectMethod:
    """总是返回大 p-value 的 mock 方法（保守, 零功效）."""

    method_id = "X_never_reject"

    def infer(self, env):  # noqa: ANN001
        m = env.source_theta.shape[0]
        return InferenceResult(
            method_id=self.method_id,
            p_values=np.ones(m),
            elapsed_seconds=0.001,
        )


class _PerfectMethod:
    """完美方法: null 下不拒绝, nonnull 下拒绝 changed 机制.

    注意: 此方法读取 env.changed_mask, 仅用于测试, 不是生产代码.
    生产代码禁止读取 oracle（changed_mask）调参.
    """

    method_id = "X_perfect"

    def infer(self, env):  # noqa: ANN001
        m = env.source_theta.shape[0]
        p = np.full(m, 0.5)  # 默认不拒绝
        if env.changed_mask.any():
            # nonnull: 对 changed 机制返回小 p-value
            p[env.changed_mask] = 0.001
        return InferenceResult(
            method_id=self.method_id,
            p_values=p,
            elapsed_seconds=0.001,
        )


def _make_condition(n=16, structure="independent", noise_type="gaussian", s=0, m=20):
    """构造条件 dict（测试辅助函数）."""
    return {
        "n": n,
        "structure": structure,
        "noise_type": noise_type,
        "s": s,
        "m": m,
    }


def _make_stage_report(evaluations, candidates=None):
    """构造 StageReport（测试辅助函数）."""
    if candidates is None:
        candidates = list({ev.method_id for ev in evaluations})
    return StageReport(
        stage="calibration",
        candidates=candidates,
        eliminated=[],
        survivors=candidates[:],
        evaluations=evaluations,
        config_hash="test",
        timestamp="2026-07-16T00:00:00",
    )


# ========== 1. 配置加载与 hash ==========


class TestLoadProfiles:
    """配置加载测试."""

    def test_load_profiles_returns_config_with_hash(self):
        """加载 profiles 返回带 __config_hash__ 的配置."""
        config = load_profiles()
        assert "__config_hash__" in config
        assert isinstance(config["__config_hash__"], str)
        assert len(config["__config_hash__"]) == 16
        # 验证关键字段存在
        assert "stages" in config
        assert "smoke" in config["stages"]
        assert "pilot" in config["stages"]
        assert "calibration" in config["stages"]
        assert "frozen_null" in config["stages"]
        assert config["candidate_methods"] == ["A0", "A1", "A2", "A3", "A4", "A5"]

    def test_config_hash_deterministic(self):
        """配置 hash 确定性: 两次加载 hash 相同."""
        config1 = load_profiles()
        config2 = load_profiles()
        assert config1["__config_hash__"] == config2["__config_hash__"]

    def test_load_profiles_from_custom_path(self, tmp_path):
        """从自定义路径加载配置."""
        custom_config = {
            "version": "2.0",
            "stages": {"smoke": {"n_environments_per_condition": 10}},
            "candidate_methods": ["A0"],
            "no_method_qualified_label": "NO_METHOD",
        }
        import json
        custom_path = tmp_path / "custom_profiles.json"
        with open(custom_path, "w", encoding="utf-8") as f:
            json.dump(custom_config, f)
        config = load_profiles(custom_path)
        assert config["version"] == "2.0"
        assert config["__config_hash__"]


# ========== 2. StageConfig 不可变 ==========


class TestStageConfig:
    """StageConfig 数据结构测试."""

    def test_stage_config_frozen(self):
        """StageConfig 是 frozen dataclass, 不可变."""
        cfg = StageConfig(
            stage_name="smoke",
            n_environments_per_condition=50,
            pass_criteria={"crash_rate_max": 0.1},
            description="smoke test",
        )
        assert cfg.stage_name == "smoke"
        assert cfg.n_environments_per_condition == 50
        # 尝试修改应抛出 FrozenInstanceError
        with pytest.raises(FrozenInstanceError):
            cfg.stage_name = "pilot"
        with pytest.raises(FrozenInstanceError):
            cfg.n_environments_per_condition = 100


# ========== 3. Smoke 阶段 ==========


class TestSmokeStage:
    """Smoke 阶段测试."""

    def test_smoke_stage_eliminates_crashing_methods(self, tmp_path):
        """Smoke 阶段淘汰 crash_rate > 0.10 的方法."""
        methods = [_CrashingMethod()]
        conditions = [_make_condition(s=0)]
        report = run_stage_smoke(
            methods=methods,
            conditions=conditions,
            n_envs=3,
            seed_base=SMOKE_SEED_BASE,
            checkpoint_dir=tmp_path,
            config_hash="test",
        )
        # crash_rate=1.0 > 0.10, 应被淘汰
        assert "X_crash" in report.eliminated
        assert "X_crash" not in report.survivors
        # 验证 evaluation 记录了 crash 信息
        ev = report.evaluations[0]
        assert ev.crash_rate == 1.0
        assert len(ev.failure_reasons) == 3  # 3 个环境都 crash

    def test_smoke_stage_keeps_normal_method(self, tmp_path):
        """Smoke 阶段保留正常方法."""
        methods = [HC3NormalMethod()]
        conditions = [_make_condition(s=0)]
        report = run_stage_smoke(
            methods=methods,
            conditions=conditions,
            n_envs=3,
            seed_base=SMOKE_SEED_BASE,
            checkpoint_dir=tmp_path,
            config_hash="test",
        )
        # A0 正常运行, crash_rate=0.0, 应存活
        assert "A0" in report.survivors
        assert "A0" not in report.eliminated


# ========== 4. Pilot 阶段 ==========


class TestPilotStage:
    """Pilot 阶段测试."""

    def test_pilot_stage_eliminates_anticonservative_methods(self, tmp_path):
        """Pilot 阶段淘汰 null FDR > 0.20 的方法."""
        methods = [_AlwaysRejectMethod(), _NeverRejectMethod()]
        # null 条件 (s=0): _AlwaysReject 的 null_fdr=1.0 > 0.20, 应被淘汰
        conditions = [_make_condition(s=0)]
        report = run_stage_pilot(
            methods=methods,
            conditions=conditions,
            n_envs=3,
            seed_base=PILOT_SEED_BASE,
            checkpoint_dir=tmp_path,
            config_hash="test",
        )
        # _AlwaysReject: null_fdr=1.0 > 0.20, 淘汰
        assert "X_always_reject" in report.eliminated
        # _NeverReject: null_fdr=0.0, 存活
        assert "X_never_reject" in report.survivors

    def test_pilot_stage_eliminates_zero_power_methods(self, tmp_path):
        """Pilot 阶段淘汰 Recall < 0.10 的方法."""
        methods = [_NeverRejectMethod(), _PerfectMethod()]
        # nonnull 条件 (s=3): _NeverReject 的 Recall=0.0 < 0.10, 应被淘汰
        conditions = [_make_condition(s=3)]
        report = run_stage_pilot(
            methods=methods,
            conditions=conditions,
            n_envs=3,
            seed_base=PILOT_SEED_BASE,
            checkpoint_dir=tmp_path,
            config_hash="test",
        )
        # _NeverReject: Recall=0.0 < 0.10, 淘汰
        assert "X_never_reject" in report.eliminated
        # _Perfect: Recall=1.0, 存活
        assert "X_perfect" in report.survivors


# ========== 5. Calibration 阶段 ==========


class TestCalibrationStage:
    """Calibration 阶段测试."""

    def test_calibration_stage_collects_metrics_without_elimination(self, tmp_path):
        """Calibration 不淘汰, 只收集指标."""
        methods = [_AlwaysRejectMethod(), _NeverRejectMethod()]
        conditions = [
            _make_condition(s=0),
            _make_condition(s=3),
        ]
        report = run_stage_calibration(
            methods=methods,
            conditions=conditions,
            n_envs=3,
            seed_base=CALIBRATION_SEED_BASE,
            checkpoint_dir=tmp_path,
            config_hash="test",
        )
        # 不淘汰任何方法
        assert report.eliminated == []
        # 所有方法都是 survivor
        assert set(report.survivors) == {"X_always_reject", "X_never_reject"}
        # 收集了所有 method × condition 的 evaluation
        assert len(report.evaluations) == 4  # 2 methods × 2 conditions
        # 验证 evaluation 包含指标
        for ev in report.evaluations:
            assert ev.method_id in {"X_always_reject", "X_never_reject"}
            assert ev.stage == "calibration"
            assert ev.n_environments == 3


# ========== 6. 冻结决策 ==========


class TestSelectFrozenMethods:
    """冻结决策测试."""

    def test_select_frozen_methods_returns_main_and_sensitivity(self):
        """冻结返回 1 主方法 + 1 敏感性方法."""
        evaluations = [
            # _PerfectMethod: null 合格, nonnull recall 高
            CandidateEvaluation(
                method_id="X_perfect", stage="calibration",
                condition="n=16_independent_gaussian_s=0",
                null_fdr_bh=0.05,
            ),
            CandidateEvaluation(
                method_id="X_perfect", stage="calibration",
                condition="n=16_independent_gaussian_s=3",
                nonnull_recall=0.9, recall_ci_low=0.85,
            ),
            # _NeverRejectMethod: null 合格, nonnull recall 低
            CandidateEvaluation(
                method_id="X_never_reject", stage="calibration",
                condition="n=16_independent_gaussian_s=0",
                null_fdr_bh=0.0,
            ),
            CandidateEvaluation(
                method_id="X_never_reject", stage="calibration",
                condition="n=16_independent_gaussian_s=3",
                nonnull_recall=0.0, recall_ci_low=0.0,
            ),
        ]
        report = _make_stage_report(evaluations, candidates=["X_perfect", "X_never_reject"])
        config = {"no_method_qualified_label": "NO_METHOD_QUALIFIED"}
        result = select_frozen_methods(report, config)
        # 主方法: recall_ci_low 更高的
        assert result["main"] == "X_perfect"
        # 敏感性方法: 排名第二
        assert result["sensitivity"] == "X_never_reject"
        assert result["all_qualified"] is True
        assert len(result["qualified_methods"]) == 2

    def test_select_frozen_methods_returns_no_method_qualified_when_all_fail(self):
        """全部不合格时返回 NO_METHOD_QUALIFIED."""
        evaluations = [
            # _AlwaysRejectMethod: null 下 null_fdr=1.0 > 0.15, 不合格
            CandidateEvaluation(
                method_id="X_always_reject", stage="calibration",
                condition="n=16_independent_gaussian_s=0",
                null_fdr_bh=1.0,
            ),
            CandidateEvaluation(
                method_id="X_always_reject", stage="calibration",
                condition="n=16_independent_gaussian_s=3",
                nonnull_recall=0.9, recall_ci_low=0.85,
            ),
        ]
        report = _make_stage_report(evaluations, candidates=["X_always_reject"])
        config = {"no_method_qualified_label": "NO_METHOD_QUALIFIED"}
        result = select_frozen_methods(report, config)
        assert result["main"] == "NO_METHOD_QUALIFIED"
        assert result["sensitivity"] == "NO_METHOD_QUALIFIED"
        assert result["all_qualified"] is False
        assert result["qualified_methods"] == []

    def test_select_frozen_methods_prefers_higher_recall_ci_low(self):
        """冻结优先选择 Recall CI low 更高的方法."""
        evaluations = [
            # 方法 A: recall_ci_low=0.8
            CandidateEvaluation(
                method_id="A", stage="calibration",
                condition="n=16_independent_gaussian_s=0",
                null_fdr_bh=0.05,
            ),
            CandidateEvaluation(
                method_id="A", stage="calibration",
                condition="n=16_independent_gaussian_s=3",
                nonnull_recall=0.9, recall_ci_low=0.8,
            ),
            # 方法 B: recall_ci_low=0.6
            CandidateEvaluation(
                method_id="B", stage="calibration",
                condition="n=16_independent_gaussian_s=0",
                null_fdr_bh=0.05,
            ),
            CandidateEvaluation(
                method_id="B", stage="calibration",
                condition="n=16_independent_gaussian_s=3",
                nonnull_recall=0.7, recall_ci_low=0.6,
            ),
        ]
        report = _make_stage_report(evaluations, candidates=["A", "B"])
        config = {"no_method_qualified_label": "NO_METHOD_QUALIFIED"}
        result = select_frozen_methods(report, config)
        # A 的 recall_ci_low=0.8 > B 的 0.6, A 为主方法
        assert result["main"] == "A"
        assert result["sensitivity"] == "B"

    def test_select_frozen_methods_tiebreak_lower_crash_rate(self):
        """Recall CI low 相同时, 选 crash_rate 更低的."""
        evaluations = [
            # 方法 A: recall_ci_low=0.8, crash_rate=0.05
            CandidateEvaluation(
                method_id="A", stage="calibration",
                condition="n=16_independent_gaussian_s=0",
                null_fdr_bh=0.05, crash_rate=0.05,
            ),
            CandidateEvaluation(
                method_id="A", stage="calibration",
                condition="n=16_independent_gaussian_s=3",
                nonnull_recall=0.9, recall_ci_low=0.8, crash_rate=0.05,
            ),
            # 方法 B: recall_ci_low=0.8, crash_rate=0.01
            CandidateEvaluation(
                method_id="B", stage="calibration",
                condition="n=16_independent_gaussian_s=0",
                null_fdr_bh=0.05, crash_rate=0.01,
            ),
            CandidateEvaluation(
                method_id="B", stage="calibration",
                condition="n=16_independent_gaussian_s=3",
                nonnull_recall=0.9, recall_ci_low=0.8, crash_rate=0.01,
            ),
        ]
        report = _make_stage_report(evaluations, candidates=["A", "B"])
        config = {"no_method_qualified_label": "NO_METHOD_QUALIFIED"}
        result = select_frozen_methods(report, config)
        # recall_ci_low 相同, crash_rate 更低的 B 为先
        assert result["main"] == "B"
        assert result["sensitivity"] == "A"

    def test_select_frozen_methods_empty_evaluations(self):
        """空 evaluations 时返回 NO_METHOD_QUALIFIED."""
        report = _make_stage_report([], candidates=[])
        config = {"no_method_qualified_label": "NO_METHOD_QUALIFIED"}
        result = select_frozen_methods(report, config)
        assert result["main"] == "NO_METHOD_QUALIFIED"
        assert result["all_qualified"] is False


# ========== 7. checkpoint 重启恢复 ==========


class TestCheckpointResume:
    """checkpoint 重启恢复测试."""

    def test_checkpoint_resume_skips_completed_chunks(self, tmp_path, monkeypatch):
        """重启跳过已完成 chunk."""
        methods = [HC3NormalMethod()]
        conditions = [_make_condition(s=0)]

        # 第一次运行: 写 checkpoint
        report1 = run_stage_smoke(
            methods=methods,
            conditions=conditions,
            n_envs=2,
            seed_base=SMOKE_SEED_BASE,
            checkpoint_dir=tmp_path,
            config_hash="test",
        )
        assert len(report1.evaluations) == 1

        # 验证 checkpoint 文件存在
        checkpoint_files = list(tmp_path.glob("smoke_A0_*.json"))
        assert len(checkpoint_files) == 1

        # spy: 计数 _evaluate_method_on_condition 调用次数
        import experiments.causal_mechanism_routing.algorithm_selector as mod
        call_count = 0
        original_eval = mod._evaluate_method_on_condition

        def counting_eval(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original_eval(*args, **kwargs)

        monkeypatch.setattr(mod, "_evaluate_method_on_condition", counting_eval)

        # 第二次运行: 应从 checkpoint 加载, 不调用 _evaluate_method_on_condition
        report2 = run_stage_smoke(
            methods=methods,
            conditions=conditions,
            n_envs=2,
            seed_base=SMOKE_SEED_BASE,
            checkpoint_dir=tmp_path,
            config_hash="test",
        )

        # 没有重新运行
        assert call_count == 0
        # 从 checkpoint 加载了 evaluation
        assert len(report2.evaluations) == 1
        # evaluation 数据与第一次一致
        ev1 = report1.evaluations[0]
        ev2 = report2.evaluations[0]
        assert ev1.method_id == ev2.method_id
        assert ev1.condition == ev2.condition
        assert ev1.n_environments == ev2.n_environments


# ========== 8. seed 互斥 ==========


class TestSeedMutualExclusion:
    """seed 互斥测试."""

    def test_seeds_are_mutually_exclusive_across_stages(self):
        """四阶段 seed 范围完全不重叠."""
        n_envs = 100
        smoke_seeds = set(range(SMOKE_SEED_BASE, SMOKE_SEED_BASE + n_envs))
        pilot_seeds = set(range(PILOT_SEED_BASE, PILOT_SEED_BASE + n_envs))
        calibration_seeds = set(
            range(CALIBRATION_SEED_BASE, CALIBRATION_SEED_BASE + n_envs)
        )
        frozen_null_seeds = set(
            range(FROZEN_NULL_SEED_BASE, FROZEN_NULL_SEED_BASE + n_envs)
        )

        # 所有阶段两两互斥
        assert smoke_seeds.isdisjoint(pilot_seeds)
        assert smoke_seeds.isdisjoint(calibration_seeds)
        assert smoke_seeds.isdisjoint(frozen_null_seeds)
        assert pilot_seeds.isdisjoint(calibration_seeds)
        assert pilot_seeds.isdisjoint(frozen_null_seeds)
        assert calibration_seeds.isdisjoint(frozen_null_seeds)

        # 验证 seed 范围间隔合理（每阶段预留 100k 空间）
        assert PILOT_SEED_BASE - SMOKE_SEED_BASE >= n_envs
        assert CALIBRATION_SEED_BASE - PILOT_SEED_BASE >= n_envs
        assert FROZEN_NULL_SEED_BASE - CALIBRATION_SEED_BASE >= n_envs


# ========== 9. CandidateEvaluation 指标记录 ==========


class TestCandidateEvaluation:
    """CandidateEvaluation 数据结构测试."""

    def test_candidate_evaluation_records_all_metrics(self):
        """CandidateEvaluation 记录所有指标."""
        ev = CandidateEvaluation(
            method_id="A0",
            stage="calibration",
            condition="n=16_independent_gaussian_s=0",
            null_fdr_bh=0.05,
            null_fdr_by=0.03,
            nonnull_recall=0.8,
            recall_ci_low=0.75,
            mean_runtime_seconds=0.01,
            crash_rate=0.0,
            p_value_in_range_rate=1.0,
            n_environments=100,
            p_value_stats={"mean": 0.5, "q50": 0.5},
            elapsed_seconds=1.0,
            failure_reasons=[],
            bootstrap_count=None,
        )
        # 验证所有字段
        assert ev.method_id == "A0"
        assert ev.stage == "calibration"
        assert ev.condition == "n=16_independent_gaussian_s=0"
        assert ev.null_fdr_bh == 0.05
        assert ev.null_fdr_by == 0.03
        assert ev.nonnull_recall == 0.8
        assert ev.recall_ci_low == 0.75
        assert ev.mean_runtime_seconds == 0.01
        assert ev.crash_rate == 0.0
        assert ev.p_value_in_range_rate == 1.0
        assert ev.n_environments == 100
        assert ev.p_value_stats["mean"] == 0.5
        assert ev.elapsed_seconds == 1.0
        assert ev.failure_reasons == []
        assert ev.bootstrap_count is None

    def test_candidate_evaluation_default_values(self):
        """CandidateEvaluation 默认值正确."""
        ev = CandidateEvaluation(
            method_id="A1",
            stage="smoke",
            condition="n=8_dag_t5_s=0",
        )
        assert ev.null_fdr_bh == 0.0
        assert ev.null_fdr_by == 0.0
        assert ev.nonnull_recall == 0.0
        assert ev.recall_ci_low == 0.0
        assert ev.mean_runtime_seconds == 0.0
        assert ev.crash_rate == 0.0
        assert ev.p_value_in_range_rate == 1.0
        assert ev.n_environments == 0
        assert ev.p_value_stats == {}
        assert ev.elapsed_seconds == 0.0
        assert ev.failure_reasons == []
        assert ev.bootstrap_count is None


# ========== 10. p-value 尾部分位数 ==========


class TestPValueTailStats:
    """p-value 尾部分位数计算测试."""

    def test_p_value_tail_stats_correct(self):
        """尾部分位数计算正确."""
        p = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
        stats = compute_p_value_tail_stats(p)
        assert stats["min"] == 0.1
        assert stats["max"] == 1.0
        assert stats["mean"] == pytest.approx(0.55)
        assert stats["q50"] == pytest.approx(0.55)  # median
        assert stats["q25"] == pytest.approx(0.325)
        assert stats["q75"] == pytest.approx(0.775)

    def test_p_value_tail_stats_empty(self):
        """空数组返回空 dict."""
        assert compute_p_value_tail_stats(np.array([])) == {}

    def test_p_value_tail_stats_single_value(self):
        """单值数组."""
        stats = compute_p_value_tail_stats(np.array([0.5]))
        assert stats["min"] == 0.5
        assert stats["max"] == 0.5
        assert stats["mean"] == 0.5
        assert stats["q50"] == 0.5


# ========== 11. 淘汰规则谓词 ==========


class TestEliminationPredicates:
    """淘汰规则谓词测试."""

    def test_fails_smoke_criteria_crash_rate(self):
        """smoke: crash_rate > 0.10 不合格."""
        ev = CandidateEvaluation(
            method_id="A0", stage="smoke",
            condition="n=16_independent_gaussian_s=0",
            crash_rate=0.15,
        )
        assert fails_smoke_criteria(ev, {"crash_rate_max": 0.10}) is True

    def test_fails_smoke_criteria_in_range_rate(self):
        """smoke: p_value_in_range_rate < 0.95 不合格."""
        ev = CandidateEvaluation(
            method_id="A0", stage="smoke",
            condition="n=16_independent_gaussian_s=0",
            p_value_in_range_rate=0.90,
        )
        assert fails_smoke_criteria(ev, {"p_value_in_range_rate_min": 0.95}) is True

    def test_fails_smoke_criteria_runtime(self):
        """smoke: mean_runtime_seconds > 5.0 不合格."""
        ev = CandidateEvaluation(
            method_id="A0", stage="smoke",
            condition="n=16_independent_gaussian_s=0",
            mean_runtime_seconds=6.0,
        )
        assert fails_smoke_criteria(ev, {"mean_runtime_seconds_max": 5.0}) is True

    def test_fails_smoke_criteria_passes(self):
        """smoke: 所有指标合格."""
        ev = CandidateEvaluation(
            method_id="A0", stage="smoke",
            condition="n=16_independent_gaussian_s=0",
            crash_rate=0.0,
            p_value_in_range_rate=1.0,
            mean_runtime_seconds=0.01,
        )
        assert fails_smoke_criteria(ev, {
            "crash_rate_max": 0.10,
            "p_value_in_range_rate_min": 0.95,
            "mean_runtime_seconds_max": 5.0,
        }) is False

    def test_fails_pilot_criteria_null_fdr(self):
        """pilot: null 条件 null_fdr > 0.20 不合格."""
        ev = CandidateEvaluation(
            method_id="A0", stage="pilot",
            condition="n=16_independent_gaussian_s=0",
            null_fdr_bh=0.25,
        )
        assert fails_pilot_criteria(ev, {"null_fdr_max": 0.20}) is True

    def test_fails_pilot_criteria_recall(self):
        """pilot: nonnull 条件 recall < 0.10 不合格."""
        ev = CandidateEvaluation(
            method_id="A0", stage="pilot",
            condition="n=16_independent_gaussian_s=3",
            nonnull_recall=0.05,
        )
        assert fails_pilot_criteria(ev, {"nonnull_recall_min": 0.10}) is True

    def test_fails_pilot_criteria_crash_rate(self):
        """pilot: crash_rate > 0.05 不合格."""
        ev = CandidateEvaluation(
            method_id="A0", stage="pilot",
            condition="n=16_independent_gaussian_s=0",
            crash_rate=0.10,
        )
        assert fails_pilot_criteria(ev, {"crash_rate_max": 0.05}) is True


# ========== Task 6: 自动实验队列调度器测试 ==========
#
# 以下 20 个测试覆盖 ExperimentScheduler 的:
# 1. 单实例锁  2. checkpoint 恢复  3. dry-run  4. 时间预算
# 5. STOP 文件  6. Ctrl+C 信号  7. 运行窗口  8. 电池检测
# 9. 低内存暂停  10. 高 CPU 暂停  11. 每日预算重置  12. 日志滚动
# 13. 暂停状态记录  14. null 门禁  15. dirty worktree  16. clean worktree
# 17. provenance 预检  18. 状态机转移  19. 资源限制默认值  20. 产物目录校验
#
# 约束: 不真等 5 分钟, 用 monkeypatch 模拟时间/psutil; 不写入 data/, 用 tmp_path.


# ========== Mock 辅助函数 ==========


_SCHED_MOD = "experiments.causal_mechanism_routing.experiment_scheduler"


def _patch_scheduler_datetime(monkeypatch, hour: int, minute: int) -> None:
    """Mock scheduler 模块的 datetime.now() 返回固定时间.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        hour: 固定小时.
        minute: 固定分钟.
    """
    fixed = _real_datetime(2026, 7, 16, hour, minute)

    class _FakeDateTime:
        """模拟 datetime 类, now() 返回固定时间."""

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return fixed

    monkeypatch.setattr(_SCHED_MOD + ".datetime", _FakeDateTime)


def _mock_battery(monkeypatch, power_plugged: bool) -> None:
    """Mock psutil.sensors_battery.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        power_plugged: True 表示接通电源.
    """
    class _FakeBattery:
        """模拟电池状态."""

        def __init__(self, plugged: bool) -> None:
            self.power_plugged = plugged

    monkeypatch.setattr(
        _SCHED_MOD + ".psutil.sensors_battery",
        lambda: _FakeBattery(power_plugged),
    )


def _mock_memory(monkeypatch, available_gb: float) -> None:
    """Mock psutil.virtual_memory.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        available_gb: 可用内存 (GB).
    """
    class _FakeMem:
        """模拟内存状态."""

        def __init__(self, avail_gb: float) -> None:
            self.available = int(avail_gb * 1024 ** 3)

    monkeypatch.setattr(
        _SCHED_MOD + ".psutil.virtual_memory",
        lambda: _FakeMem(available_gb),
    )


def _mock_cpu(monkeypatch, cpu_percent: float) -> None:
    """Mock psutil.cpu_percent.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        cpu_percent: CPU 占用百分比 (0-100).
    """
    monkeypatch.setattr(
        _SCHED_MOD + ".psutil.cpu_percent",
        lambda interval=None: cpu_percent,
    )


def _mock_idle(monkeypatch, idle_minutes: float) -> None:
    """Mock _get_idle_seconds.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        idle_minutes: 系统空闲分钟数.
    """
    monkeypatch.setattr(
        _SCHED_MOD + "._get_idle_seconds",
        lambda: idle_minutes * 60.0,
    )


def _mock_all_clear(monkeypatch, scheduler) -> None:
    """Mock 所有资源检查为正常 (不触发暂停).

    Args:
        monkeypatch: pytest monkeypatch fixture.
        scheduler: 调度器实例.
    """
    _mock_idle(monkeypatch, idle_minutes=30.0)
    _mock_battery(monkeypatch, power_plugged=True)
    _mock_memory(monkeypatch, available_gb=16.0)
    _mock_cpu(monkeypatch, cpu_percent=10.0)
    monkeypatch.setattr(scheduler, "_get_daily_elapsed", lambda: 0.0)
    monkeypatch.setattr(scheduler, "_is_in_run_window", lambda: True)


# ========== 1. 单实例锁 ==========


class TestSchedulerSingleInstanceLock:
    """单实例锁测试."""

    def test_single_instance_lock_prevents_concurrent_runners(self, tmp_path):
        """单实例锁: 第二个调度器无法获取锁."""
        output_dir = tmp_path / "p2_lock_test"
        scheduler1 = ExperimentScheduler(output_dir=output_dir, profile="smoke")
        assert scheduler1.acquire_lock() is True

        # 第二个调度器使用同一目录, 应无法获取锁
        scheduler2 = ExperimentScheduler(output_dir=output_dir, profile="smoke")
        assert scheduler2.acquire_lock() is False

        # 释放后第二个可以获取
        scheduler1.release_lock()
        assert scheduler2.acquire_lock() is True
        scheduler2.release_lock()


# ========== 2. checkpoint 恢复 ==========


class TestSchedulerCheckpointResume:
    """checkpoint 恢复测试."""

    def test_checkpoint_resume_skips_completed_chunks(self, tmp_path):
        """从 checkpoint 恢复时跳过已完成阶段."""
        output_dir = tmp_path / "p2_resume_test"
        scheduler = ExperimentScheduler(output_dir=output_dir, profile="pilot")

        # 手动写入 smoke 已完成的 checkpoint
        cp = ChunkCheckpoint(stage="smoke", status="completed")
        scheduler._save_checkpoint(cp)

        # 恢复后应跳过 smoke, 从 pilot 开始
        loaded = scheduler._load_latest_checkpoint()
        initial_state = scheduler._resolve_initial_state(loaded)
        assert initial_state == SchedulerState.PILOT

        # 无 checkpoint 时从 QUEUED 开始
        scheduler2 = ExperimentScheduler(
            output_dir=tmp_path / "p2_resume_test2", profile="pilot",
        )
        assert scheduler2._resolve_initial_state(None) == SchedulerState.QUEUED

        # running 状态从当前阶段恢复
        cp_running = ChunkCheckpoint(stage="pilot", status="running")
        scheduler2._save_checkpoint(cp_running)
        loaded2 = scheduler2._load_latest_checkpoint()
        assert scheduler2._resolve_initial_state(loaded2) == SchedulerState.PILOT

        # 终止态直接返回
        cp_blocked = ChunkCheckpoint(stage="blocked_null_gate", status="completed")
        scheduler2._save_checkpoint(cp_blocked)
        loaded3 = scheduler2._load_latest_checkpoint()
        assert scheduler2._resolve_initial_state(loaded3) == SchedulerState.BLOCKED_NULL_GATE


# ========== 3. dry-run ==========


class TestSchedulerDryRun:
    """dry-run 测试."""

    def test_dry_run_outputs_execution_graph_without_computing(self, tmp_path, monkeypatch):
        """dry-run 只输出执行图, 不执行计算."""
        call_count = 0

        def fake_runner(cp, ctx):  # noqa: ANN001
            nonlocal call_count
            call_count += 1
            return StageResult(success=True)

        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_dry_test",
            stage_runners={"smoke": fake_runner},
            profile="smoke",
        )
        status = scheduler.run(dry_run=True)

        assert status.state == SchedulerState.QUEUED
        assert "dry-run" in status.next_action.lower() or "remove" in status.next_action.lower()
        assert call_count == 0  # 没有调用 runner


# ========== 4. 时间预算 ==========


class TestSchedulerBudget:
    """时间预算测试."""

    def test_max_minutes_stops_after_budget(self, tmp_path, monkeypatch):
        """max_minutes 超时后暂停为 PAUSED_BUDGET."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_budget_test",
            stage_runners={"smoke": lambda cp, ctx: StageResult(success=True)},
            profile="pilot",  # smoke + pilot
        )
        monkeypatch.setattr(scheduler, "_should_pause", lambda: (False, None, None))

        # max_minutes=0: smoke 执行后预算检查立即触发
        status = scheduler.run(max_minutes=0.0)
        assert status.state == SchedulerState.PAUSED_BUDGET
        assert "budget" in status.paused_reason.lower()


# ========== 5. STOP 文件 ==========


class TestSchedulerStopFile:
    """STOP 文件安全退出测试."""

    def test_stop_file_triggers_safe_exit(self, tmp_path, monkeypatch):
        """STOP 文件存在时安全退出."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_stop_test",
            profile="smoke",
        )
        monkeypatch.setattr(scheduler, "_should_pause", lambda: (False, None, None))

        # 创建 STOP 文件
        scheduler.stop_file.touch()
        assert scheduler.stop_file.exists()

        status = scheduler.run()
        assert "STOP" in status.paused_reason or "STOP file" in status.paused_reason
        # STOP 文件应被删除 (便于下次启动)
        assert not scheduler.stop_file.exists()


# ========== 6. Ctrl+C 信号 ==========


class TestSchedulerSignal:
    """Ctrl+C 信号安全退出测试."""

    def test_ctrl_c_safe_exit(self, tmp_path, monkeypatch):
        """Ctrl+C (SIGINT) 触发安全退出."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_signal_test",
            profile="pilot",
        )
        monkeypatch.setattr(scheduler, "_should_pause", lambda: (False, None, None))

        # 验证信号处理器设置停止标志
        scheduler._handle_signal(signal.SIGINT, None)
        assert scheduler._stop_requested is True

        # 使用 runner 在执行中触发信号
        call_count = 0

        def signaling_runner(cp, ctx):  # noqa: ANN001
            nonlocal call_count
            call_count += 1
            # 在首次调用时触发信号
            if call_count == 1:
                scheduler._handle_signal(signal.SIGINT, None)
            return StageResult(success=True)

        scheduler.stage_runners["smoke"] = signaling_runner
        # 重置停止标志 (run() 会重置)
        status = scheduler.run()
        assert call_count >= 1  # smoke 至少执行了一次
        assert "stop requested" in status.paused_reason.lower() or "signal" in status.paused_reason.lower()


# ========== 7. 运行窗口 ==========


class TestSchedulerRunWindow:
    """运行窗口检查测试."""

    def test_run_window_check(self, tmp_path, monkeypatch):
        """运行窗口内/外判断正确, 含跨午夜窗口."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_window_test",
            resource_limits=ResourceLimits(
                run_window_start="00:30", run_window_end="06:30",
            ),
            profile="smoke",
        )

        # 窗口内 (03:00)
        _patch_scheduler_datetime(monkeypatch, 3, 0)
        assert scheduler._is_in_run_window() is True

        # 窗口外 (12:00)
        _patch_scheduler_datetime(monkeypatch, 12, 0)
        assert scheduler._is_in_run_window() is False

        # 边界: 起始时间
        _patch_scheduler_datetime(monkeypatch, 0, 30)
        assert scheduler._is_in_run_window() is True

        # 边界: 结束时间
        _patch_scheduler_datetime(monkeypatch, 6, 30)
        assert scheduler._is_in_run_window() is True

        # 跨午夜窗口: 23:00-06:00
        scheduler2 = ExperimentScheduler(
            output_dir=tmp_path / "p2_window_test2",
            resource_limits=ResourceLimits(
                run_window_start="23:00", run_window_end="06:00",
            ),
            profile="smoke",
        )
        _patch_scheduler_datetime(monkeypatch, 2, 0)
        assert scheduler2._is_in_run_window() is True  # 02:00 在 23:00-06:00 内

        _patch_scheduler_datetime(monkeypatch, 12, 0)
        assert scheduler2._is_in_run_window() is False  # 12:00 不在 23:00-06:00 内

        _patch_scheduler_datetime(monkeypatch, 23, 30)
        assert scheduler2._is_in_run_window() is True  # 23:30 在 23:00-06:00 内


# ========== 8. 电池检测 ==========


class TestSchedulerBattery:
    """电池供电检测测试."""

    def test_battery_detection(self, tmp_path, monkeypatch):
        """电池供电时暂停为 PAUSED_BATTERY."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_battery_test",
            profile="smoke",
        )

        # 模拟电池供电
        _mock_idle(monkeypatch, idle_minutes=30.0)
        _mock_battery(monkeypatch, power_plugged=False)

        should_pause, reason, pause_state = scheduler._should_pause()
        assert should_pause is True
        assert pause_state == SchedulerState.PAUSED_BATTERY
        assert "battery" in reason.lower()

        # 模拟接通电源: 不应因电池暂停
        _mock_battery(monkeypatch, power_plugged=True)
        _mock_memory(monkeypatch, available_gb=16.0)
        _mock_cpu(monkeypatch, cpu_percent=10.0)
        monkeypatch.setattr(scheduler, "_get_daily_elapsed", lambda: 0.0)
        monkeypatch.setattr(scheduler, "_is_in_run_window", lambda: True)
        should_pause2, _, pause_state2 = scheduler._should_pause()
        assert should_pause2 is False
        assert pause_state2 is None


# ========== 9. 低内存暂停 ==========


class TestSchedulerLowMemoryPause:
    """低内存暂停测试."""

    def test_low_memory_pause(self, tmp_path, monkeypatch):
        """可用内存低于阈值时暂停为 PAUSED_RESOURCE."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_mem_test",
            profile="smoke",
        )

        _mock_idle(monkeypatch, idle_minutes=30.0)
        _mock_battery(monkeypatch, power_plugged=True)
        _mock_memory(monkeypatch, available_gb=2.0)  # 2 GB < 4 GB 阈值

        should_pause, reason, pause_state = scheduler._should_pause()
        assert should_pause is True
        assert pause_state == SchedulerState.PAUSED_RESOURCE
        assert "memory" in reason.lower()


# ========== 10. 高 CPU 暂停 ==========


class TestSchedulerHighCpuPause:
    """高 CPU 暂停测试."""

    def test_high_cpu_pause(self, tmp_path, monkeypatch):
        """CPU 持续高于阈值时暂停为 PAUSED_RESOURCE."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_cpu_test",
            profile="smoke",
        )

        _mock_idle(monkeypatch, idle_minutes=30.0)
        _mock_battery(monkeypatch, power_plugged=True)
        _mock_memory(monkeypatch, available_gb=16.0)
        _mock_cpu(monkeypatch, cpu_percent=80.0)  # 80% > 60% 阈值

        should_pause, reason, pause_state = scheduler._should_pause()
        assert should_pause is True
        assert pause_state == SchedulerState.PAUSED_RESOURCE
        assert "cpu" in reason.lower()


# ========== 11. 每日预算重置 ==========


class TestSchedulerDailyBudget:
    """每日时间预算重置测试."""

    def test_daily_budget_reset(self, tmp_path):
        """每日时间预算在日期变更时自动重置为 0."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_budget_reset",
            profile="smoke",
        )
        budget_path = scheduler.output_dir / "time_budget.json"

        # 写入昨天的预算 (3600 秒 = 60 分钟)
        yesterday = (_real_datetime.now() - _timedelta(days=1)).strftime("%Y-%m-%d")
        budget_path.write_text(
            json.dumps({"date": yesterday, "elapsed_seconds": 3600}),
            encoding="utf-8",
        )
        assert scheduler._get_daily_elapsed() == 0.0  # 日期变更, 重置

        # 写入今天的预算 (1800 秒 = 30 分钟)
        today = _real_datetime.now().strftime("%Y-%m-%d")
        budget_path.write_text(
            json.dumps({"date": today, "elapsed_seconds": 1800}),
            encoding="utf-8",
        )
        assert scheduler._get_daily_elapsed() == 30.0

        # 验证 _update_daily_elapsed 累加
        scheduler._update_daily_elapsed(60.0)  # 新增 60 秒 = 1 分钟
        assert scheduler._get_daily_elapsed() == 31.0  # 30 + 1


# ========== 12. 日志滚动 ==========


class TestSchedulerLogRotation:
    """日志滚动保留测试."""

    def test_log_rotation_retention(self, tmp_path):
        """超过保留期的旧日志被删除, 新日志保留."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_log_test",
            resource_limits=ResourceLimits(log_retention_days=14),
            profile="smoke",
        )

        # 创建旧日志 (30 天前)
        old_log = scheduler.log_dir / "scheduler_20260615.log"
        old_log.write_text("old log", encoding="utf-8")
        old_time = _time_module.time() - 30 * 86400
        os.utime(old_log, (old_time, old_time))

        # 创建新日志 (今天)
        new_log = scheduler.log_dir / "scheduler_20260716.log"
        new_log.write_text("new log", encoding="utf-8")

        # 清理旧日志
        scheduler._cleanup_old_logs()

        assert not old_log.exists()  # 旧日志被删除
        assert new_log.exists()  # 新日志保留


# ========== 13. 暂停状态记录 ==========


class TestSchedulerPausedStates:
    """暂停状态记录原因和下一步动作测试."""

    def test_paused_states_record_reason_and_next_action(self, tmp_path, monkeypatch):
        """暂停状态记录 paused_reason 和 next_action."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_paused_test",
            profile="smoke",
        )

        # Mock _should_pause 返回资源暂停
        monkeypatch.setattr(
            scheduler, "_should_pause",
            lambda: (True, "low memory (2.0 < 4.0 GB)", SchedulerState.PAUSED_RESOURCE),
        )

        status = scheduler.run()
        assert status.state == SchedulerState.PAUSED_RESOURCE
        assert status.paused_reason is not None
        assert "low memory" in status.paused_reason
        assert status.next_action  # 非空
        assert "resource" in status.next_action.lower() or "wait" in status.next_action.lower()


# ========== 14. null 门禁 ==========


class TestSchedulerNullGate:
    """null 门禁失败阻断 confirmatory 测试."""

    def test_null_gate_failure_blocks_confirmatory(self, tmp_path, monkeypatch):
        """null 门禁失败时进入 BLOCKED_NULL_GATE.

        前置阶段 (smoke/pilot/calibration/frozen) 需注册 passthrough runner,
        否则 fail-closed 会在 smoke 阶段抛异常.
        """
        def null_fail_runner(cp, ctx):  # noqa: ANN001
            return StageResult(success=True, data={"null_gate_passed": False})

        passthrough = lambda cp, ctx: StageResult(success=True)  # noqa: E731

        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_null_gate_test",
            stage_runners={
                "smoke": passthrough,
                "pilot": passthrough,
                "calibration": passthrough,
                "frozen": passthrough,
                "null_formal": null_fail_runner,
            },
            profile="background",
        )
        monkeypatch.setattr(scheduler, "_should_pause", lambda: (False, None, None))

        status = scheduler.run()
        assert status.state == SchedulerState.BLOCKED_NULL_GATE
        assert status.next_action  # 非空, 提示修复


# ========== 15-17. provenance 门禁 ==========


class TestSchedulerProvenanceGate:
    """provenance 预检与 worktree 清洁度门禁测试."""

    def test_dirty_worktree_blocks_confirmatory(self, tmp_path, monkeypatch):
        """dirty worktree 阻断 confirmatory, 进入 BLOCKED_PROVENANCE.

        前置阶段需注册 passthrough runner (fail-closed).
        """
        def null_pass_runner(cp, ctx):  # noqa: ANN001
            return StageResult(success=True, data={"null_gate_passed": True})

        def dirty_worktree_runner(cp, ctx):  # noqa: ANN001
            return StageResult(success=True, data={"worktree_clean": False})

        passthrough = lambda cp, ctx: StageResult(success=True)  # noqa: E731

        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_dirty_test",
            stage_runners={
                "smoke": passthrough,
                "pilot": passthrough,
                "calibration": passthrough,
                "frozen": passthrough,
                "null_formal": null_pass_runner,
                "confirmatory_precheck": dirty_worktree_runner,
            },
            profile="background",
        )
        monkeypatch.setattr(scheduler, "_should_pause", lambda: (False, None, None))

        status = scheduler.run()
        assert status.state == SchedulerState.BLOCKED_PROVENANCE
        assert "dirty" in status.next_action.lower() or "commit" in status.next_action.lower()

    def test_clean_worktree_allows_confirmatory(self, tmp_path, monkeypatch):
        """clean worktree 允许 confirmatory, 但后续 P2A 阶段需真实 runner.

        confirmatory 路径未完整实现时, P2A 缺 runner → fail-closed → RETRYING.
        不断言 COMPLETE, 因为 confirmatory 路径需要真实 runner (P1-5 反馈).
        """
        def null_pass_runner(cp, ctx):  # noqa: ANN001
            return StageResult(success=True, data={"null_gate_passed": True})

        def clean_worktree_runner(cp, ctx):  # noqa: ANN001
            return StageResult(success=True, data={"worktree_clean": True})

        passthrough = lambda cp, ctx: StageResult(success=True)  # noqa: E731

        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_clean_test",
            stage_runners={
                "smoke": passthrough,
                "pilot": passthrough,
                "calibration": passthrough,
                "frozen": passthrough,
                "null_formal": null_pass_runner,
                "confirmatory_precheck": clean_worktree_runner,
                # P2A 及后续阶段不注册 runner → fail-closed → RETRYING
            },
            profile="background",
        )
        monkeypatch.setattr(scheduler, "_should_pause", lambda: (False, None, None))

        status = scheduler.run()
        # P2A 缺 runner → fail-closed 抛异常 → RETRYING (非 COMPLETE)
        assert status.state == SchedulerState.RETRYING
        assert "p2a" in status.next_action.lower() or "runner" in status.next_action.lower()

    def test_provenance_precheck(self, tmp_path, monkeypatch):
        """provenance.assert_clean_worktree: clean 返回 False, dirty 抛 RuntimeError."""
        from experiments.causal_mechanism_routing import provenance as prov_mod

        # Mock clean git status
        class _CleanResult:
            returncode = 0
            stdout = ""

        monkeypatch.setattr(prov_mod.subprocess, "run", lambda *a, **kw: _CleanResult())
        dirty = prov_mod.assert_clean_worktree(confirmatory=True)
        assert dirty is False

        # Mock dirty git status
        class _DirtyResult:
            returncode = 0
            stdout = " M some_file.py"

        monkeypatch.setattr(prov_mod.subprocess, "run", lambda *a, **kw: _DirtyResult())
        with pytest.raises(RuntimeError, match="dirty"):
            prov_mod.assert_clean_worktree(confirmatory=True)

        # exploratory 模式不抛异常, 仅返回 True
        dirty_explore = prov_mod.assert_clean_worktree(confirmatory=False)
        assert dirty_explore is True


# ========== 18. 状态机转移 ==========


class TestSchedulerStateMachine:
    """状态机转移规则测试."""

    def test_state_machine_transitions(self, tmp_path):
        """状态机正向转移、决策态门禁、失败态."""
        scheduler = ExperimentScheduler(
            output_dir=tmp_path / "p2_state_test",
            profile="background",
        )

        # 普通正向转移
        assert scheduler._get_next_state(
            SchedulerState.SMOKE, StageResult(success=True),
        ) == SchedulerState.PILOT
        assert scheduler._get_next_state(
            SchedulerState.PILOT, StageResult(success=True),
        ) == SchedulerState.CALIBRATION
        assert scheduler._get_next_state(
            SchedulerState.CALIBRATION, StageResult(success=True),
        ) == SchedulerState.FROZEN
        assert scheduler._get_next_state(
            SchedulerState.FROZEN, StageResult(success=True),
        ) == SchedulerState.NULL_FORMAL

        # null 门禁: 通过
        assert scheduler._get_next_state(
            SchedulerState.NULL_FORMAL,
            StageResult(success=True, data={"null_gate_passed": True}),
        ) == SchedulerState.CONFIRMATORY_PRECHECK
        # null 门禁: 失败
        assert scheduler._get_next_state(
            SchedulerState.NULL_FORMAL,
            StageResult(success=True, data={"null_gate_passed": False}),
        ) == SchedulerState.BLOCKED_NULL_GATE

        # provenance 门禁: 通过
        assert scheduler._get_next_state(
            SchedulerState.CONFIRMATORY_PRECHECK,
            StageResult(success=True, data={"worktree_clean": True}),
        ) == SchedulerState.P2A
        # provenance 门禁: 失败
        assert scheduler._get_next_state(
            SchedulerState.CONFIRMATORY_PRECHECK,
            StageResult(success=True, data={"worktree_clean": False}),
        ) == SchedulerState.BLOCKED_PROVENANCE

        # confirmatory 后续链
        assert scheduler._get_next_state(
            SchedulerState.P2A, StageResult(success=True),
        ) == SchedulerState.P2B
        assert scheduler._get_next_state(
            SchedulerState.P2B, StageResult(success=True),
        ) == SchedulerState.UNKNOWN_NOISE
        assert scheduler._get_next_state(
            SchedulerState.UNKNOWN_NOISE, StageResult(success=True),
        ) == SchedulerState.REPRODUCTION
        assert scheduler._get_next_state(
            SchedulerState.REPRODUCTION, StageResult(success=True),
        ) == SchedulerState.COMPLETE

        # 失败态 → RETRYING
        assert scheduler._get_next_state(
            SchedulerState.SMOKE, StageResult(success=False),
        ) == SchedulerState.RETRYING

        # smoke profile: smoke 完成后下一阶段 pilot 不在 profile 中 → COMPLETE
        scheduler_smoke = ExperimentScheduler(
            output_dir=tmp_path / "p2_state_smoke", profile="smoke",
        )
        assert scheduler_smoke._get_next_state(
            SchedulerState.SMOKE, StageResult(success=True),
        ) == SchedulerState.COMPLETE


# ========== 19. 资源限制默认值 ==========


class TestSchedulerResourceLimits:
    """资源限制默认值测试."""

    def test_resource_limits_default_values(self):
        """ResourceLimits 默认值符合规范."""
        limits = ResourceLimits()
        assert limits.max_workers == 1
        assert limits.max_compute_threads == 2
        assert limits.chunk_max_minutes == 15.0
        assert limits.daily_max_minutes == 120.0
        assert limits.idle_threshold_minutes == 10
        assert limits.run_window_start == "00:30"
        assert limits.run_window_end == "06:30"
        assert limits.cpu_high_threshold == 0.60
        assert limits.memory_low_threshold_gb == 4.0
        assert limits.log_retention_days == 14

    def test_compute_max_threads_within_bounds(self):
        """_compute_max_threads 返回值在 [1, 2] 范围内."""
        threads = _compute_max_threads()
        assert 1 <= threads <= 2

    def test_parse_hhmm_valid(self):
        """_parse_hhmm 正确解析 HH:MM."""
        from datetime import time as dtime
        assert _parse_hhmm("00:30") == dtime(0, 30)
        assert _parse_hhmm("06:30") == dtime(6, 30)
        assert _parse_hhmm("23:59") == dtime(23, 59)

    def test_parse_hhmm_invalid(self):
        """_parse_hhmm 格式错误时抛 ValueError."""
        with pytest.raises(ValueError):
            _parse_hhmm("invalid")
        with pytest.raises(ValueError):
            _parse_hhmm("25:00")  # hour 越界


# ========== 20. 产物目录校验 ==========


class TestSchedulerOutputDir:
    """产物目录不得位于生产路径测试."""

    def test_output_dir_not_in_production(self, tmp_path):
        """产物目录位于生产路径时抛 ValueError."""
        for prod in PRODUCTION_PATHS:
            bad_dir = tmp_path / prod / "p2_test"
            with pytest.raises(ValueError, match="生产路径"):
                ExperimentScheduler(output_dir=bad_dir, profile="smoke")

        # data/experiments/p2_automation 不在生产路径中
        good_dir = tmp_path / "data" / "experiments" / "p2_automation"
        scheduler = ExperimentScheduler(output_dir=good_dir, profile="smoke")
        assert scheduler.output_dir.exists()

        # 验证 PROFILE_STAGES 包含所有 profile
        assert "smoke" in PROFILE_STAGES
        assert "pilot" in PROFILE_STAGES
        assert "calibration" in PROFILE_STAGES
        assert "frozen_null" in PROFILE_STAGES
        assert "background" in PROFILE_STAGES

        # background profile 包含所有阶段
        bg_stages = PROFILE_STAGES["background"]
        assert "smoke" in bg_stages
        assert "pilot" in bg_stages
        assert "calibration" in bg_stages
        assert "frozen" in bg_stages
        assert "null_formal" in bg_stages
        assert "confirmatory_precheck" in bg_stages
        assert "p2a" in bg_stages
        assert "p2b" in bg_stages
        assert "unknown_noise" in bg_stages
        assert "reproduction" in bg_stages

        # 验证未知 profile 抛 ValueError
        with pytest.raises(ValueError, match="unknown profile"):
            ExperimentScheduler(output_dir=tmp_path / "safe_dir", profile="unknown")
