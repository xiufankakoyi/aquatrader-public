"""Task 6: 运行冻结后的正式 null calibration.

读取 Task 5 的 frozen_inference_method.json:
- 如果 all_qualified=False (NO_METHOD_QUALIFIED) → 直接输出 NULL_GATE_FAIL 并退出
- 如果有合格方法 → 用冻结的 main_method 运行 null formal

不再硬编码 A3 和 n=64: 方法和条件由冻结结果决定.

预注册条件（基于 Task 5 冻结方法，在查看 test 前限定）:
    - n: [64]
    - structure: ["independent", "dag"]
    - noise_type: ["gaussian", "heteroscedastic", "t5", "outlier"]
    - s: [0] (null only)
    - 共 1 × 2 × 4 × 1 = 8 个 null 条件

门禁阈值:
    - BH FDR 95% CI high <= 0.12
    - BY FDR 95% CI high <= 0.10
    - 所有条件通过才写 NULL_GATE_PASS
    - 任一条件失败写 NULL_GATE_FAIL 并终止下游 confirmatory 队列

产物写入 data/experiments/p2_automation/null_formal_exploratory/<timestamp>/
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 设置 BLAS/NumPy 线程上限（必须在 import numpy 之前）
# 与 run_p2_experiment_queue.py 一致，避免线程竞争
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
from joblib import Parallel, delayed

from experiments.causal_mechanism_routing.algorithm_selector import (
    FROZEN_NULL_SEED_BASE,
    _generate_env_for_condition,
    load_profiles,
)
from experiments.causal_mechanism_routing.finite_sample_inference import (
    FiniteSampleInferenceMethod,
    HC3NormalMethod,
    HC3FixedTMethod,
    WelchSatterthwaiteMethod,
    RademacherWildBootstrapTMethod,
    MammenWildBootstrapTMethod,
    StudentizedMultiplierBootstrapMethod,
)
from experiments.causal_mechanism_routing.multiple_testing import BHMethod, BYMethod
from experiments.causal_mechanism_routing.null_calibration import (
    _bootstrap_ci_mean,
    _cramer_von_mises_uniform_test,
    _ks_uniform_test,
    _wilson_ci,
    can_start_confirmatory,
)


# ==========================
# 门禁阈值（与 config/p2_experiment_profiles.json frozen_null 一致）
# ==========================

BH_FDR_CI_HIGH_MAX = 0.12
BY_FDR_CI_HIGH_MAX = 0.10
TARGET_FDR = 0.10
N_BOOTSTRAP_A3 = 499  # 与 Task 5 calibration 一致（frozen 决策基于此 seed 的结果）
A3_SEED = 42  # 与 Task 5 calibration 一致（frozen 决策基于此 seed 的结果）


# ==========================
# 冻结结果读取与方法构造
# ==========================


def _load_latest_frozen_method(output_root: Path | None = None) -> dict | None:
    """查找最新的 frozen_inference_method.json.

    搜索路径:
        - data/experiments/p2_automation/candidate_screening_*/frozen_inference_method.json

    Args:
        output_root: 产物输出根目录, None 时用默认 data/experiments/p2_automation.

    Returns:
        冻结结果 dict; 未找到返回 None.
    """
    import glob
    root = output_root or (Path("data/experiments/p2_automation"))
    pattern = str(root / "candidate_screening_*" / "frozen_inference_method.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _build_method_from_frozen(frozen: dict) -> FiniteSampleInferenceMethod:
    """根据冻结结果构造推断方法实例.

    根据 main_method ID 构造对应的方法, 使用与 Task 5 一致的
    n_bootstrap 和 seed.

    Args:
        frozen: frozen_inference_method.json 的 dict.

    Returns:
        推断方法实例.

    Raises:
        ValueError: 未知 method_id 或 all_qualified=False.
    """
    if not frozen.get("all_qualified", False):
        raise ValueError(
            "cannot build method from NO_METHOD_QUALIFIED frozen result"
        )
    method_id = frozen.get("main_method")
    if method_id is None:
        raise ValueError("frozen main_method is None")

    method_map = {
        "A0": lambda: HC3NormalMethod(),
        "A1": lambda: HC3FixedTMethod(),
        "A2": lambda: WelchSatterthwaiteMethod(),
        "A3": lambda: RademacherWildBootstrapTMethod(
            n_bootstrap=N_BOOTSTRAP_A3, seed=A3_SEED,
        ),
        "A4": lambda: MammenWildBootstrapTMethod(
            n_bootstrap=N_BOOTSTRAP_A3, seed=A3_SEED,
        ),
        "A5": lambda: StudentizedMultiplierBootstrapMethod(
            n_bootstrap=N_BOOTSTRAP_A3, multiplier="normal", seed=A3_SEED,
        ),
    }
    builder = method_map.get(method_id)
    if builder is None:
        raise ValueError(f"unknown method_id in frozen: {method_id}")
    return builder()


# ==========================
# 单环境 worker
# ==========================


def _run_single_env_a3(
    condition: dict,
    env_seed: int,
    method: FiniteSampleInferenceMethod,
    bh: BHMethod,
    by: BYMethod,
) -> dict:
    """运行单环境: 生成 null env → 推断方法 p-value → BH/BY 拒绝.

    Args:
        condition: null 条件 dict (s=0).
        env_seed: 环境随机种子.
        method: 推断方法实例 (与 Task 5 冻结结果一致).
        bh: BH 方法实例.
        by: BY 方法实例.

    Returns:
        dict: {crashed, p_values, bh_rejected_any, by_rejected_any,
               bh_reject_mask, by_reject_mask}
    """
    m = int(condition.get("m", 20))
    try:
        env = _generate_env_for_condition(condition, env_seed)
        result = method.infer(env)
        p = np.asarray(result.p_values, dtype=float)
        bh_reject = bh.adjust(p)
        by_reject = by.adjust(p)
        return {
            "crashed": False,
            "p_values": p,
            "bh_rejected_any": bool(bh_reject.any()),
            "by_rejected_any": bool(by_reject.any()),
            "bh_reject_mask": bh_reject,
            "by_reject_mask": by_reject,
        }
    except Exception as e:  # noqa: BLE001 — 防御性: 任何异常都标记为 crash
        return {
            "crashed": True,
            "p_values": None,
            "bh_rejected_any": False,
            "by_rejected_any": False,
            "bh_reject_mask": np.zeros(m, dtype=bool),
            "by_reject_mask": np.zeros(m, dtype=bool),
            "error": f"{type(e).__name__}: {e}",
        }


# ==========================
# 单条件运行
# ==========================


def _run_single_condition(
    condition: dict,
    n_envs: int,
    seed_base: int,
    method: FiniteSampleInferenceMethod,
    bh: BHMethod,
    by: BYMethod,
    n_jobs: int,
) -> dict:
    """运行单条件: n_envs 个 null envs, 聚合 FDR/CI/type-I.

    s=0 下所有机制都是 null, 任何拒绝都是误拒, 因此:
        FDR = FWER = P(至少拒绝一个)
    per-mechanism type-I error = 每个机制 j 的拒绝率.

    Args:
        condition: null 条件 dict.
        n_envs: 环境数.
        seed_base: seed 起点 (frozen_null 范围 400000+).
        method: 推断方法实例.
        bh: BH 实例.
        by: BY 实例.
        n_jobs: 并行数.

    Returns:
        条件结果 dict, 包含 bh/by 子字典和门禁判定.
    """
    m = int(condition.get("m", 20))
    seeds = list(range(seed_base, seed_base + n_envs))
    method_id = method.method_id

    t0 = time.perf_counter()
    # threading backend: numpy einsum 释放 GIL, 可多线程并行
    results = Parallel(n_jobs=n_jobs, backend="threading")(
        delayed(_run_single_env_a3)(condition, s, method, bh, by) for s in seeds
    )
    elapsed = time.perf_counter() - t0

    n_crashed = sum(1 for r in results if r["crashed"])
    valid_results = [r for r in results if not r["crashed"]]
    n_valid = len(valid_results)

    if n_valid == 0:
        # 全部崩溃: 视为门禁失败
        return {
            "condition": _condition_to_serializable(condition),
            "method": method_id,
            "n_environments": n_envs,
            "n_valid": 0,
            "n_crashed": n_crashed,
            "bh": _empty_method_result(BH_FDR_CI_HIGH_MAX),
            "by": _empty_method_result(BY_FDR_CI_HIGH_MAX),
            "ks_stat": 0.0,
            "ks_pvalue": 1.0,
            "cvm_stat": 0.0,
            "passed_gate": False,
            "elapsed_seconds": float(elapsed),
        }

    # family-level FDR (s=0 下 = FWER = P(至少一个误拒))
    bh_rejected_any = np.array(
        [r["bh_rejected_any"] for r in valid_results], dtype=bool
    )
    by_rejected_any = np.array(
        [r["by_rejected_any"] for r in valid_results], dtype=bool
    )

    bh_n_rejected = int(bh_rejected_any.sum())
    by_n_rejected = int(by_rejected_any.sum())

    bh_fdr_mean = bh_n_rejected / n_valid
    by_fdr_mean = by_n_rejected / n_valid

    bh_fdr_low, bh_fdr_high = _wilson_ci(bh_n_rejected, n_valid, alpha=0.05)
    by_fdr_low, by_fdr_high = _wilson_ci(by_n_rejected, n_valid, alpha=0.05)

    # per-mechanism type-I error
    bh_reject_masks = np.stack(
        [r["bh_reject_mask"] for r in valid_results], axis=0
    )  # (n_valid, m)
    by_reject_masks = np.stack(
        [r["by_reject_mask"] for r in valid_results], axis=0
    )

    bh_per_mech_type1 = bh_reject_masks.mean(axis=0)  # (m,)
    by_per_mech_type1 = by_reject_masks.mean(axis=0)

    bh_type1_mean = float(bh_per_mech_type1.mean())
    bh_type1_max = float(bh_per_mech_type1.max())
    by_type1_mean = float(by_per_mech_type1.mean())
    by_type1_max = float(by_per_mech_type1.max())

    # type1 环境层 bootstrap CI（与 null_calibration.py 口径一致）
    bh_type1_low, bh_type1_high = _bootstrap_ci_mean(
        bh_reject_masks.sum(axis=1) / m, n_boot=2000, rng_seed=42,
    )
    by_type1_low, by_type1_high = _bootstrap_ci_mean(
        by_reject_masks.sum(axis=1) / m, n_boot=2000, rng_seed=42,
    )

    # p-value 均匀性检验（汇总所有环境的 p-value）
    all_p = np.concatenate([r["p_values"] for r in valid_results])
    ks_stat, ks_pvalue = _ks_uniform_test(all_p)
    cvm_stat = _cramer_von_mises_uniform_test(all_p)

    # 门禁: BH CI high <= 0.12, BY CI high <= 0.10
    bh_passed = bh_fdr_high <= BH_FDR_CI_HIGH_MAX
    by_passed = by_fdr_high <= BY_FDR_CI_HIGH_MAX

    return {
        "condition": _condition_to_serializable(condition),
        "method": method_id,
        "n_environments": n_envs,
        "n_valid": n_valid,
        "n_crashed": n_crashed,
        "bh": {
            "fdr_mean": float(bh_fdr_mean),
            "fdr_ci95_low": float(bh_fdr_low),
            "fdr_ci95_high": float(bh_fdr_high),
            "fwer_mean": float(bh_fdr_mean),  # s=0 下 FDR = FWER
            "type1_error_mean": bh_type1_mean,
            "type1_error_max": bh_type1_max,
            "type1_error_ci95_low": float(bh_type1_low),
            "type1_error_ci95_high": float(bh_type1_high),
            "n_rejected_envs": bh_n_rejected,
            "passed_gate": bool(bh_passed),
            "bound": BH_FDR_CI_HIGH_MAX,
        },
        "by": {
            "fdr_mean": float(by_fdr_mean),
            "fdr_ci95_low": float(by_fdr_low),
            "fdr_ci95_high": float(by_fdr_high),
            "fwer_mean": float(by_fdr_mean),
            "type1_error_mean": by_type1_mean,
            "type1_error_max": by_type1_max,
            "type1_error_ci95_low": float(by_type1_low),
            "type1_error_ci95_high": float(by_type1_high),
            "n_rejected_envs": by_n_rejected,
            "passed_gate": bool(by_passed),
            "bound": BY_FDR_CI_HIGH_MAX,
        },
        "ks_stat": float(ks_stat),
        "ks_pvalue": float(ks_pvalue),
        "cvm_stat": float(cvm_stat),
        "passed_gate": bool(bh_passed and by_passed),
        "elapsed_seconds": float(elapsed),
    }


def _condition_to_serializable(condition: dict) -> dict:
    """条件 dict 转可序列化形式（只保留关键字段）.

    Args:
        condition: 原始条件 dict.

    Returns:
        只含 n/structure/noise_type/s/m 的 dict.
    """
    return {
        "n": int(condition["n"]),
        "structure": str(condition["structure"]),
        "noise_type": str(condition["noise_type"]),
        "s": int(condition.get("s", 0)),
        "m": int(condition.get("m", 20)),
    }


def _empty_method_result(bound: float) -> dict:
    """空方法结果（全部崩溃时使用）.

    Args:
        bound: 门禁上界.

    Returns:
        全 0 的方法结果 dict, passed_gate=False.
    """
    return {
        "fdr_mean": 1.0,
        "fdr_ci95_low": 1.0,
        "fdr_ci95_high": 1.0,
        "fwer_mean": 1.0,
        "type1_error_mean": 1.0,
        "type1_error_max": 1.0,
        "type1_error_ci95_low": 1.0,
        "type1_error_ci95_high": 1.0,
        "n_rejected_envs": 0,
        "passed_gate": False,
        "bound": bound,
    }


# ==========================
# 主运行函数
# ==========================


def run_null_formal_a3(
    conditions: list[dict],
    n_envs: int = 5000,
    seed_base: int = FROZEN_NULL_SEED_BASE,
    n_jobs: int = -1,
    progress: bool = True,
    method: FiniteSampleInferenceMethod | None = None,
) -> dict:
    """运行冻结方法的正式 null calibration.

    Args:
        conditions: null 条件列表 (s=0).
        n_envs: 每条件环境数 (5000).
        seed_base: seed 起点 (frozen_null 范围 400000).
        n_jobs: 并行数, -1 表示使用所有 CPU.
        progress: 是否打印进度.
        method: 推断方法实例, None 时默认用 A3 (向后兼容).

    Returns:
        {
            "method": method_id,
            "method_params": {...},
            "conditions": [...],
            "gate_summary": {bh_pass, by_pass, all_pass, failures},
            "results": [per-condition results],
            "total_elapsed_seconds": ...,
        }
    """
    # 如果未传入 method, 默认用 A3 (向后兼容旧调用方)
    if method is None:
        method = RademacherWildBootstrapTMethod(
            n_bootstrap=N_BOOTSTRAP_A3, seed=A3_SEED,
        )
    bh = BHMethod(alpha=TARGET_FDR)
    by = BYMethod(alpha=TARGET_FDR)

    if n_jobs == -1:
        n_jobs = min(os.cpu_count() or 8, 8)

    all_results: list[dict] = []
    overall_start = time.perf_counter()

    for cond_idx, condition in enumerate(conditions, 1):
        if progress:
            print(
                f"[null_formal] ({cond_idx}/{len(conditions)}) "
                f"n={condition['n']} structure={condition['structure']} "
                f"noise={condition['noise_type']} s={condition['s']} ...",
                flush=True,
            )

        result = _run_single_condition(
            condition=condition,
            n_envs=n_envs,
            seed_base=seed_base,
            method=method,
            bh=bh,
            by=by,
            n_jobs=n_jobs,
        )
        all_results.append(result)

        if progress:
            bh_r = result["bh"]
            by_r = result["by"]
            status = "PASS" if result["passed_gate"] else "FAIL"
            print(
                f"  -> BH FDR={bh_r['fdr_mean']:.4f} "
                f"CI=[{bh_r['fdr_ci95_low']:.4f}, {bh_r['fdr_ci95_high']:.4f}] "
                f"BY FDR={by_r['fdr_mean']:.4f} "
                f"CI=[{by_r['fdr_ci95_low']:.4f}, {by_r['fdr_ci95_high']:.4f}] "
                f"type1_max={bh_r['type1_error_max']:.4f} "
                f"KS_p={result['ks_pvalue']:.4g} "
                f"({result['elapsed_seconds']:.1f}s) [{status}]",
                flush=True,
            )
            if not result["passed_gate"]:
                fails = []
                if not bh_r["passed_gate"]:
                    fails.append(
                        f"BH CI high {bh_r['fdr_ci95_high']:.4f} > "
                        f"{BH_FDR_CI_HIGH_MAX}"
                    )
                if not by_r["passed_gate"]:
                    fails.append(
                        f"BY CI high {by_r['fdr_ci95_high']:.4f} > "
                        f"{BY_FDR_CI_HIGH_MAX}"
                    )
                print(
                    f"  WARNING: gate FAILED ({', '.join(fails)})",
                    flush=True,
                )

    total_elapsed = time.perf_counter() - overall_start

    # 门禁汇总
    bh_failures = [
        {
            "condition": r["condition"],
            "bh_fdr_ci95_high": r["bh"]["fdr_ci95_high"],
            "bound": BH_FDR_CI_HIGH_MAX,
        }
        for r in all_results
        if not r["bh"]["passed_gate"]
    ]
    by_failures = [
        {
            "condition": r["condition"],
            "by_fdr_ci95_high": r["by"]["fdr_ci95_high"],
            "bound": BY_FDR_CI_HIGH_MAX,
        }
        for r in all_results
        if not r["by"]["passed_gate"]
    ]

    all_pass = len(bh_failures) == 0 and len(by_failures) == 0

    gate_summary = {
        "bh_pass": len(bh_failures) == 0,
        "by_pass": len(by_failures) == 0,
        "all_pass": all_pass,
        "bh_failures": bh_failures,
        "by_failures": by_failures,
        "bh_upper_bound": BH_FDR_CI_HIGH_MAX,
        "by_upper_bound": BY_FDR_CI_HIGH_MAX,
    }

    if progress:
        print(
            f"[null_formal] done in {total_elapsed:.1f}s, "
            f"BH pass={gate_summary['bh_pass']} "
            f"BY pass={gate_summary['by_pass']}",
            flush=True,
        )

    # 提取 method 元信息 (lineage 追溯用)
    method_id = method.method_id
    method_params: dict = {"seed": getattr(method, "seed", None)}
    if hasattr(method, "n_bootstrap"):
        method_params["n_bootstrap"] = method.n_bootstrap
    if hasattr(method, "weight_type"):
        method_params["weight_type"] = method.weight_type
    if hasattr(method, "multiplier"):
        method_params["multiplier"] = method.multiplier

    return {
        "method": method_id,
        "method_params": method_params,
        "gate_summary": gate_summary,
        "results": all_results,
        "total_elapsed_seconds": float(total_elapsed),
        "n_conditions": len(conditions),
        "n_envs_per_condition": n_envs,
    }


# ==========================
# JSON 序列化辅助
# ==========================


def _json_default(obj):
    """JSON 序列化 fallback (numpy types)."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Cannot serialize {type(obj)}")


# ==========================
# CLI 入口
# ==========================


def _build_preregistered_conditions() -> list[dict]:
    """构建预注册的 n=64 null 条件.

    基于 Task 5 calibration 发现，预注册限定 n>=64（在查看 test 前）.
    共 8 个 null 条件.

    Returns:
        条件 dict 列表.
    """
    conditions = []
    for structure in ["independent", "dag"]:
        for noise_type in ["gaussian", "heteroscedastic", "t5", "outlier"]:
            conditions.append({
                "n": 64,
                "structure": structure,
                "noise_type": noise_type,
                "s": 0,
                "m": 20,
                "source_sample_size": 512,
                "source_noise": 0.5,
                "target_noise": 1.0,
                "shift_magnitude": 1.0,
            })
    return conditions


def main(argv: list[str] | None = None) -> int:
    """CLI 入口.

    读取最新的 frozen_inference_method.json:
        - 如果 all_qualified=False (NO_METHOD_QUALIFIED) → 直接输出 NULL_GATE_FAIL 并退出
        - 如果有合格方法 → 用冻结的 main_method 运行 null formal

    Args:
        argv: 命令行参数 (None 时用 sys.argv).

    Returns:
        0 if NULL_GATE_PASS; 1 if NULL_GATE_FAIL.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Task 6: 运行冻结后的正式 null calibration (方法由 frozen 决定)"
    )
    parser.add_argument(
        "--n-envs",
        type=int,
        default=5000,
        help="每条件环境数 (default: 5000, spec 下限)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke 模式: n_envs=50, 用于快速验证脚本正确性",
    )
    parser.add_argument(
        "--n-jobs",
        type=int,
        default=-1,
        help="并行数, -1 表示使用所有 CPU (default: -1)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="输出目录 (default: data/experiments/p2_automation/null_formal_exploratory/<timestamp>)",
    )
    parser.add_argument(
        "--frozen-path",
        type=str,
        default=None,
        help="frozen_inference_method.json 路径, 默认搜索最新 candidate_screening_*",
    )
    args = parser.parse_args(argv)

    n_envs = 50 if args.smoke else args.n_envs

    # 1. 加载配置 (计算 config_hash, lineage 追溯)
    config = load_profiles()
    config_hash = config["__config_hash__"]

    # 2. 读取最新的冻结结果 (Task 5 输出)
    if args.frozen_path is not None:
        frozen_path = Path(args.frozen_path)
        if not frozen_path.exists():
            print(f"ERROR: frozen 文件不存在: {frozen_path}")
            return 1
        with open(frozen_path, encoding="utf-8") as f:
            frozen = json.load(f)
    else:
        frozen = _load_latest_frozen_method()
        if frozen is None:
            print("ERROR: 未找到 frozen_inference_method.json")
            print("  请先运行 scripts/run_candidate_screening.py 完成 Task 5")
            return 1

    # 3. 检查 all_qualified: NO_METHOD_QUALIFIED → 直接 NULL_GATE_FAIL
    if not frozen.get("all_qualified", False):
        # Task 5 正式结论: NO_METHOD_QUALIFIED, 不可继续 null formal
        # 诚实接受: 不再生成 n=64 子集运行作为正式 Task 6
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if args.output_dir is not None:
            output_dir = Path(args.output_dir)
        else:
            suffix = "_smoke" if args.smoke else ""
            output_dir = (
                Path("data/experiments/p2_automation")
                / f"null_formal_exploratory{suffix}"
                / timestamp
            )
        output_dir.mkdir(parents=True, exist_ok=True)

        result = {
            "method": None,
            "method_params": {},
            "gate_summary": {
                "bh_pass": False,
                "by_pass": False,
                "all_pass": False,
                "bh_failures": [],
                "by_failures": [],
                "bh_upper_bound": BH_FDR_CI_HIGH_MAX,
                "by_upper_bound": BY_FDR_CI_HIGH_MAX,
            },
            "results": [],
            "total_elapsed_seconds": 0.0,
            "n_conditions": 0,
            "n_envs_per_condition": n_envs,
            "elapsed_seconds": 0.0,
            "exploratory": True,
            "git_dirty": True,
            "frozen_method": None,
            "frozen_path": frozen.get("frozen_path") if isinstance(frozen, dict) else None,
            "config_hash": config_hash,
            "timestamp": timestamp,
            "is_smoke": bool(args.smoke),
            "verdict": "NULL_GATE_FAIL",
            "reason": "Task 5 returned NO_METHOD_QUALIFIED; cannot start null formal",
            "frozen_all_qualified": False,
        }
        output_path = output_dir / "null_formal_result.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=_json_default)

        print("=== Task 5 正式结论: NO_METHOD_QUALIFIED ===")
        print("  - 不可继续 null formal")
        print("  - 不再生成 n=64 子集运行作为正式 Task 6")
        print("  - null_formal 阶段直接返回 NULL_GATE_FAIL")
        print(f"\n判定: NULL_GATE_FAIL")
        print(f"产物: {output_path}")
        return 1

    # 4. 有合格方法: 构造方法实例
    try:
        method = _build_method_from_frozen(frozen)
    except ValueError as e:
        print(f"ERROR: 无法从 frozen 构造方法: {e}")
        return 1

    method_id = method.method_id

    # 5. 构建预注册条件 (仍以 n=64 为下限, 在查看 test 前限定)
    conditions = _build_preregistered_conditions()

    # 6. 运行
    print(f"=== Task 6: Null Formal Calibration ===")
    print(f"方法: {method_id} (由 Task 5 frozen 决定)")
    print(f"frozen seed_hash: {frozen.get('seed_hash', 'N/A')}")
    print(f"条件数: {len(conditions)}")
    print(f"每条件环境数: {n_envs}")
    print(f"总环境数: {len(conditions) * n_envs}")
    print(f"seed_base: {FROZEN_NULL_SEED_BASE} (frozen_null range)")
    print(f"门禁: BH FDR CI high <= {BH_FDR_CI_HIGH_MAX}, BY FDR CI high <= {BY_FDR_CI_HIGH_MAX}")
    print(flush=True)

    t0 = time.perf_counter()
    result = run_null_formal_a3(
        conditions=conditions,
        n_envs=n_envs,
        seed_base=FROZEN_NULL_SEED_BASE,
        n_jobs=args.n_jobs,
        progress=True,
        method=method,
    )
    elapsed = time.perf_counter() - t0

    # 7. 写入产物
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        suffix = "_smoke" if args.smoke else ""
        output_dir = (
            Path("data/experiments/p2_automation")
            / f"null_formal_exploratory{suffix}"
            / timestamp
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    result["elapsed_seconds"] = float(elapsed)
    result["exploratory"] = True
    result["git_dirty"] = True
    result["frozen_method"] = method_id
    result["frozen_seed_hash"] = frozen.get("seed_hash")
    result["n_limitation"] = (
        "n>=64 (based on Task 5 calibration finding, pre-registered before test)"
    )
    result["config_hash"] = config_hash
    result["timestamp"] = timestamp
    result["pre_registered_conditions"] = {
        "n_values": [64],
        "structures": ["independent", "dag"],
        "noise_types": ["gaussian", "heteroscedastic", "t5", "outlier"],
        "s_values": [0],
        "n_envs_per_condition": n_envs,
        "gate_thresholds": {
            "bh_fdr_ci_high_max": BH_FDR_CI_HIGH_MAX,
            "by_fdr_ci_high_max": BY_FDR_CI_HIGH_MAX,
        },
        "seed_base": FROZEN_NULL_SEED_BASE,
    }
    result["is_smoke"] = bool(args.smoke)

    output_path = output_dir / "null_formal_result.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=_json_default)

    # 8. 检查门禁
    gate = result["gate_summary"]
    print(f"\n=== 门禁结果 ===")
    print(f"BH pass: {gate['bh_pass']}")
    print(f"BY pass: {gate['by_pass']}")
    print(f"All pass: {gate['all_pass']}")

    if gate["all_pass"]:
        verdict = "NULL_GATE_PASS"
    else:
        verdict = "NULL_GATE_FAIL"
        print(f"\n失败条件:")
        if gate["bh_failures"]:
            print(f"  BH (bound={BH_FDR_CI_HIGH_MAX}):")
            for f in gate["bh_failures"]:
                c = f["condition"]
                print(
                    f"    n={c['n']} {c['structure']} {c['noise_type']} s={c['s']}: "
                    f"CI high={f['bh_fdr_ci95_high']:.4f}"
                )
        if gate["by_failures"]:
            print(f"  BY (bound={BY_FDR_CI_HIGH_MAX}):")
            for f in gate["by_failures"]:
                c = f["condition"]
                print(
                    f"    n={c['n']} {c['structure']} {c['noise_type']} s={c['s']}: "
                    f"CI high={f['by_fdr_ci95_high']:.4f}"
                )

    print(f"\n判定: {verdict}")

    # 9. 验证 can_start_confirmatory 一致性
    can_start = can_start_confirmatory(result)
    print(f"can_start_confirmatory(result) = {can_start}")
    if can_start != gate["all_pass"]:
        print(
            f"WARNING: 一致性检查失败: can_start={can_start} != "
            f"all_pass={gate['all_pass']}"
        )

    print(f"\n产物: {output_path}")
    print(f"总耗时: {elapsed:.1f}s ({elapsed / 60:.1f}min)")

    if args.smoke:
        print("\n[smoke] 跳过 Task 7 判定 (smoke 模式)")
        return 0

    if gate["all_pass"]:
        print(f"\n→ Checkpoint C 通过, 可以继续 Task 7 (P2-A confirmatory)")
        return 0
    else:
        print(f"\n→ NULL_GATE_FAIL, 终止下游 confirmatory 队列")
        return 1


if __name__ == "__main__":
    sys.exit(main())
