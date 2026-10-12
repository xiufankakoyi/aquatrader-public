"""Task 10: 独立复现.

使用全新 seed（500000+）执行独立复现，验证:
1. Null formal NULL_GATE_FAIL 是否可复现
2. 小样本反保守是否可复现

严格应用预注册容差:
- all_within_tolerance: 复现结果与原结果的相对差异 < 10%
- false 时报告"方向性一致但容差失败"

复现 seed 管理（与原 100000-499999 完全不重叠）:
    - null_formal:  [500000, 505000)
    - smoke:        [600000, 600050)
    - pilot:        [700000, 700200)

复现原则:
    - 不读取原 test label 或结果来调整方法
    - 仅读取原 null_formal_result.json 用于事后容差比较（不参与方法选择）
    - 容差严格按 10% 相对差异，不得放宽
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 必须在 import numpy 之前设置 BLAS 线程上限为 1,
# 与 run_null_formal_calibration.py / run_candidate_screening.py 一致,
# 避免 joblib threading 后端与 BLAS 内部线程竞争.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 让本脚本可以直接 import 同目录下的兄弟脚本模块
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import numpy as np

from experiments.causal_mechanism_routing.algorithm_selector import (
    FROZEN_NULL_SEED_BASE,
    PILOT_SEED_BASE,
    SMOKE_SEED_BASE,
    load_profiles,
    run_stage_pilot,
    run_stage_smoke,
)
from experiments.causal_mechanism_routing.finite_sample_inference import (
    all_finite_sample_methods,
)

# 复用 Task 5/6 的实现，确保方法不变（只换 seed）
from run_candidate_screening import build_conditions
from run_null_formal_calibration import (
    BH_FDR_CI_HIGH_MAX,
    BY_FDR_CI_HIGH_MAX,
    _build_preregistered_conditions,
    run_null_formal_a3,
)


# ==========================
# 复现 seed 基点（新范围，与原 100000-499999 不重叠）
# ==========================

#: 复现 null formal seed 起点（原 FROZEN_NULL_SEED_BASE=400000）
REPRO_NULL_SEED_BASE = 500000

#: 复现 smoke 阶段 seed 起点（原 SMOKE_SEED_BASE=100000）
REPRO_SMOKE_SEED_BASE = 600000

#: 复现 pilot 阶段 seed 起点（原 PILOT_SEED_BASE=200000）
REPRO_PILOT_SEED_BASE = 700000


# ==========================
# 容差与比较配置
# ==========================

#: 预注册容差阈值（相对差异 < 10%）
TOLERANCE_REL_DIFF = 0.10

#: 原 null formal 结果路径（只读取用于事后比较，不调整方法）
ORIGINAL_NULL_FORMAL_PATH = (
    _PROJECT_ROOT
    / "data"
    / "experiments"
    / "p2_automation"
    / "null_formal_exploratory"
    / "20260716_025137"
    / "null_formal_result.json"
)

#: 原候选筛选结果路径（完整 128 条件 NO_METHOD_QUALIFIED 的那次运行）
ORIGINAL_SCREENING_PATH = (
    _PROJECT_ROOT
    / "data"
    / "experiments"
    / "p2_automation"
    / "candidate_screening_exploratory_full_20260716_015929"
    / "frozen_inference_method.json"
)


# ==========================
# 加载原结果（仅用于事后比较）
# ==========================


def _load_original_null_formal() -> dict | None:
    """加载原 null formal 结果用于事后比较.

    本函数只读取产物文件用于容差计算, 不参与方法/参数调整.

    Returns:
        原 null formal 结果 dict, 文件不存在时返回 None.
    """
    if not ORIGINAL_NULL_FORMAL_PATH.exists():
        return None
    with open(ORIGINAL_NULL_FORMAL_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_original_screening() -> dict | None:
    """加载原候选筛选结果用于事后比较."""
    if not ORIGINAL_SCREENING_PATH.exists():
        return None
    with open(ORIGINAL_SCREENING_PATH, encoding="utf-8") as f:
        return json.load(f)


# ==========================
# 容差与方向性检查
# ==========================


def _check_null_tolerance(original: dict, repro: dict) -> dict:
    """检查 BH FDR mean 的相对差异是否在预注册容差内.

    预注册规则:
        - 对每个条件, 计算 |repro - original| / max(|original|, eps)
        - 所有条件相对差异 < TOLERANCE_REL_DIFF 才算 all_within_tolerance=true
        - false 时只能报告"方向性一致但容差失败", 不得事后放宽阈值

    Args:
        original: 原 null formal 结果.
        repro: 复现 null formal 结果.

    Returns:
        dict 包含:
            - all_within_tolerance: bool
            - per_condition: list of per-condition 比较结果
            - max_rel_diff: float
            - tolerance_threshold: float
    """
    eps = 1e-9
    original_by_cond: dict[tuple, float] = {}
    for r in original.get("results", []):
        c = r["condition"]
        key = (c["n"], c["structure"], c["noise_type"], c["s"])
        original_by_cond[key] = float(r["bh"]["fdr_mean"])

    per_condition: list[dict] = []
    max_rel_diff = 0.0
    for r in repro.get("results", []):
        c = r["condition"]
        key = (c["n"], c["structure"], c["noise_type"], c["s"])
        if key not in original_by_cond:
            continue
        orig = original_by_cond[key]
        repro_val = float(r["bh"]["fdr_mean"])
        rel_diff = abs(repro_val - orig) / max(abs(orig), eps)
        within = rel_diff < TOLERANCE_REL_DIFF
        per_condition.append({
            "condition": c,
            "original_bh_fdr_mean": orig,
            "repro_bh_fdr_mean": repro_val,
            "rel_diff": float(rel_diff),
            "within_tolerance": bool(within),
        })
        if rel_diff > max_rel_diff:
            max_rel_diff = rel_diff

    all_within = (
        len(per_condition) > 0
        and all(p["within_tolerance"] for p in per_condition)
    )
    return {
        "all_within_tolerance": bool(all_within),
        "per_condition": per_condition,
        "max_rel_diff": float(max_rel_diff),
        "tolerance_threshold": TOLERANCE_REL_DIFF,
    }


def _check_null_directional(original: dict, repro: dict) -> dict:
    """检查 NULL_GATE_FAIL 是否方向性可复现.

    方向性一致 := 原 verdict 与复现 verdict 相同.

    Args:
        original: 原 null formal 结果.
        repro: 复现 null formal 结果.

    Returns:
        dict 包含原/复现 verdict、BH/BY 通过状态、失败条件数.
    """
    o_gate = original["gate_summary"]
    r_gate = repro["gate_summary"]
    o_verdict = "NULL_GATE_PASS" if o_gate["all_pass"] else "NULL_GATE_FAIL"
    r_verdict = "NULL_GATE_PASS" if r_gate["all_pass"] else "NULL_GATE_FAIL"
    consistent = (o_verdict == r_verdict)
    return {
        "original_verdict": o_verdict,
        "repro_verdict": r_verdict,
        "directionally_consistent": bool(consistent),
        "original_bh_pass": bool(o_gate["bh_pass"]),
        "repro_bh_pass": bool(r_gate["bh_pass"]),
        "original_by_pass": bool(o_gate["by_pass"]),
        "repro_by_pass": bool(r_gate["by_pass"]),
        "original_bh_failures_count": len(o_gate.get("bh_failures", [])),
        "repro_bh_failures_count": len(r_gate.get("bh_failures", [])),
    }


def _check_screening_directional(original: dict, repro: dict) -> dict:
    """检查候选筛选方向性一致性.

    小样本反保守 := Pilot 阶段所有方法被淘汰 (survivors=[]).
    原结果（128 条件）= NO_METHOD_QUALIFIED（pilot 无存活者）.

    Args:
        original: 原候选筛选 frozen_inference_method.json.
        repro: 复现候选筛选结果.

    Returns:
        dict 包含原/复现 pilot survivors、all_eliminated 标志、方向一致性.
    """
    o_pilot = original.get("stage_summaries", {}).get("pilot") or {}
    r_pilot = repro.get("pilot") or {}
    o_survivors = list(o_pilot.get("survivors") or [])
    r_survivors = list(r_pilot.get("survivors") or [])
    o_all_elim = (len(o_survivors) == 0)
    r_all_elim = (len(r_survivors) == 0)
    return {
        "original_pilot_survivors": o_survivors,
        "repro_pilot_survivors": r_survivors,
        "original_pilot_eliminated": list(o_pilot.get("eliminated") or []),
        "repro_pilot_eliminated": list(r_pilot.get("eliminated") or []),
        "original_all_eliminated": bool(o_all_elim),
        "repro_all_eliminated": bool(r_all_elim),
        "directionally_consistent": bool(o_all_elim == r_all_elim),
    }


# ==========================
# 复现运行
# ==========================


def run_repro_null_formal(n_jobs: int = -1, progress: bool = True) -> dict:
    """运行 null formal 复现.

    使用新 seed_base=500000, 与原 400000 完全不重叠.
    方法固定为 A3（Rademacher wild bootstrap-t, B=499, seed=42），
    与原 Task 6 完全一致——只换 data seed 范围，不调方法。

    Args:
        n_jobs: 并行数, -1 表示使用所有 CPU.
        progress: 是否打印进度.

    Returns:
        run_null_formal_a3 返回的 result dict, 附带复现元信息.
    """
    conditions = _build_preregistered_conditions()
    if progress:
        print(
            f"=== Reproduction: Null Formal "
            f"(A3, n=64, seed_base={REPRO_NULL_SEED_BASE}) ==="
        )
        print(f"方法: A3 (Rademacher wild bootstrap-t, B=499, seed=42)")
        print(f"条件数: {len(conditions)}")
        print(f"每条件环境数: 5000")
        print(f"总环境数: {len(conditions) * 5000}")
        print(
            f"门禁: BH FDR CI high <= {BH_FDR_CI_HIGH_MAX}, "
            f"BY FDR CI high <= {BY_FDR_CI_HIGH_MAX}"
        )
        print(flush=True)

    t0 = time.perf_counter()
    result = run_null_formal_a3(
        conditions=conditions,
        n_envs=5000,
        seed_base=REPRO_NULL_SEED_BASE,
        n_jobs=n_jobs,
        progress=progress,
    )
    elapsed = time.perf_counter() - t0

    # 附带复现元信息（不修改方法参数）
    # 注意: run_null_formal_a3 返回的 dict 不含 pre_registered_conditions 字段
    # （该字段由原 main() 添加），因此这里独立记录复现元信息。
    result["elapsed_seconds"] = float(elapsed)
    result["exploratory"] = True
    result["git_dirty"] = True
    result["frozen_method"] = "A3"
    result["reproduction"] = True
    result["reproduction_seed_base"] = REPRO_NULL_SEED_BASE
    result["original_seed_base"] = FROZEN_NULL_SEED_BASE
    result["reproduction_pre_registered_conditions"] = {
        "n_values": [64],
        "structures": ["independent", "dag"],
        "noise_types": ["gaussian", "heteroscedastic", "t5", "outlier"],
        "s_values": [0],
        "n_envs_per_condition": 5000,
        "gate_thresholds": {
            "bh_fdr_ci_high_max": BH_FDR_CI_HIGH_MAX,
            "by_fdr_ci_high_max": BY_FDR_CI_HIGH_MAX,
        },
        "seed_base": REPRO_NULL_SEED_BASE,
    }
    return result


def run_repro_candidate_screening(
    n_jobs: int = -1, progress: bool = True,
) -> dict:
    """运行候选筛选复现 (Smoke + Pilot, 完整 128 条件).

    使用新 seed_base=600000 (smoke) 和 700000 (pilot)，
    与原 100000/200000 完全不重叠. 方法集为 A0-A5 全部，不调方法.

    Args:
        n_jobs: 并行数, -1 表示使用所有 CPU.
        progress: 是否打印进度.

    Returns:
        dict 包含 smoke/pilot 阶段汇总和复现元信息.
    """
    config = load_profiles()
    config_hash = config["__config_hash__"]

    # 完整 128 条件: n=8/16/32/64 × 2 structures × 4 noise_types × 4 s_values
    conditions = build_conditions(config)
    if progress:
        print(
            f"\n=== Reproduction: Candidate Screening "
            f"(Smoke + Pilot, seed_base={REPRO_SMOKE_SEED_BASE}/"
            f"{REPRO_PILOT_SEED_BASE}) ==="
        )
        print(f"条件数: {len(conditions)} (n=[8,16,32,64] × 2 × 4 × 4)")

    all_methods = all_finite_sample_methods()
    if progress:
        print(f"候选方法: {[m.method_id for m in all_methods]}")

    # Smoke 阶段: 50 envs/条件
    if progress:
        print(
            f"\n--- Smoke 阶段 (50 envs/条件, "
            f"seed_base={REPRO_SMOKE_SEED_BASE}) ---"
        )
    t0 = time.perf_counter()
    smoke_report = run_stage_smoke(
        methods=all_methods,
        conditions=conditions,
        n_envs=50,
        seed_base=REPRO_SMOKE_SEED_BASE,
        checkpoint_dir=None,
        config_hash=config_hash,
        n_jobs=n_jobs,
    )
    smoke_elapsed = time.perf_counter() - t0
    if progress:
        print(
            f"Smoke 完成: {smoke_elapsed:.1f}s, "
            f"存活: {smoke_report.survivors}, "
            f"淘汰: {smoke_report.eliminated}"
        )

    # Pilot 阶段: 200 envs/条件（仅 smoke 存活者）
    pilot_methods = [
        m for m in all_methods if m.method_id in smoke_report.survivors
    ]
    pilot_summary: dict | None = None
    pilot_elapsed = 0.0
    if pilot_methods:
        if progress:
            print(
                f"\n--- Pilot 阶段 (200 envs/条件, "
                f"seed_base={REPRO_PILOT_SEED_BASE}) ---"
            )
        t0 = time.perf_counter()
        pilot_report = run_stage_pilot(
            methods=pilot_methods,
            conditions=conditions,
            n_envs=200,
            seed_base=REPRO_PILOT_SEED_BASE,
            checkpoint_dir=None,
            config_hash=config_hash,
            n_jobs=n_jobs,
        )
        pilot_elapsed = time.perf_counter() - t0
        if progress:
            print(
                f"Pilot 完成: {pilot_elapsed:.1f}s, "
                f"存活: {pilot_report.survivors}, "
                f"淘汰: {pilot_report.eliminated}"
            )
        pilot_summary = {
            "stage": "pilot",
            "candidates": list(pilot_report.candidates),
            "eliminated": list(pilot_report.eliminated),
            "survivors": list(pilot_report.survivors),
            "n_envs_per_condition": 200,
            "n_conditions": len(conditions),
            "elapsed_seconds": float(pilot_elapsed),
            "seed_base": REPRO_PILOT_SEED_BASE,
        }
    else:
        if progress:
            print("\n无 smoke 存活者, 跳过 Pilot")

    return {
        "smoke": {
            "stage": "smoke",
            "candidates": list(smoke_report.candidates),
            "eliminated": list(smoke_report.eliminated),
            "survivors": list(smoke_report.survivors),
            "n_envs_per_condition": 50,
            "n_conditions": len(conditions),
            "elapsed_seconds": float(smoke_elapsed),
            "seed_base": REPRO_SMOKE_SEED_BASE,
        },
        "pilot": pilot_summary,
        "n_conditions": len(conditions),
        "config_hash": config_hash,
        "reproduction": True,
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


def main(argv: list[str] | None = None) -> int:
    """CLI 入口.

    Args:
        argv: 命令行参数 (None 时用 sys.argv).

    Returns:
        0 表示复现完成（无论容差是否通过）；1 表示参数错误或致命错误.
    """
    parser = argparse.ArgumentParser(
        description="Task 10: 独立复现 (新 seed 500000+)",
    )
    parser.add_argument(
        "--n-jobs", type=int, default=-1,
        help="并行数, -1 表示使用所有 CPU (default: -1)",
    )
    parser.add_argument(
        "--skip-null-formal", action="store_true",
        help="跳过 null formal 复现",
    )
    parser.add_argument(
        "--skip-screening", action="store_true",
        help="跳过候选筛选复现",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help=(
            "输出目录 (default: "
            "data/experiments/p2_automation/independent_reproduction_<timestamp>)"
        ),
    )
    args = parser.parse_args(argv)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        output_dir = (
            _PROJECT_ROOT
            / "data"
            / "experiments"
            / "p2_automation"
            / f"independent_reproduction_{timestamp}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Task 10: 独立复现 ===")
    print(f"产物目录: {output_dir}")
    print(
        f"复现 seed 范围: null_formal={REPRO_NULL_SEED_BASE}, "
        f"smoke={REPRO_SMOKE_SEED_BASE}, pilot={REPRO_PILOT_SEED_BASE}"
    )
    print(
        f"原 seed 范围: null_formal={FROZEN_NULL_SEED_BASE}, "
        f"smoke={SMOKE_SEED_BASE}, pilot={PILOT_SEED_BASE}"
    )
    print(f"容差: 相对差异 < {TOLERANCE_REL_DIFF * 100:.0f}%")
    print(flush=True)

    overall_t0 = time.perf_counter()

    # 1. Null formal 复现
    null_repro: dict | None = None
    null_tolerance: dict | None = None
    null_direction: dict | None = None
    if not args.skip_null_formal:
        null_repro = run_repro_null_formal(n_jobs=args.n_jobs, progress=True)
        with open(
            output_dir / "null_formal_reproduction.json", "w", encoding="utf-8"
        ) as f:
            json.dump(
                null_repro, f, indent=2, ensure_ascii=False,
                default=_json_default,
            )

        original_null = _load_original_null_formal()
        if original_null is not None:
            null_tolerance = _check_null_tolerance(original_null, null_repro)
            null_direction = _check_null_directional(original_null, null_repro)
            print(f"\n--- Null formal 容差检查 ---")
            print(
                f"all_within_tolerance: "
                f"{null_tolerance['all_within_tolerance']}"
            )
            print(
                f"max_rel_diff: {null_tolerance['max_rel_diff']:.4f} "
                f"(阈值 {TOLERANCE_REL_DIFF})"
            )
            for p in null_tolerance["per_condition"]:
                status = "PASS" if p["within_tolerance"] else "FAIL"
                c = p["condition"]
                print(
                    f"  n={c['n']} {c['structure']}/{c['noise_type']}: "
                    f"orig={p['original_bh_fdr_mean']:.4f} "
                    f"repro={p['repro_bh_fdr_mean']:.4f} "
                    f"rel_diff={p['rel_diff']:.4f} [{status}]"
                )
            print(f"\n--- Null formal 方向性一致性 ---")
            print(f"原 verdict: {null_direction['original_verdict']}")
            print(f"复现 verdict: {null_direction['repro_verdict']}")
            print(
                f"directionally_consistent: "
                f"{null_direction['directionally_consistent']}"
            )
            print(
                f"原 BH failures: "
                f"{null_direction['original_bh_failures_count']}, "
                f"复现 BH failures: "
                f"{null_direction['repro_bh_failures_count']}"
            )
            if not null_tolerance["all_within_tolerance"]:
                if null_direction["directionally_consistent"]:
                    print("→ 方向性一致但容差失败")
                else:
                    print("→ 方向性不一致（需要进一步调查）")
        else:
            print(
                f"WARNING: 原结果不存在: {ORIGINAL_NULL_FORMAL_PATH}"
            )

    # 2. 候选筛选复现
    screening_repro: dict | None = None
    screening_direction: dict | None = None
    if not args.skip_screening:
        screening_repro = run_repro_candidate_screening(
            n_jobs=args.n_jobs, progress=True,
        )
        with open(
            output_dir / "candidate_screening_reproduction.json",
            "w", encoding="utf-8",
        ) as f:
            json.dump(
                screening_repro, f, indent=2, ensure_ascii=False,
                default=_json_default,
            )

        original_screening = _load_original_screening()
        if original_screening is not None:
            screening_direction = _check_screening_directional(
                original_screening, screening_repro,
            )
            print(f"\n--- 候选筛选方向性一致性 ---")
            print(
                f"原 pilot survivors: "
                f"{screening_direction['original_pilot_survivors']}"
            )
            print(
                f"复现 pilot survivors: "
                f"{screening_direction['repro_pilot_survivors']}"
            )
            print(
                f"原 all_eliminated: "
                f"{screening_direction['original_all_eliminated']}, "
                f"复现 all_eliminated: "
                f"{screening_direction['repro_all_eliminated']}"
            )
            print(
                f"directionally_consistent: "
                f"{screening_direction['directionally_consistent']}"
            )
        else:
            print(
                f"WARNING: 原结果不存在: {ORIGINAL_SCREENING_PATH}"
            )

    overall_elapsed = time.perf_counter() - overall_t0

    # 3. 汇总判定
    null_verdict: str | None = None
    if null_tolerance and null_direction:
        if null_tolerance["all_within_tolerance"]:
            null_verdict = "REPRODUCED_WITHIN_TOLERANCE"
        elif null_direction["directionally_consistent"]:
            null_verdict = "DIRECTIONALLY_CONSISTENT_BUT_TOLERANCE_FAILED"
        else:
            null_verdict = "DIRECTIONALLY_INCONSISTENT"

    screening_verdict: str | None = None
    if screening_direction:
        screening_verdict = (
            "REPRODUCED" if screening_direction["directionally_consistent"]
            else "DIRECTIONALLY_INCONSISTENT"
        )

    comparison = {
        "null_formal": {
            "all_within_tolerance": (
                null_tolerance["all_within_tolerance"]
                if null_tolerance else None
            ),
            "max_rel_diff": (
                null_tolerance["max_rel_diff"] if null_tolerance else None
            ),
            "tolerance_threshold": TOLERANCE_REL_DIFF,
            "directionally_consistent": (
                null_direction["directionally_consistent"]
                if null_direction else None
            ),
            "original_verdict": (
                null_direction["original_verdict"] if null_direction else None
            ),
            "repro_verdict": (
                null_direction["repro_verdict"] if null_direction else None
            ),
            "original_bh_failures_count": (
                null_direction["original_bh_failures_count"]
                if null_direction else None
            ),
            "repro_bh_failures_count": (
                null_direction["repro_bh_failures_count"]
                if null_direction else None
            ),
            "verdict": null_verdict,
        },
        "candidate_screening": {
            "directionally_consistent": (
                screening_direction["directionally_consistent"]
                if screening_direction else None
            ),
            "original_all_eliminated": (
                screening_direction["original_all_eliminated"]
                if screening_direction else None
            ),
            "repro_all_eliminated": (
                screening_direction["repro_all_eliminated"]
                if screening_direction else None
            ),
            "original_pilot_survivors": (
                screening_direction["original_pilot_survivors"]
                if screening_direction else None
            ),
            "repro_pilot_survivors": (
                screening_direction["repro_pilot_survivors"]
                if screening_direction else None
            ),
            "verdict": screening_verdict,
        },
        "overall": {
            "total_elapsed_seconds": float(overall_elapsed),
            "reproduction_seed_bases": {
                "null_formal": REPRO_NULL_SEED_BASE,
                "smoke": REPRO_SMOKE_SEED_BASE,
                "pilot": REPRO_PILOT_SEED_BASE,
            },
            "original_seed_bases": {
                "null_formal": FROZEN_NULL_SEED_BASE,
                "smoke": SMOKE_SEED_BASE,
                "pilot": PILOT_SEED_BASE,
            },
            "exploratory": True,
            "git_dirty": True,
            "provenance_confirmatory": False,
            "timestamp": timestamp,
            "honest_reporting": (
                "复现脚本不读取原 test label 或结果来调整方法；"
                "原 null_formal_result.json 仅用于事后容差比较。"
                "容差严格按 10% 相对差异，未通过时只报告"
                "「方向性一致但容差失败」。"
            ),
        },
    }

    with open(
        output_dir / "comparison_summary.json", "w", encoding="utf-8",
    ) as f:
        json.dump(
            comparison, f, indent=2, ensure_ascii=False,
            default=_json_default,
        )

    print(f"\n=== 复现汇总 ===")
    print(
        f"总耗时: {overall_elapsed:.1f}s "
        f"({overall_elapsed / 60:.1f}min)"
    )
    if null_verdict is not None:
        print(f"Null formal: {null_verdict}")
    if screening_verdict is not None:
        print(f"候选筛选: {screening_verdict}")
    print(f"\n产物目录: {output_dir}")
    print(f"  - null_formal_reproduction.json")
    print(f"  - candidate_screening_reproduction.json")
    print(f"  - comparison_summary.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
