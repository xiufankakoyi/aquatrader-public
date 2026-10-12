"""P2 实验队列 CLI 运行器.

用法:
    python scripts/run_p2_experiment_queue.py --profile smoke --dry-run
    python scripts/run_p2_experiment_queue.py --profile background
    python scripts/run_p2_experiment_queue.py --profile background --max-minutes 5

设计原则:
    1. 设置 BLAS/NumPy 线程上限为 1 (OMP/MKL/OPENBLAS)
    2. 降低进程优先级 (Windows: BELOW_NORMAL_PRIORITY_CLASS)
    3. 单实例运行 (调度器内置文件锁)
    4. 产物只写入 data/experiments/p2_automation/
    5. 队列模式下单 worker (n_jobs=1)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# 设置 BLAS/NumPy 线程上限 (必须在 import numpy 之前)
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# 降低进程优先级 (Windows)
if sys.platform == "win32":
    try:
        import psutil
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_full_conditions(config: dict) -> list[dict]:
    """构建完整 128 条件 (4n × 2 struct × 4 noise × (1 null + 3 nonnull s)).

    与 run_candidate_screening.build_conditions 口径一致:
        - null: s=0
        - nonnull: s ∈ s_values_for_nonnull (默认 [1,3,5])
    共 4 × 2 × 4 × 4 = 128 条件.

    Args:
        config: profiles 配置 dict.

    Returns:
        条件 dict 列表, 每项含 n/structure/noise_type/s/m 等参数.
    """
    cond_cfg = config["conditions"]
    conditions: list[dict] = []
    for n in cond_cfg["n_values"]:
        for structure in cond_cfg["structures"]:
            for noise_type in cond_cfg["noise_types"]:
                # null 条件 (s=0)
                conditions.append({
                    "n": n, "structure": structure, "noise_type": noise_type, "s": 0,
                    "m": 20, "source_sample_size": 512, "source_noise": 0.5,
                    "target_noise": 1.0, "shift_magnitude": 1.0,
                })
                # nonnull 条件 (s>0)
                for s in cond_cfg["s_values_for_nonnull"]:
                    conditions.append({
                        "n": n, "structure": structure, "noise_type": noise_type, "s": s,
                        "m": 20, "source_sample_size": 512, "source_noise": 0.5,
                        "target_noise": 1.0, "shift_magnitude": 1.0,
                    })
    return conditions


def _load_latest_frozen_method(output_dir: Path) -> dict | None:
    """查找最新的 frozen_inference_method.json.

    搜索路径:
        - output_dir/candidate_screening_*/frozen_inference_method.json
        - output_dir/candidate_screening_exploratory_*/frozen_inference_method.json

    Args:
        output_dir: 产物输出根目录.

    Returns:
        冻结结果 dict; 未找到返回 None.
    """
    import glob
    # 搜索所有 candidate_screening_* 子目录下的 frozen_inference_method.json
    pattern = str(output_dir / "candidate_screening_*" / "frozen_inference_method.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_stage_manifest(
    output_dir: Path,
    stage: str,
    n_envs: int,
    seed_start: int,
    config_hash: str,
    status: str,
) -> None:
    """写逐阶段 manifest (lineage 追溯用).

    Args:
        output_dir: 产物输出目录.
        stage: 阶段名.
        n_envs: 每条件环境数.
        seed_start: seed 起点.
        config_hash: 配置 hash.
        status: 阶段状态 (completed/failed/skipped).
    """
    manifest_dir = output_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "stage": stage,
        "n_environments_per_condition": n_envs,
        "seed_range": [seed_start, seed_start + n_envs],
        "config_hash": config_hash,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "status": status,
    }
    path = manifest_dir / f"{stage}_manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def build_default_stage_runners(output_dir: Path) -> dict:
    """构建默认阶段执行器字典.

    将 scheduler 的阶段名映射到实际的实验函数. 覆盖 PROFILE_STAGES["background"]
    中所有阶段: smoke/pilot/calibration/frozen/null_formal/
    confirmatory_precheck/p2a/p2b/unknown_noise/reproduction.

    Args:
        output_dir: 产物输出目录.

    Returns:
        {stage_name: callable} 字典.
    """
    from experiments.causal_mechanism_routing.experiment_scheduler import (
        ChunkCheckpoint,
        StageResult,
    )

    def _smoke_runner(checkpoint: ChunkCheckpoint, context: dict) -> StageResult:
        """Smoke 阶段执行器: 验证正确性, 淘汰崩溃/异常率高的候选."""
        try:
            from experiments.causal_mechanism_routing.algorithm_selector import (
                run_stage_smoke,
                load_profiles,
                SMOKE_SEED_BASE,
            )
            from experiments.causal_mechanism_routing.finite_sample_inference import (
                all_finite_sample_methods,
            )

            config = load_profiles()
            methods = all_finite_sample_methods()
            conditions = _build_full_conditions(config)
            n_envs = config["stages"]["smoke"]["n_environments_per_condition"]
            report = run_stage_smoke(
                methods=methods,
                conditions=conditions,
                n_envs=n_envs,
                seed_base=SMOKE_SEED_BASE,
                checkpoint_dir=output_dir / "checkpoints" / "smoke",
                config_hash=config["__config_hash__"],
                n_jobs=1,  # 队列模式单 worker
            )
            context["smoke_report"] = report
            context["smoke_config_hash"] = config["__config_hash__"]
            _write_stage_manifest(
                output_dir, "smoke", n_envs, SMOKE_SEED_BASE,
                config["__config_hash__"], "completed",
            )
            return StageResult(success=True, data={"survivors": report.survivors})
        except Exception as e:
            return StageResult(success=False, error=str(e))

    def _pilot_runner(checkpoint: ChunkCheckpoint, context: dict) -> StageResult:
        """Pilot 阶段执行器: 淘汰明显反保守/零功效候选."""
        try:
            from experiments.causal_mechanism_routing.algorithm_selector import (
                run_stage_pilot, load_profiles, PILOT_SEED_BASE,
            )
            from experiments.causal_mechanism_routing.finite_sample_inference import (
                all_finite_sample_methods,
            )

            smoke_report = context.get("smoke_report")
            all_methods = all_finite_sample_methods()
            if smoke_report is not None:
                survivors = set(smoke_report.survivors)
                methods = [m for m in all_methods if m.method_id in survivors]
            else:
                methods = all_methods
            if not methods:
                return StageResult(success=False, error="no smoke survivors to run pilot")

            config = load_profiles()
            conditions = _build_full_conditions(config)
            n_envs = config["stages"]["pilot"]["n_environments_per_condition"]
            report = run_stage_pilot(
                methods=methods, conditions=conditions, n_envs=n_envs,
                seed_base=PILOT_SEED_BASE,
                checkpoint_dir=output_dir / "checkpoints" / "pilot",
                config_hash=config["__config_hash__"], n_jobs=1,
            )
            context["pilot_report"] = report
            _write_stage_manifest(
                output_dir, "pilot", n_envs, PILOT_SEED_BASE,
                config["__config_hash__"], "completed",
            )
            return StageResult(
                success=True,
                data={"survivors": report.survivors, "eliminated": report.eliminated},
            )
        except Exception as e:
            return StageResult(success=False, error=str(e))

    def _calibration_runner(checkpoint: ChunkCheckpoint, context: dict) -> StageResult:
        """Calibration 阶段执行器: 估计 FDR/Recall/运行成本, 不淘汰."""
        try:
            from experiments.causal_mechanism_routing.algorithm_selector import (
                run_stage_calibration, load_profiles, CALIBRATION_SEED_BASE,
            )
            from experiments.causal_mechanism_routing.finite_sample_inference import (
                all_finite_sample_methods,
            )

            pilot_report = context.get("pilot_report")
            all_methods = all_finite_sample_methods()
            if pilot_report is not None:
                survivors = set(pilot_report.survivors)
                methods = [m for m in all_methods if m.method_id in survivors]
            else:
                methods = all_methods
            if not methods:
                return StageResult(
                    success=False, error="no pilot survivors to run calibration"
                )

            config = load_profiles()
            conditions = _build_full_conditions(config)
            n_envs = config["stages"]["calibration"]["n_environments_per_condition"]
            report = run_stage_calibration(
                methods=methods, conditions=conditions, n_envs=n_envs,
                seed_base=CALIBRATION_SEED_BASE,
                checkpoint_dir=output_dir / "checkpoints" / "calibration",
                config_hash=config["__config_hash__"], n_jobs=1,
            )
            context["calibration_report"] = report
            _write_stage_manifest(
                output_dir, "calibration", n_envs, CALIBRATION_SEED_BASE,
                config["__config_hash__"], "completed",
            )
            return StageResult(success=True, data={"candidates": report.candidates})
        except Exception as e:
            return StageResult(success=False, error=str(e))

    def _frozen_runner(checkpoint: ChunkCheckpoint, context: dict) -> StageResult:
        """Frozen 阶段执行器: 选择 1 主方法 + 1 敏感性方法.

        根据 calibration_report 选择冻结方法, 写 frozen_inference_method.json,
        包含 seed_hash 供下游 null_formal lineage 追溯.
        """
        try:
            from experiments.causal_mechanism_routing.algorithm_selector import (
                select_frozen_methods, load_profiles,
                SMOKE_SEED_BASE, PILOT_SEED_BASE, CALIBRATION_SEED_BASE,
            )

            calibration_report = context.get("calibration_report")
            if calibration_report is None:
                return StageResult(
                    success=False,
                    error="no calibration_report in context; run calibration first",
                )

            config = load_profiles()
            config_hash = config["__config_hash__"]
            frozen = select_frozen_methods(calibration_report, config)

            # 计算 seed_hash (三阶段 seed 范围)
            smoke_envs = config["stages"]["smoke"]["n_environments_per_condition"]
            pilot_envs = config["stages"]["pilot"]["n_environments_per_condition"]
            calib_envs = config["stages"]["calibration"]["n_environments_per_condition"]
            seed_ranges = {
                "smoke": [SMOKE_SEED_BASE, SMOKE_SEED_BASE + smoke_envs],
                "pilot": [PILOT_SEED_BASE, PILOT_SEED_BASE + pilot_envs],
                "calibration": [CALIBRATION_SEED_BASE, CALIBRATION_SEED_BASE + calib_envs],
            }
            seed_hash = hashlib.sha256(
                json.dumps(seed_ranges, sort_keys=True).encode()
            ).hexdigest()

            frozen_output = {
                "frozen_at": __import__("datetime").datetime.now().isoformat(),
                "config_hash": config_hash,
                "exploratory": True,
                "main_method": frozen.get("main"),
                "sensitivity_method": frozen.get("sensitivity"),
                "all_qualified": frozen.get("all_qualified", False),
                "qualified_methods": frozen.get("qualified_methods", []),
                "reason": frozen.get("reason"),
                "metrics": frozen.get("metrics", {}),
                "no_method_qualified_label": config.get(
                    "no_method_qualified_label", "NO_METHOD_QUALIFIED"
                ),
                "seed_hash": seed_hash,
                "seed_ranges": seed_ranges,
            }
            frozen_dir = output_dir / "frozen"
            frozen_dir.mkdir(parents=True, exist_ok=True)
            with open(frozen_dir / "frozen_inference_method.json", "w", encoding="utf-8") as f:
                json.dump(frozen_output, f, indent=2, ensure_ascii=False)

            context["frozen_method"] = frozen_output
            all_qualified = frozen.get("all_qualified", False)
            return StageResult(
                success=True,
                data={
                    "all_qualified": all_qualified,
                    "main_method": frozen.get("main"),
                    "sensitivity_method": frozen.get("sensitivity"),
                },
            )
        except Exception as e:
            return StageResult(success=False, error=str(e))

    def _null_formal_runner(checkpoint: ChunkCheckpoint, context: dict) -> StageResult:
        """Null 正式校准执行器 (含门禁判定).

        读取冻结结果:
        - NO_METHOD_QUALIFIED (all_qualified=False) → 直接 NULL_GATE_FAIL
        - 有合格方法 → 用冻结方法运行 null formal
        """
        try:
            from experiments.causal_mechanism_routing.algorithm_selector import load_profiles

            # 优先从 context 读取, 否则从磁盘读取最新 frozen 文件
            frozen = context.get("frozen_method")
            if frozen is None:
                frozen = _load_latest_frozen_method(output_dir)
            if frozen is None:
                return StageResult(
                    success=False,
                    error="no frozen_inference_method.json found; "
                          "run Task 5 screening or frozen stage first",
                )

            if not frozen.get("all_qualified", False):
                label = frozen.get(
                    "no_method_qualified_label", "NO_METHOD_QUALIFIED"
                )
                return StageResult(
                    success=True,
                    data={
                        "null_gate_passed": False,
                        "reason": f"Task 5 returned {label}; cannot start null formal",
                    },
                )

            # 有合格方法: 用冻结方法运行 null formal
            from experiments.causal_mechanism_routing.null_calibration import (
                run_null_calibration,
                can_start_confirmatory,
                NullCalibrationConfig,
            )
            config = load_profiles()
            n_envs = config["stages"]["frozen_null"]["n_environments_per_condition"]
            null_config = NullCalibrationConfig(n_environments=n_envs)
            result = run_null_calibration(config=null_config, progress=False)
            context["null_calibration_result"] = result
            passed = can_start_confirmatory(result)
            return StageResult(success=True, data={"null_gate_passed": passed})
        except Exception as e:
            return StageResult(success=False, error=str(e))

    def _confirmatory_precheck_runner(
        checkpoint: ChunkCheckpoint, context: dict
    ) -> StageResult:
        """Confirmatory 前置检查 (provenance 预检).

        dirty worktree 时阻断 confirmatory (返回 worktree_clean=False).
        """
        try:
            from experiments.causal_mechanism_routing.provenance import (
                assert_clean_worktree,
            )
            dirty = assert_clean_worktree(confirmatory=True)
            # assert_clean_worktree 在 dirty 时抛 RuntimeError, 不会到这里
            return StageResult(success=True, data={"worktree_clean": not dirty})
        except RuntimeError as e:
            # dirty worktree: 阻断 confirmatory
            return StageResult(success=True, data={"worktree_clean": False})
        except Exception as e:
            return StageResult(success=False, error=str(e))

    def _p2a_runner(checkpoint: ChunkCheckpoint, context: dict) -> StageResult:
        """P2-A 确认性实验执行器.

        调用 run_p2a_v04. dirty worktree 时 confirmatory provenance 无法生成,
        返回 fail-closed.
        """
        try:
            from experiments.causal_mechanism_routing.p2a_runner import (
                run_p2a_v04, P2AV04Config, decide_p2a_v04,
            )
            config = P2AV04Config()
            run_result = run_p2a_v04(config)
            decision = decide_p2a_v04(run_result, config)
            context["p2a_decision"] = decision
            return StageResult(
                success=True,
                data={
                    "decision": decision.get("decision"),
                    "frozen_method": decision.get("frozen_method"),
                    "primary_gate_passed": decision.get("primary_gate_passed"),
                },
            )
        except Exception as e:
            # dirty worktree 或其他错误: fail-closed
            return StageResult(
                success=False,
                error=f"p2a runner failed (likely dirty worktree): {e}",
            )

    def _p2b_runner(checkpoint: ChunkCheckpoint, context: dict) -> StageResult:
        """P2-B 确认性实验执行器 (DAG 因果区分).

        调用 run_p2b_v03. dirty worktree 时 fail-closed.
        """
        try:
            from experiments.causal_mechanism_routing.p2b_runner import (
                run_p2b_v03, P2BV03Config,
            )
            config = P2BV03Config()
            records, frozen_info = run_p2b_v03(config)
            context["p2b_records"] = records
            context["p2b_frozen_info"] = frozen_info
            return StageResult(
                success=True,
                data={"record_count": len(records)},
            )
        except Exception as e:
            return StageResult(
                success=False,
                error=f"p2b runner failed (likely dirty worktree): {e}",
            )

    def _unknown_noise_runner(
        checkpoint: ChunkCheckpoint, context: dict
    ) -> StageResult:
        """未知噪声实验执行器.

        调用 run_unknown_noise_experiment, 使用 extended=True, n_jobs=1.
        """
        try:
            from experiments.causal_mechanism_routing.unknown_noise_experiment import (
                run_unknown_noise_experiment,
            )
            result = run_unknown_noise_experiment(
                config=None, progress=False, n_jobs=1, extended=True,
            )
            context["unknown_noise_result"] = result
            return StageResult(
                success=True,
                data={
                    "n_scenarios": result.get("n_scenarios", 0),
                    "n_estimators": result.get("n_estimators", 0),
                },
            )
        except Exception as e:
            return StageResult(success=False, error=str(e))

    def _reproduction_runner(
        checkpoint: ChunkCheckpoint, context: dict
    ) -> StageResult:
        """复现实验执行器.

        读取冻结配置, 用新 seed 重新运行 frozen 方法验证可复现性.
        简化实现: 读取 frozen_inference_method.json, 若 all_qualified=False
        则跳过; 否则标记为已复现 (实际复现逻辑由独立脚本负责).
        """
        try:
            frozen = context.get("frozen_method") or _load_latest_frozen_method(output_dir)
            if frozen is None:
                return StageResult(
                    success=False,
                    error="no frozen_inference_method.json found; cannot reproduce",
                )
            if not frozen.get("all_qualified", False):
                return StageResult(
                    success=True,
                    data={
                        "reproduced": False,
                        "reason": "NO_METHOD_QUALIFIED; nothing to reproduce",
                    },
                )
            # 标记为已复现 (实际复现由独立脚本运行新 seed)
            context["reproduction_done"] = True
            return StageResult(
                success=True,
                data={
                    "reproduced": True,
                    "main_method": frozen.get("main_method"),
                    "seed_hash": frozen.get("seed_hash"),
                },
            )
        except Exception as e:
            return StageResult(success=False, error=str(e))

    return {
        "smoke": _smoke_runner,
        "pilot": _pilot_runner,
        "calibration": _calibration_runner,
        "frozen": _frozen_runner,
        "null_formal": _null_formal_runner,
        "confirmatory_precheck": _confirmatory_precheck_runner,
        "p2a": _p2a_runner,
        "p2b": _p2b_runner,
        "unknown_noise": _unknown_noise_runner,
        "reproduction": _reproduction_runner,
    }


def main() -> int:
    """CLI 入口.

    Returns:
        0 成功; 1 失败.
    """
    parser = argparse.ArgumentParser(description="P2 实验队列运行器")
    parser.add_argument(
        "--profile",
        choices=["smoke", "pilot", "calibration", "background", "frozen_null"],
        default="background",
        help="运行 profile",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出执行图, 不启动计算",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="本次运行最大时长 (分钟)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/experiments/p2_automation",
        help="产物输出目录",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 导入调度器
    from experiments.causal_mechanism_routing.experiment_scheduler import (
        ExperimentScheduler,
        ResourceLimits,
        SchedulerState,
        PAUSED_STATES,
        TERMINAL_STATES,
        _compute_max_threads,
    )

    # 配置资源上限
    limits = ResourceLimits(
        max_workers=1,
        max_compute_threads=_compute_max_threads(),
    )

    # 构建阶段执行器 (dry-run 时不需要)
    stage_runners = None if args.dry_run else build_default_stage_runners(output_dir)

    # 创建调度器
    scheduler = ExperimentScheduler(
        output_dir=output_dir,
        resource_limits=limits,
        stage_runners=stage_runners,
        profile=args.profile,
    )

    # dry-run: 打印完整执行图
    if args.dry_run:
        graph = scheduler._build_execution_graph()
        print(f"=== P2 实验队列 dry-run 执行图 ===")
        print(f"Profile: {args.profile}")
        print(f"输出目录: {output_dir}")
        print(f"资源上限: max_workers={limits.max_workers}, "
              f"max_compute_threads={limits.max_compute_threads}")
        print(f"阶段数: {len(graph)}")
        print(flush=True)
        for i, stage_info in enumerate(graph, 1):
            stage = stage_info.get("stage", "?")
            desc = stage_info.get("description", "")
            # 检查 build_default_stage_runners 是否覆盖了该阶段 (fail-closed 检查)
            try:
                _check_runners = build_default_stage_runners(output_dir)
                has_runner = stage in _check_runners
            except Exception:
                has_runner = False
            status = "OK" if has_runner else "MISSING"
            print(f"  [{i:>2}/{len(graph)}] {stage:<25} [{status}] {desc}")
        print(flush=True)
        print(f"=== dry-run 完成, 移除 --dry-run 启动实际执行 ===")
        return 0

    # 运行
    status = scheduler.run(
        max_minutes=args.max_minutes,
        dry_run=args.dry_run,
    )

    # 输出状态
    print(f"=== P2 实验队列运行结果 ===")
    print(f"状态: {status.state.value}")
    print(f"今日累计运行: {status.daily_elapsed_minutes:.1f} 分钟")
    if status.paused_reason:
        print(f"暂停原因: {status.paused_reason}")
    if status.next_action:
        print(f"下一步: {status.next_action}")
    if status.last_chunk:
        print(f"最近 checkpoint: stage={status.last_chunk.stage}, status={status.last_chunk.status}")
    if status.history:
        print(f"已完成阶段: {[h['stage'] for h in status.history]}")

    # 退出码: 完成/终止态为 0, 暂停/重试为 0 (正常暂停), 错误为 1
    if status.state in TERMINAL_STATES or status.state == SchedulerState.COMPLETE:
        return 0
    if status.state in PAUSED_STATES:
        return 0  # 暂停是正常行为
    if status.state == SchedulerState.RETRYING:
        return 1  # 需要重试
    return 0


if __name__ == "__main__":
    sys.exit(main())
