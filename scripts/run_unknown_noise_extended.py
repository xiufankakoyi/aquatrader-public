"""Task 9: 补齐未知噪声结构矩阵.

在现有 480 场景之外补齐 correlated/dag/near_collinear/variance_shift/distribution_shift.
用全核并行运行，产物写入 data/experiments/p2_automation/unknown_noise_extended_<timestamp>/.

输出:
- unknown_noise_report.csv — 每条件每方法的指标（含新维度列）
- unknown_noise_summary.json — 汇总统计
- scenario_coverage.json — 维度覆盖率
- crossfit_comparison.json — 每维度 base vs cross-fitted 成对比较
- provenance.json / manifest.json — 溯源与清单
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

# 确保项目根目录在 sys.path 中（直接运行脚本时）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from experiments.causal_mechanism_routing.unknown_noise_experiment import (
    ScenarioConfig,
    compute_scenario_coverage,
    print_summary,
    run_unknown_noise_experiment,
    write_unknown_noise_results,
)


def _nan_mean(values: list[float]) -> float:
    """忽略 NaN 的均值，全 NaN 返回 NaN."""
    valid = [v for v in values if not (isinstance(v, float) and math.isnan(v))]
    if not valid:
        return float("nan")
    return float(statistics.fmean(valid))


def _crossfit_comparison(records: list[dict]) -> dict:
    """按维度分组比较 base vs cross-fitted 估计器.

    对每个新维度值，计算 base 估计器与对应 CF 版本的 mean_risk / paired_abs_risk_diff
    成对差异，判定 cross-fitting 是改善/中性/恶化.

    判定规则（基于 mean_paired_abs_risk_diff，负数表示优于 oracle 更多 = 改善）:
    - cf 更优（risk_diff 更负）超过 2% robust baseline → 改善
    - 差异 < 2% → 中性
    - cf 更差 → 恶化

    Args:
        records: 所有场景的指标行.

    Returns:
        每维度的比较结果 dict.
    """
    # base → cf 名称映射
    base_names = [
        "residual_variance", "mad", "huber",
        "trimmed_variance", "shrinkage_residual_variance",
    ]
    pairs = [(b, f"cf_{b}") for b in base_names]

    # 按维度值分组
    dimensions = {
        "feature_structure": ["independent", "correlated", "dag", "near_collinear"],
        "variance_shift": [False, True],
        "distribution_shift": [False, True],
    }

    result: dict = {}
    for dim, values in dimensions.items():
        dim_result: dict = {}
        for val in values:
            # 筛选该维度值的行
            subset = [r for r in records if r[dim] == val]
            if not subset:
                continue
            per_pair: dict = {}
            for base_name, cf_name in pairs:
                base_rows = [r for r in subset if r["estimator"] == base_name]
                cf_rows = [r for r in subset if r["estimator"] == cf_name]
                if not base_rows or not cf_rows:
                    continue
                base_risk = float(statistics.fmean([r["mean_risk"] for r in base_rows]))
                cf_risk = float(statistics.fmean([r["mean_risk"] for r in cf_rows]))
                base_diff = float(statistics.fmean([r["paired_abs_risk_diff_mean"] for r in base_rows]))
                cf_diff = float(statistics.fmean([r["paired_abs_risk_diff_mean"] for r in cf_rows]))
                risk_delta = cf_risk - base_risk  # 正 = CF 更差
                diff_delta = cf_diff - base_diff  # 正 = CF 的 abs_risk_diff 更大 = 更差

                # 判定：以 risk_delta 相对 base_risk 的比例
                rel_change = risk_delta / max(abs(base_risk), 1e-12)
                if rel_change < -0.02:
                    verdict = "improved"
                elif rel_change > 0.02:
                    verdict = "worsened"
                else:
                    verdict = "neutral"

                per_pair[base_name] = {
                    "base_mean_risk": base_risk,
                    "cf_mean_risk": cf_risk,
                    "risk_delta": risk_delta,
                    "rel_change": rel_change,
                    "base_abs_risk_diff": base_diff,
                    "cf_abs_risk_diff": cf_diff,
                    "diff_delta": diff_delta,
                    "verdict": verdict,
                }
            dim_result[str(val)] = per_pair
        result[dim] = dim_result
    return result


def main() -> int:
    """主入口：运行扩展未知噪声实验."""
    parser = argparse.ArgumentParser(
        description="Run extended unknown noise experiment (Task 9)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录（默认 data/experiments/p2_automation/unknown_noise_extended_<timestamp>/）",
    )
    parser.add_argument("--n-envs", type=int, default=100, help="每条件环境数")
    parser.add_argument(
        "--n-jobs",
        type=int,
        default=1,
        help="并行线程数，默认 1（单 worker，自动队列模式下低占用）",
    )
    parser.add_argument("--lam", type=float, default=10.0, help="ridge 正则强度")
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or (
        Path("data/experiments/p2_automation")
        / f"unknown_noise_extended_{timestamp}"
    )

    config = ScenarioConfig(n_envs=args.n_envs, lam=args.lam)
    print(
        f"[task9] running extended experiment: {config.n_envs} envs/condition, "
        f"lam={config.lam}, n_jobs={args.n_jobs}"
    )
    print(f"[task9] output_dir={output_dir}")

    result = run_unknown_noise_experiment(
        config=config,
        progress=True,
        n_jobs=args.n_jobs,
        extended=True,
    )

    # 写标准产物（含 scenario_coverage.json）
    write_unknown_noise_results(output_dir, result, confirmatory=False)

    # 写 cross-fitting 比较产物
    cf_comparison = _crossfit_comparison(result["records"])
    (output_dir / "crossfit_comparison.json").write_text(
        json.dumps(cf_comparison, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 打印摘要
    print_summary(result)

    # 打印每维度 cross-fitting 关键发现
    print("\n" + "=" * 90)
    print("Cross-fitting 对比（按新维度分组）")
    print("=" * 90)
    for dim, dim_data in cf_comparison.items():
        print(f"\n--- {dim} ---")
        for val, pairs in dim_data.items():
            verdicts = [p["verdict"] for p in pairs.values()]
            n_improved = sum(1 for v in verdicts if v == "improved")
            n_neutral = sum(1 for v in verdicts if v == "neutral")
            n_worsened = sum(1 for v in verdicts if v == "worsened")
            print(
                f"  {val}: improved={n_improved}, neutral={n_neutral}, "
                f"worsened={n_worsened}"
            )
            for base_name, info in pairs.items():
                print(
                    f"    {base_name}: rel_change={info['rel_change']:+.4f} "
                    f"-> {info['verdict']}"
                )

    print("\n" + "=" * 90)
    print(json.dumps({
        "output_dir": str(output_dir),
        "n_scenarios": result["n_scenarios"],
        "n_estimators": result["n_estimators"],
        "n_records": len(result["records"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
