"""Task 5: 运行候选筛选与冻结.

使用 algorithm_selector 的 run_stage_smoke/pilot/calibration,
用 8 核 joblib 并行运行三阶段, 输出候选比较报告和冻结决策.

产物写入 data/experiments/p2_automation/candidate_screening_exploratory_<timestamp>/

seed 管理（三阶段互斥, 不读取 test 数据）:
    - smoke:        [100000, 100000 + n_envs)
    - pilot:        [200000, 200000 + n_envs)
    - calibration:  [300000, 300000 + n_envs)
    - test (frozen_null/P2-A/P2-B/reproduction): >= 400000  ← 本脚本不触及

用法:
    # 完整三阶段（默认 50/200/1000 envs）
    python scripts/run_candidate_screening.py

    # 快速验证管道端到端可跑通（减少条件数和 envs）
    python scripts/run_candidate_screening.py --quick

    # 自定义参数
    python scripts/run_candidate_screening.py --smoke-envs 50 --pilot-envs 200 --calibration-envs 500
    python scripts/run_candidate_screening.py --noise-types gaussian heteroscedastic --s-values 3
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# 必须在 import numpy 之前设置 BLAS 线程上限为 1,
# 否则 joblib threading 后端会与 BLAS 内部线程竞争, 反而变慢.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from experiments.causal_mechanism_routing.algorithm_selector import (
    CALIBRATION_SEED_BASE,
    PILOT_SEED_BASE,
    SMOKE_SEED_BASE,
    StageReport,
    _json_default,
    load_profiles,
    run_stage_calibration,
    run_stage_pilot,
    run_stage_smoke,
    select_frozen_methods,
)
from experiments.causal_mechanism_routing.finite_sample_inference import (
    all_finite_sample_methods,
)


# ========== 条件构建 ==========


def build_conditions(
    config: dict,
    n_values: list[int] | None = None,
    structures: list[str] | None = None,
    noise_types: list[str] | None = None,
    s_values: list[int] | None = None,
) -> list[dict]:
    """根据 config 构建条件列表.

    条件维度:
        - n: [8, 16, 32, 64]
        - structure: ["independent", "dag"]
        - noise_type: ["gaussian", "heteroscedastic", "t5", "outlier"]
        - s: [0] for null + [1, 3, 5] for nonnull

    Args:
        config: profiles 配置 dict.
        n_values: 自定义 n 列表, None 时用 config.
        structures: 自定义 structure 列表, None 时用 config.
        noise_types: 自定义 noise_type 列表, None 时用 config.
        s_values: 自定义 nonnull s 列表, None 时用 config.

    Returns:
        条件 dict 列表, 每个 dict 含 n/structure/noise_type/s/m 等参数.
    """
    cond_cfg = config["conditions"]
    n_values = n_values if n_values is not None else cond_cfg["n_values"]
    structures = structures if structures is not None else cond_cfg["structures"]
    noise_types = noise_types if noise_types is not None else cond_cfg["noise_types"]
    s_values = s_values if s_values is not None else cond_cfg["s_values_for_nonnull"]

    conditions: list[dict] = []
    for n in n_values:
        for structure in structures:
            for noise_type in noise_types:
                # null 条件 (s=0): 用于 FDR/FWER 评估
                conditions.append({
                    "n": n,
                    "structure": structure,
                    "noise_type": noise_type,
                    "s": 0,
                    "m": 20,
                    "source_sample_size": 512,
                    "source_noise": 0.5,
                    "target_noise": 1.0,
                    "shift_magnitude": 1.0,
                })
                # nonnull 条件 (s>0): 用于 Recall 评估
                for s in s_values:
                    conditions.append({
                        "n": n,
                        "structure": structure,
                        "noise_type": noise_type,
                        "s": s,
                        "m": 20,
                        "source_sample_size": 512,
                        "source_noise": 0.5,
                        "target_noise": 1.0,
                        "shift_magnitude": 1.0,
                    })
    return conditions


# ========== git 信息 ==========


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
        output_dir: 输出目录.
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
        "timestamp": datetime.now().isoformat(),
        "status": status,
    }
    path = manifest_dir / f"{stage}_manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def _compute_seed_hash(seed_ranges: dict) -> str:
    """计算 seed_hash (三阶段 seed 范围的 SHA256).

    Args:
        seed_ranges: {stage: [start, end]} 字典.

    Returns:
        16 字符 SHA256 前缀.
    """
    return hashlib.sha256(
        json.dumps(seed_ranges, sort_keys=True).encode()
    ).hexdigest()[:16]


def _get_git_info() -> dict:
    """获取 git commit 和 dirty 状态.

    Returns:
        dict 包含 git_commit, git_branch, git_dirty.
    """
    info = {"git_commit": "unknown", "git_branch": "unknown", "git_dirty": True}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=_PROJECT_ROOT, timeout=5,
        )
        if result.returncode == 0:
            info["git_commit"] = result.stdout.strip()
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=_PROJECT_ROOT, timeout=5,
        )
        if result.returncode == 0:
            info["git_branch"] = result.stdout.strip()
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=_PROJECT_ROOT, timeout=5,
        )
        info["git_dirty"] = bool(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        pass
    return info


# ========== 报告序列化 ==========


def _report_to_dict(report: StageReport) -> dict:
    """将 StageReport 转为可 JSON 序列化的 dict.

    Args:
        report: 阶段报告.

    Returns:
        包含 stage/candidates/eliminated/survivors/evaluations 的 dict.
    """
    return {
        "stage": report.stage,
        "candidates": report.candidates,
        "eliminated": report.eliminated,
        "survivors": report.survivors,
        "config_hash": report.config_hash,
        "timestamp": report.timestamp,
        "evaluations": [asdict(e) for e in report.evaluations],
    }


def _write_comparison_csv(report: StageReport, path: Path) -> None:
    """将候选评估结果写入 CSV.

    每行 = (method_id, condition) 的评估指标, 包含:
    FDR(BH/BY)、Recall、Recall CI low、运行时间、失败率、
    p-value 尾部分位数（mean/min/q25/q50/q75/q95/max）、paired CI.

    Args:
        report: Calibration 阶段报告（也可用于其它阶段）.
        path: 输出 CSV 路径.
    """
    # CSV 表头
    base_fields = [
        "method_id", "condition", "stage", "n_envs",
        "null_fdr_bh", "null_fdr_by",
        "nonnull_recall", "recall_ci_low",
        "mean_runtime_seconds", "crash_rate",
        "p_value_in_range_rate", "bootstrap_count",
        "elapsed_seconds",
    ]
    p_quantile_fields = [
        "p_mean", "p_min", "p_q25", "p_q50", "p_q75", "p_q95", "p_max",
    ]
    fieldnames = base_fields + p_quantile_fields + ["failure_reasons"]

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ev in report.evaluations:
            row = {
                "method_id": ev.method_id,
                "condition": ev.condition,
                "stage": ev.stage,
                "n_envs": ev.n_environments,
                "null_fdr_bh": f"{ev.null_fdr_bh:.6f}",
                "null_fdr_by": f"{ev.null_fdr_by:.6f}",
                "nonnull_recall": f"{ev.nonnull_recall:.6f}",
                "recall_ci_low": f"{ev.recall_ci_low:.6f}",
                "mean_runtime_seconds": f"{ev.mean_runtime_seconds:.6f}",
                "crash_rate": f"{ev.crash_rate:.6f}",
                "p_value_in_range_rate": f"{ev.p_value_in_range_rate:.6f}",
                "bootstrap_count": ev.bootstrap_count if ev.bootstrap_count is not None else "",
                "elapsed_seconds": f"{ev.elapsed_seconds:.6f}",
                "failure_reasons": "; ".join(ev.failure_reasons) if ev.failure_reasons else "",
            }
            # 展平 p_value_stats
            stats = ev.p_value_stats or {}
            for q in ["mean", "min", "q25", "q50", "q75", "q95", "max"]:
                val = stats.get(q)
                row[f"p_{q}"] = f"{val:.6f}" if val is not None else ""
            writer.writerow(row)


# ========== 三阶段运行 ==========


def _run_stage_with_timing(
    stage_name: str,
    runner,
    methods,
    conditions,
    n_envs,
    seed_base,
    output_dir,
    config_hash,
    n_jobs,
) -> tuple[StageReport, float]:
    """运行单阶段并计时, 打印进度.

    Args:
        stage_name: 阶段名（用于打印）.
        runner: 阶段运行函数 (run_stage_smoke/pilot/calibration).
        methods: 候选方法列表.
        conditions: 条件列表.
        n_envs: 每条件环境数.
        seed_base: seed 起点.
        output_dir: 输出目录（checkpoint 写入其下）.
        config_hash: 配置 hash.
        n_jobs: 并行数.

    Returns:
        (StageReport, elapsed_seconds).
    """
    checkpoint_dir = output_dir / "checkpoints" / stage_name
    n_evals = len(methods) * len(conditions) * n_envs
    print(f"\n=== {stage_name.capitalize()} 阶段 ===")
    print(f"  方法数: {len(methods)} ({[m.method_id for m in methods]})")
    print(f"  条件数: {len(conditions)}")
    print(f"  envs/条件: {n_envs}")
    print(f"  总 evaluation 数: {n_evals}")
    print(f"  seed 范围: [{seed_base}, {seed_base + n_envs})")
    print(f"  并行核数: {n_jobs}")

    t0 = time.perf_counter()
    report = runner(
        methods=methods,
        conditions=conditions,
        n_envs=n_envs,
        seed_base=seed_base,
        checkpoint_dir=checkpoint_dir,
        config_hash=config_hash,
        n_jobs=n_jobs,
    )
    elapsed = time.perf_counter() - t0
    print(f"{stage_name.capitalize()} 完成: {elapsed:.1f}s")
    if hasattr(report, "eliminated"):
        print(f"  淘汰: {report.eliminated}")
        print(f"  存活: {report.survivors}")
    return report, elapsed


def main():
    """主入口: 解析参数, 运行三阶段, 输出冻结决策."""
    parser = argparse.ArgumentParser(
        description="Task 5: 运行候选筛选与冻结",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="快速验证模式: 减少条件数和 envs, 仅验证管道端到端可跑通",
    )
    parser.add_argument("--smoke-envs", type=int, default=None, help="Smoke 阶段每条件环境数")
    parser.add_argument("--pilot-envs", type=int, default=None, help="Pilot 阶段每条件环境数")
    parser.add_argument(
        "--calibration-envs", type=int, default=None,
        help="Calibration 阶段每条件环境数",
    )
    parser.add_argument(
        "--n-values", type=int, nargs="+", default=None,
        help="自定义 n 值列表, 如 --n-values 8 32 64",
    )
    parser.add_argument(
        "--noise-types", type=str, nargs="+", default=None,
        choices=["gaussian", "heteroscedastic", "t5", "outlier"],
        help="自定义 noise_type 列表",
    )
    parser.add_argument(
        "--s-values", type=int, nargs="+", default=None,
        help="自定义 nonnull s 值列表, 如 --s-values 3",
    )
    parser.add_argument(
        "--skip-calibration", action="store_true",
        help="跳过 Calibration 阶段（仅运行 Smoke + Pilot）",
    )
    parser.add_argument(
        "--n-jobs", type=int, default=-1,
        help="并行核数, -1 表示使用所有 CPU 核心（默认）",
    )
    args = parser.parse_args()

    # 1. 加载配置
    config = load_profiles()
    config_hash = config["__config_hash__"]
    print(f"配置 hash: {config_hash}")

    # 2. 确定 envs 和条件
    if args.quick:
        smoke_envs = args.smoke_envs or 20
        pilot_envs = args.pilot_envs or 50
        calibration_envs = args.calibration_envs or 100
        n_values = args.n_values or [8, 32]
        noise_types = args.noise_types or ["gaussian", "heteroscedastic"]
        s_values = args.s_values or [3]
        print("[QUICK 模式] 减少条件数和 envs 以快速验证管道")
    else:
        smoke_envs = args.smoke_envs or config["stages"]["smoke"]["n_environments_per_condition"]
        pilot_envs = args.pilot_envs or config["stages"]["pilot"]["n_environments_per_condition"]
        calibration_envs = (
            args.calibration_envs
            or config["stages"]["calibration"]["n_environments_per_condition"]
        )
        n_values = args.n_values or None
        noise_types = args.noise_types or None
        s_values = args.s_values or None

    conditions = build_conditions(
        config,
        n_values=n_values,
        noise_types=noise_types,
        s_values=s_values,
    )
    print(f"每阶段 envs: smoke={smoke_envs}, pilot={pilot_envs}, calibration={calibration_envs}")
    print(f"总条件数: {len(conditions)}")

    # 3. 创建输出目录（带时间戳, 不覆盖历史产物）
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "quick" if args.quick else "full"
    output_dir = (
        Path("data/experiments/p2_automation")
        / f"candidate_screening_exploratory_{suffix}_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"产物目录: {output_dir}")

    # 4. 获取 git 信息
    git_info = _get_git_info()
    print(f"git commit: {git_info['git_commit'][:12]}")
    print(f"git branch: {git_info['git_branch']}")
    print(f"git dirty: {git_info['git_dirty']}")

    # 5. 获取所有候选方法 A0-A5
    all_methods = all_finite_sample_methods()
    print(f"候选方法: {[m.method_id for m in all_methods]}")

    # 6. Smoke 阶段
    smoke_report, smoke_elapsed = _run_stage_with_timing(
        "smoke", run_stage_smoke,
        all_methods, conditions, smoke_envs,
        SMOKE_SEED_BASE, output_dir, config_hash, args.n_jobs,
    )
    _write_stage_manifest(
        output_dir, "smoke", smoke_envs, SMOKE_SEED_BASE, config_hash, "completed",
    )
    with open(output_dir / "smoke_report.json", "w", encoding="utf-8") as f:
        json.dump(_report_to_dict(smoke_report), f, indent=2, ensure_ascii=False, default=_json_default)

    # 7. Pilot 阶段（仅 smoke 存活者）
    pilot_methods = [m for m in all_methods if m.method_id in smoke_report.survivors]
    if not pilot_methods:
        print("\n无 smoke 存活者, 跳过 Pilot 和 Calibration")
        _write_no_qualified_output(
            output_dir, config, config_hash, git_info, conditions,
            smoke_envs=smoke_envs, pilot_envs=0,
            smoke_report=smoke_report, smoke_elapsed=smoke_elapsed,
            reason="no smoke survivors",
        )
        return

    pilot_report, pilot_elapsed = _run_stage_with_timing(
        "pilot", run_stage_pilot,
        pilot_methods, conditions, pilot_envs,
        PILOT_SEED_BASE, output_dir, config_hash, args.n_jobs,
    )
    _write_stage_manifest(
        output_dir, "pilot", pilot_envs, PILOT_SEED_BASE, config_hash, "completed",
    )
    with open(output_dir / "pilot_report.json", "w", encoding="utf-8") as f:
        json.dump(_report_to_dict(pilot_report), f, indent=2, ensure_ascii=False, default=_json_default)

    # 8. Calibration 阶段（仅 pilot 存活者）
    calibration_methods = [m for m in pilot_methods if m.method_id in pilot_report.survivors]
    if not calibration_methods or args.skip_calibration:
        if args.skip_calibration:
            print("\n--skip-calibration 已指定, 跳过 Calibration 阶段")
            _write_no_qualified_output(
                output_dir, config, config_hash, git_info, conditions,
                smoke_envs=smoke_envs, pilot_envs=pilot_envs,
                smoke_report=smoke_report, smoke_elapsed=smoke_elapsed,
                pilot_report=pilot_report, pilot_elapsed=pilot_elapsed,
                reason="calibration skipped by --skip-calibration",
            )
        else:
            print("\n无 pilot 存活者, 跳过 Calibration")
            _write_no_qualified_output(
                output_dir, config, config_hash, git_info, conditions,
                smoke_envs=smoke_envs, pilot_envs=pilot_envs,
                smoke_report=smoke_report, smoke_elapsed=smoke_elapsed,
                pilot_report=pilot_report, pilot_elapsed=pilot_elapsed,
                reason="no pilot survivors",
            )
        return

    calibration_report, calibration_elapsed = _run_stage_with_timing(
        "calibration", run_stage_calibration,
        calibration_methods, conditions, calibration_envs,
        CALIBRATION_SEED_BASE, output_dir, config_hash, args.n_jobs,
    )
    _write_stage_manifest(
        output_dir, "calibration", calibration_envs,
        CALIBRATION_SEED_BASE, config_hash, "completed",
    )
    with open(output_dir / "calibration_report.json", "w", encoding="utf-8") as f:
        json.dump(_report_to_dict(calibration_report), f, indent=2, ensure_ascii=False, default=_json_default)

    # 9. 冻结决策
    print("\n=== 冻结决策 ===")
    frozen = select_frozen_methods(calibration_report, config)
    print(f"主方法: {frozen.get('main', 'N/A')}")
    print(f"敏感性方法: {frozen.get('sensitivity', 'N/A')}")
    print(f"全部合格: {frozen.get('all_qualified', False)}")
    print(f"合格方法: {frozen.get('qualified_methods', [])}")
    print(f"理由: {frozen.get('reason', 'N/A')}")

    # 10. 输出 frozen_inference_method.json
    seed_ranges = {
        "smoke": [SMOKE_SEED_BASE, SMOKE_SEED_BASE + smoke_envs],
        "pilot": [PILOT_SEED_BASE, PILOT_SEED_BASE + pilot_envs],
        "calibration": [CALIBRATION_SEED_BASE, CALIBRATION_SEED_BASE + calibration_envs],
        "test_untouched": ">= 400000 (frozen_null/P2-A/P2-B/reproduction)",
    }
    seed_hash = _compute_seed_hash({
        "smoke": seed_ranges["smoke"],
        "pilot": seed_ranges["pilot"],
        "calibration": seed_ranges["calibration"],
    })
    frozen_output = {
        "frozen_at": datetime.now().isoformat(),
        "config_hash": config_hash,
        "exploratory": True,  # dirty worktree
        "git_dirty": git_info["git_dirty"],
        "git_commit": git_info["git_commit"],
        "git_branch": git_info["git_branch"],
        "main_method": frozen.get("main"),
        "sensitivity_method": frozen.get("sensitivity"),
        "all_qualified": frozen.get("all_qualified", False),
        "qualified_methods": frozen.get("qualified_methods", []),
        "reason": frozen.get("reason"),
        "metrics": frozen.get("metrics", {}),
        "no_method_qualified_label": config.get("no_method_qualified_label", "NO_METHOD_QUALIFIED"),
        "seed_hash": seed_hash,
        "stage_summaries": {
            "smoke": {
                "elapsed_seconds": smoke_elapsed,
                "eliminated": smoke_report.eliminated,
                "survivors": smoke_report.survivors,
                "n_envs_per_condition": smoke_envs,
                "n_conditions": len(conditions),
            },
            "pilot": {
                "elapsed_seconds": pilot_elapsed,
                "eliminated": pilot_report.eliminated,
                "survivors": pilot_report.survivors,
                "n_envs_per_condition": pilot_envs,
                "n_conditions": len(conditions),
            },
            "calibration": {
                "elapsed_seconds": calibration_elapsed,
                "candidates": calibration_report.candidates,
                "n_envs_per_condition": calibration_envs,
                "n_conditions": len(conditions),
            },
        },
        "seed_ranges": seed_ranges,
        "conditions_summary": {
            "n_values": sorted({c["n"] for c in conditions}),
            "structures": sorted({c["structure"] for c in conditions}),
            "noise_types": sorted({c["noise_type"] for c in conditions}),
            "s_values": sorted({c["s"] for c in conditions}),
            "total_conditions": len(conditions),
        },
    }
    with open(output_dir / "frozen_inference_method.json", "w", encoding="utf-8") as f:
        json.dump(frozen_output, f, indent=2, ensure_ascii=False, default=_json_default)

    # 11. 输出候选比较 CSV（含 calibration 全部评估）
    _write_comparison_csv(calibration_report, output_dir / "candidate_comparison.csv")

    # 12. 诚实接受 NO_METHOD_QUALIFIED 作为 Task 5 正式结论
    if not frozen.get("all_qualified", False):
        print(
            "\nTask 5 正式结论: NO_METHOD_QUALIFIED，不可继续 null formal"
        )
        print("  - 不再生成 n=64 子集运行作为正式 Task 6")
        print("  - null_formal 阶段应直接返回 NULL_GATE_FAIL")

    print(f"\n产物已写入: {output_dir}")
    print(f"  - frozen_inference_method.json")
    print(f"  - candidate_comparison.csv")
    print(f"  - smoke_report.json")
    print(f"  - pilot_report.json")
    print(f"  - calibration_report.json")
    print(f"  - manifests/ (逐阶段 manifest)")
    print(f"  - checkpoints/")
    print(f"\n总耗时: {smoke_elapsed + pilot_elapsed + calibration_elapsed:.1f}s")


def _write_no_qualified_output(
    output_dir: Path,
    config: dict,
    config_hash: str,
    git_info: dict,
    conditions: list[dict],
    smoke_envs: int = 0,
    pilot_envs: int = 0,
    smoke_report: StageReport | None = None,
    smoke_elapsed: float = 0.0,
    pilot_report: StageReport | None = None,
    pilot_elapsed: float = 0.0,
    reason: str = "",
) -> None:
    """当无候选合格或阶段跳过时, 输出 NO_METHOD_QUALIFIED 标记.

    诚实接受 NO_METHOD_QUALIFIED 作为 Task 5 正式结论:
    - main_method/sensitivity_method 置为 null (而非 label 字符串)
    - all_qualified=False
    - 包含 seed_hash 供 lineage 追溯
    - 不再生成 n=64 子集运行作为正式 Task 6

    Args:
        output_dir: 输出目录.
        config: profiles 配置.
        config_hash: 配置 hash.
        git_info: git 信息 dict.
        conditions: 条件列表.
        smoke_envs: Smoke 阶段每条件环境数.
        pilot_envs: Pilot 阶段每条件环境数.
        smoke_report: Smoke 报告（可能为 None）.
        smoke_elapsed: Smoke 耗时.
        pilot_report: Pilot 报告（可能为 None）.
        pilot_elapsed: Pilot 耗时.
        reason: 未合格原因.
    """
    no_method_label = config.get("no_method_qualified_label", "NO_METHOD_QUALIFIED")
    # seed_ranges 仍写入, 供 lineage 追溯 (即使无合格方法)
    seed_ranges = {
        "smoke": [SMOKE_SEED_BASE, SMOKE_SEED_BASE + smoke_envs],
        "pilot": [PILOT_SEED_BASE, PILOT_SEED_BASE + pilot_envs],
        "calibration": None,
        "test_untouched": ">= 400000 (frozen_null/P2-A/P2-B/reproduction)",
    }
    # 仅对非 None 的范围计算 seed_hash
    seed_hash_input = {
        k: v for k, v in seed_ranges.items()
        if isinstance(v, list)
    }
    seed_hash = _compute_seed_hash(seed_hash_input) if seed_hash_input else ""
    frozen_output = {
        "frozen_at": datetime.now().isoformat(),
        "config_hash": config_hash,
        "exploratory": True,
        "git_dirty": git_info["git_dirty"],
        "git_commit": git_info["git_commit"],
        "git_branch": git_info["git_branch"],
        # 诚实接受 NO_METHOD_QUALIFIED: main/sensitivity 置 null
        "main_method": None,
        "sensitivity_method": None,
        "all_qualified": False,
        "qualified_methods": [],
        "reason": reason,
        "no_method_qualified_label": no_method_label,
        "seed_hash": seed_hash,
        "stage_summaries": {
            "smoke": {
                "elapsed_seconds": smoke_elapsed,
                "eliminated": smoke_report.eliminated if smoke_report else [],
                "survivors": smoke_report.survivors if smoke_report else [],
                "n_envs_per_condition": smoke_envs,
                "n_conditions": len(conditions),
            },
            "pilot": {
                "elapsed_seconds": pilot_elapsed,
                "eliminated": pilot_report.eliminated if pilot_report else [],
                "survivors": pilot_report.survivors if pilot_report else [],
                "n_envs_per_condition": pilot_envs,
                "n_conditions": len(conditions),
            },
            "calibration": None,
        },
        "seed_ranges": seed_ranges,
    }
    with open(output_dir / "frozen_inference_method.json", "w", encoding="utf-8") as f:
        json.dump(frozen_output, f, indent=2, ensure_ascii=False, default=_json_default)

    # 仍输出 smoke/pilot 的比较 CSV（如果有）
    if smoke_report is not None:
        _write_comparison_csv(smoke_report, output_dir / "smoke_comparison.csv")
    if pilot_report is not None:
        _write_comparison_csv(pilot_report, output_dir / "pilot_comparison.csv")

    # 诚实接受 NO_METHOD_QUALIFIED 作为 Task 5 正式结论
    print(
        f"\nTask 5 正式结论: {no_method_label}，不可继续 null formal"
    )
    print(f"  原因: {reason}")
    print("  - 不再生成 n=64 子集运行作为正式 Task 6")
    print("  - null_formal 阶段应直接返回 NULL_GATE_FAIL")

    print(f"\n产物已写入: {output_dir}")
    print(f"  - frozen_inference_method.json ({no_method_label})")
    if smoke_report is not None:
        print(f"  - smoke_comparison.csv")
    if pilot_report is not None:
        print(f"  - pilot_comparison.csv")


if __name__ == "__main__":
    main()
