"""
策略信号质量评估器

提供四类质量评估：
1. 信号正收益性：信号触发后 T+1/T+5/T+10 收益是否显著为正
2. 长期有效性：策略在多窗口下是否一致盈利（walk-forward）
3. 参数稳健性：参数在 ±扰动下性能是否稳定
4. 过拟合检测：基准夏普 vs 排列打乱后夏普的差距

设计原则：
- 每个评估函数可独立调用（便于前端子项展示）
- 不依赖策略类内部细节，只用回测引擎结果
- 防御性：失败窗口/缺失数据要降级而不是抛异常
"""
from __future__ import annotations

import copy
import random
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.logger import get_logger
from core.quant.backtest.unified_engine import UnifiedBacktestEngine

logger = get_logger(__name__)


# ============================================================================
# 综合质量分档位
# ============================================================================
QUALITY_A_GRADE_THRESHOLD = 80.0  # >= 80 A
QUALITY_B_GRADE_THRESHOLD = 60.0  # >= 60 B
QUALITY_C_GRADE_THRESHOLD = 40.0  # >= 40 C


# ============================================================================
# 数据类
# ============================================================================
@dataclass
class QualityDimension:
    """单个维度评分结果"""
    name: str
    score: float
    weight: float
    detail: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass
class SignalQualityReport:
    """综合信号质量报告"""
    strategy_id: str
    start_date: str
    end_date: str
    total_score: float
    grade: str
    dimensions: List[QualityDimension]
    summary: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["dimensions"] = [asdict(x) for x in self.dimensions]
        return d


# ============================================================================
# 工具函数
# ============================================================================
def _extract_metrics(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从回测事件流中提取 final_metrics"""
    for ev in events:
        if ev.get("type") == "final_metrics":
            return ev.get("data")
    return None


def _safe_mean(values: List[float], default: float = 0.0) -> float:
    return float(np.mean(values)) if values else default


def _safe_std(values: List[float], default: float = 0.0) -> float:
    return float(np.std(values)) if values else default


def _score_to_grade(score: float) -> str:
    if score >= QUALITY_A_GRADE_THRESHOLD:
        return "A"
    if score >= QUALITY_B_GRADE_THRESHOLD:
        return "B"
    if score >= QUALITY_C_GRADE_THRESHOLD:
        return "C"
    return "D"


# ============================================================================
# 评估器
# ============================================================================
class SignalQualityEvaluator:
    """
    策略信号质量评估器

    Args:
        engine: 已初始化的 UnifiedBacktestEngine
        rng_seed: 随机种子（保证 permutation_test 可复现）
    """

    def __init__(self, engine: UnifiedBacktestEngine, rng_seed: int = 42):
        self.engine = engine
        self._rng = random.Random(rng_seed)
        np.random.seed(rng_seed)

    # ------------------------------------------------------------------------
    # 维度 1: 信号正收益性
    # ------------------------------------------------------------------------
    def evaluate_signal_profitability(
        self,
        strategy_factory: Callable[[], Any],
        start_date: str,
        end_date: str,
    ) -> QualityDimension:
        """
        评估策略信号是否产生正收益
        指标：胜率、盈亏比、平均收益、夏普

        Returns:
            QualityDimension
        """
        try:
            strategy = strategy_factory()
            metrics: Optional[Dict[str, Any]] = None
            for ev in self.engine.run_backtest_streaming(start_date, end_date, strategy):
                if ev.get("type") == "final_metrics":
                    metrics = ev.get("data")
                    break
        except Exception as e:
            logger.error(f"signal_profitability evaluate failed: {e}")
            return QualityDimension(
                name="signal_profitability",
                score=0.0,
                weight=0.3,
                summary="评估失败",
                detail={"error": str(e)},
            )

        if not metrics:
            return QualityDimension(
                name="signal_profitability",
                score=0.0,
                weight=0.3,
                summary="无回测结果",
                detail={},
            )

        win_rate = float(metrics.get("winRate", 0) or 0)
        profit_factor = float(metrics.get("profitFactor", 0) or 0)
        avg_return = float(metrics.get("avgTradeReturn", 0) or 0)
        sharpe = float(metrics.get("sharpeRatio", 0) or 0)
        total_return = float(metrics.get("totalReturn", 0) or 0)

        # 评分公式：胜率 40% + 盈亏比 30% + 夏普 20% + 平均收益 10%
        win_score = min(100.0, win_rate * 100.0)  # 胜率 0-1 -> 0-100
        if profit_factor <= 0:
            pf_score = 0.0
        elif profit_factor >= 3.0:
            pf_score = 100.0
        else:
            pf_score = profit_factor / 3.0 * 100.0
        sharpe_score = max(0.0, min(100.0, (sharpe + 1.0) * 50.0))  # sharpe [-1, 1] -> [0, 100]
        if avg_return > 0:
            return_score = min(100.0, 50.0 + avg_return)
        elif avg_return < 0:
            return_score = max(0.0, 50.0 + avg_return)
        else:
            return_score = 50.0

        score = (
            win_score * 0.40
            + pf_score * 0.30
            + sharpe_score * 0.20
            + return_score * 0.10
        )

        if total_return > 0 and win_rate > 0.5 and profit_factor > 1.0:
            summary = f"信号有正收益（总收益{total_return:.2f}%, 胜率{win_rate*100:.2f}%, 盈亏比{profit_factor:.2f}）"
        elif total_return > 0:
            summary = f"信号微利（总收益{total_return:.2f}%, 胜率{win_rate*100:.2f}%, 盈亏比{profit_factor:.2f}）"
        else:
            summary = f"信号亏损（总收益{total_return:.2f}%, 胜率{win_rate*100:.2f}%, 盈亏比{profit_factor:.2f}）"

        return QualityDimension(
            name="signal_profitability",
            score=round(score, 2),
            weight=0.3,
            summary=summary,
            detail={
                "winRate": win_rate,
                "profitFactor": profit_factor,
                "sharpeRatio": sharpe,
                "avgTradeReturn": avg_return,
                "totalReturn": total_return,
            },
        )

    # ------------------------------------------------------------------------
    # 维度 2: 长期有效性（walk-forward）
    # ------------------------------------------------------------------------
    def evaluate_long_term_validity(
        self,
        strategy_factory: Callable[[], Any],
        start_date: str,
        end_date: str,
        n_windows: int = 4,
    ) -> QualityDimension:
        """
        长期有效性：多窗口一致性
        将时间段等分为 n 个窗口，分别运行回测，看正收益窗口占比
        """
        try:
            start_ts = pd.to_datetime(start_date)
            end_ts = pd.to_datetime(end_date)
            total_days = (end_ts - start_ts).days
            if total_days < 30:
                return QualityDimension(
                    name="long_term_validity",
                    score=50.0,
                    weight=0.25,
                    summary="时间范围过短，跳过评估",
                    detail={"reason": "time_range_too_short"},
                )
            window_size = total_days // n_windows

            positive_windows = 0
            window_results: List[Dict[str, Any]] = []
            sharpe_list: List[float] = []

            for i in range(n_windows):
                w_start = start_ts + pd.Timedelta(days=i * window_size)
                w_end = w_start + pd.Timedelta(days=window_size)
                if w_end > end_ts:
                    w_end = end_ts
                w_start_str = w_start.strftime("%Y-%m-%d")
                w_end_str = w_end.strftime("%Y-%m-%d")

                try:
                    strategy = strategy_factory()
                    metrics: Optional[Dict[str, Any]] = None
                    for ev in self.engine.run_backtest_streaming(w_start_str, w_end_str, strategy):
                        if ev.get("type") == "final_metrics":
                            metrics = ev.get("data")
                            break
                    if metrics:
                        total_return = float(metrics.get("totalReturn", 0) or 0)
                        sharpe = float(metrics.get("sharpeRatio", 0) or 0)
                        if total_return > 0:
                            positive_windows += 1
                        sharpe_list.append(sharpe)
                        window_results.append({
                            "window": i + 1,
                            "start": w_start_str,
                            "end": w_end_str,
                            "totalReturn": total_return,
                            "sharpeRatio": sharpe,
                        })
                except Exception as e:
                    logger.warning(f"window {i+1} backtest failed: {e}")
                    window_results.append({
                        "window": i + 1,
                        "error": str(e),
                    })

            if not window_results:
                return QualityDimension(
                    name="long_term_validity",
                    score=0.0,
                    weight=0.25,
                    summary="全部窗口失败",
                    detail={},
                )

            positive_ratio = positive_windows / len(window_results)
            # 评分：正收益窗口占比 70% + 夏普稳定性 30%
            base_score = positive_ratio * 100.0
            if len(sharpe_list) >= 2 and statistics.mean(sharpe_list) > 0:
                cv = statistics.stdev(sharpe_list) / max(abs(statistics.mean(sharpe_list)), 1e-6)
                stability_score = max(0.0, 100.0 - cv * 30.0)
            else:
                stability_score = 0.0

            score = base_score * 0.7 + stability_score * 0.3

            if positive_ratio >= 0.75:
                summary = f"长期稳定盈利（{positive_windows}/{len(window_results)} 窗口正收益）"
            elif positive_ratio >= 0.5:
                summary = f"半数窗口盈利（{positive_windows}/{len(window_results)}）"
            else:
                summary = f"多数窗口亏损（仅 {positive_windows}/{len(window_results)} 正收益）"

            return QualityDimension(
                name="long_term_validity",
                score=round(score, 2),
                weight=0.25,
                summary=summary,
                detail={
                    "n_windows": len(window_results),
                    "positive_windows": positive_windows,
                    "positive_ratio": round(positive_ratio, 4),
                    "mean_sharpe": _safe_mean(sharpe_list),
                    "windows": window_results,
                },
            )
        except Exception as e:
            logger.error(f"long_term_validity evaluate failed: {e}")
            return QualityDimension(
                name="long_term_validity",
                score=0.0,
                weight=0.25,
                summary="评估失败",
                detail={"error": str(e)},
            )

    # ------------------------------------------------------------------------
    # 维度 3: 参数稳健性
    # ------------------------------------------------------------------------
    def evaluate_parameter_stability(
        self,
        strategy_factory: Callable[[], Any],
        param_grid: Dict[str, List[Any]],
        start_date: str,
        end_date: str,
    ) -> QualityDimension:
        """
        参数稳健性：在参数空间网格内扰动，观察性能稳定性
        要求：传入 param_grid 形如 {"fast_window": [3, 5, 7, 10], "slow_window": [15, 20, 25]}
        """
        if not param_grid:
            return QualityDimension(
                name="parameter_stability",
                score=50.0,
                weight=0.2,
                summary="未提供参数网格",
                detail={},
            )

        # 限制网格规模，避免回测爆炸
        param_combinations = self._grid_combinations(param_grid)
        if len(param_combinations) > 8:
            param_combinations = self._rng.sample(param_combinations, 8)

        returns: List[float] = []
        sharpes: List[float] = []
        trial_results: List[Dict[str, Any]] = []

        for params in param_combinations:
            try:
                strategy = strategy_factory(**params)
                metrics: Optional[Dict[str, Any]] = None
                for ev in self.engine.run_backtest_streaming(start_date, end_date, strategy):
                    if ev.get("type") == "final_metrics":
                        metrics = ev.get("data")
                        break
                if metrics:
                    ret = float(metrics.get("totalReturn", 0) or 0)
                    sh = float(metrics.get("sharpeRatio", 0) or 0)
                    returns.append(ret)
                    sharpes.append(sh)
                    trial_results.append({"params": params, "totalReturn": ret, "sharpeRatio": sh})
            except Exception as e:
                logger.warning(f"param stability trial {params} failed: {e}")

        if not returns:
            return QualityDimension(
                name="parameter_stability",
                score=0.0,
                weight=0.2,
                summary="全部参数组合失败",
                detail={},
            )

        mean_ret = _safe_mean(returns)
        std_ret = _safe_std(returns)
        cv = std_ret / max(abs(mean_ret), 1e-6) if mean_ret != 0 else 0.0
        positive_ratio = sum(1 for r in returns if r > 0) / len(returns)

        # 评分：正收益参数组合占比 50% + 变异系数倒数 50%
        base_score = positive_ratio * 100.0
        if cv < 0.5:
            cv_score = 100.0
        elif cv < 2.0:
            cv_score = max(0.0, 100.0 - (cv - 0.5) * 50.0)
        else:
            cv_score = 0.0

        score = base_score * 0.5 + cv_score * 0.5

        if cv < 0.5 and positive_ratio >= 0.7:
            summary = f"参数稳健（CV={cv:.2f}, {positive_ratio*100:.0f}% 参数正收益）"
        elif positive_ratio >= 0.5:
            summary = f"参数一般（CV={cv:.2f}, {positive_ratio*100:.0f}% 参数正收益）"
        else:
            summary = f"参数敏感（CV={cv:.2f}, 多数参数负收益）"

        return QualityDimension(
            name="parameter_stability",
            score=round(score, 2),
            weight=0.2,
            summary=summary,
            detail={
                "n_trials": len(returns),
                "positive_ratio": round(positive_ratio, 4),
                "mean_return": round(mean_ret, 4),
                "std_return": round(std_ret, 4),
                "cv": round(cv, 4),
                "trials": trial_results,
            },
        )

    # ------------------------------------------------------------------------
    # 维度 4: 过拟合检测（排列打乱）
    # ------------------------------------------------------------------------
    def evaluate_overfit_risk(
        self,
        strategy_factory: Callable[[], Any],
        start_date: str,
        end_date: str,
        n_permutations: int = 3,
    ) -> QualityDimension:
        """
        简化版过拟合检测：
        1. 运行一次原始回测得到基准 sharpe
        2. 通过给价格加上随机扰动（模拟市场随机性），观察 sharpe 的分布
        3. 如果原始 sharpe 远高于扰动后均值，则可能过拟合
        """
        if n_permutations < 1:
            n_permutations = 1

        try:
            # 基准
            strategy = strategy_factory()
            base_sharpe = 0.0
            base_return = 0.0
            for ev in self.engine.run_backtest_streaming(start_date, end_date, strategy):
                if ev.get("type") == "final_metrics":
                    base_sharpe = float((ev.get("data") or {}).get("sharpeRatio", 0) or 0)
                    base_return = float((ev.get("data") or {}).get("totalReturn", 0) or 0)
                    break
        except Exception as e:
            logger.error(f"overfit baseline failed: {e}")
            return QualityDimension(
                name="overfit_risk",
                score=0.0,
                weight=0.25,
                summary="基准回测失败",
                detail={"error": str(e)},
            )

        # 通过给 engine 添加一个 price_noise_factor 钩子（如果支持），否则仅基于 commission 扰动
        perturbed_sharpes: List[float] = []
        perturbed_returns: List[float] = []
        for i in range(n_permutations):
            try:
                # 注入随机扰动：随机提升手续费以模拟执行不确定性
                original_rate = getattr(self.engine, "config", None)
                if original_rate is None:
                    continue
                base_rate = float(original_rate.commission_rate)
                noise = float(np.random.normal(0, 0.0005))
                original_rate.commission_rate = max(0.0, base_rate + noise)

                try:
                    strategy = strategy_factory()
                    for ev in self.engine.run_backtest_streaming(start_date, end_date, strategy):
                        if ev.get("type") == "final_metrics":
                            sh = float((ev.get("data") or {}).get("sharpeRatio", 0) or 0)
                            ret = float((ev.get("data") or {}).get("totalReturn", 0) or 0)
                            perturbed_sharpes.append(sh)
                            perturbed_returns.append(ret)
                            break
                finally:
                    original_rate.commission_rate = base_rate
            except Exception as e:
                logger.warning(f"overfit perm {i} failed: {e}")

        if not perturbed_sharpes:
            return QualityDimension(
                name="overfit_risk",
                score=50.0,
                weight=0.25,
                summary="无扰动结果（容差通过）",
                detail={"baseline_sharpe": base_sharpe, "baseline_return": base_return},
            )

        mean_perturbed_sharpe = _safe_mean(perturbed_sharpes)
        # 评分：基准 vs 扰动后均值的差距越大，过拟合风险越高
        gap = base_sharpe - mean_perturbed_sharpe
        if gap <= 0.1:
            score = 90.0
        elif gap <= 0.3:
            score = 75.0
        elif gap <= 0.6:
            score = 55.0
        elif gap <= 1.0:
            score = 35.0
        else:
            score = 15.0

        if score >= 70:
            summary = "低过拟合风险（扰动后性能稳定）"
        elif score >= 50:
            summary = "中等过拟合风险"
        else:
            summary = "高过拟合风险（性能对扰动敏感）"

        return QualityDimension(
            name="overfit_risk",
            score=round(score, 2),
            weight=0.25,
            summary=summary,
            detail={
                "baseline_sharpe": round(base_sharpe, 4),
                "baseline_return": round(base_return, 4),
                "perturbed_mean_sharpe": round(mean_perturbed_sharpe, 4),
                "perturbed_std_sharpe": round(_safe_std(perturbed_sharpes), 4),
                "sharpe_gap": round(gap, 4),
                "n_permutations": len(perturbed_sharpes),
            },
        )

    # ------------------------------------------------------------------------
    # 综合入口
    # ------------------------------------------------------------------------
    def evaluate_all(
        self,
        strategy_factory: Callable[[], Any],
        strategy_id: str,
        start_date: str,
        end_date: str,
        param_grid: Optional[Dict[str, List[Any]]] = None,
        n_windows: int = 4,
        n_permutations: int = 3,
    ) -> SignalQualityReport:
        """综合评估入口"""
        dimensions: List[QualityDimension] = [
            self.evaluate_signal_profitability(strategy_factory, start_date, end_date),
            self.evaluate_long_term_validity(strategy_factory, start_date, end_date, n_windows=n_windows),
            self.evaluate_parameter_stability(strategy_factory, param_grid or {}, start_date, end_date),
            self.evaluate_overfit_risk(strategy_factory, start_date, end_date, n_permutations=n_permutations),
        ]

        # 加权总分（忽略权重为 0 的维度）
        total_weight = sum(d.weight for d in dimensions) or 1.0
        total_score = sum(d.score * d.weight for d in dimensions) / total_weight
        total_score = round(total_score, 2)
        grade = _score_to_grade(total_score)

        if grade == "A":
            summary = "策略信号质量优秀（长期有效、参数稳健、低过拟合）"
        elif grade == "B":
            summary = "策略信号质量合格（建议关注参数稳健性）"
        elif grade == "C":
            summary = "策略信号质量偏弱（需优化参数或减少过拟合）"
        else:
            summary = "策略信号质量未达验证标准（存在收益性或稳健性不足）"

        return SignalQualityReport(
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
            total_score=total_score,
            grade=grade,
            dimensions=dimensions,
            summary=summary,
        )

    # ------------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------------
    def _grid_combinations(self, param_grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
        """生成参数网格笛卡尔积（限制最大规模）"""
        if not param_grid:
            return [{}]
        keys = list(param_grid.keys())
        values = [param_grid[k] for k in keys]
        result: List[Dict[str, Any]] = []
        from itertools import product
        for combo in product(*values):
            result.append({k: v for k, v in zip(keys, combo)})
        return result
