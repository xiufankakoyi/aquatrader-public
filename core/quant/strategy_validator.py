"""
策略验证框架

包含过拟合检测、样本内外测试、滚动窗口步进测试、蒙特卡洛压力测试等。

核心目标：
- 排列测试（Permutation Test）：检验收益是否来自真实的预测能力，而非随机噪声。
- Walk-Forward Analysis：在多个独立时段上验证策略稳定性，减少全样本过拟合。
- 蒙特卡洛测试（Monte Carlo）：多维扰动（手续费、滑点、信号质量）后评估策略稳健性。
"""

import itertools
from datetime import timedelta
from typing import Any, Dict, List, Optional, Type, Union

import numpy as np
import pandas as pd

from config.logger import get_logger
from core.quant.backtest.unified_engine import UnifiedBacktestEngine

logger = get_logger(__name__)


class _SignalPerturbationStrategy:
    """
    信号扰动包装器

    在不修改原策略的前提下，对 generate_signals 输出进行随机扰动：
    - dropout_rate: 随机丢弃部分买入信号
    - flip_rate: 随机翻转小部分买入/卖出信号
    """

    def __init__(
        self,
        wrapped: Any,
        dropout_rate: float = 0.0,
        flip_rate: float = 0.0,
        seed: Optional[int] = None,
    ):
        self._wrapped = wrapped
        self._dropout_rate = dropout_rate
        self._flip_rate = flip_rate
        self._rng = np.random.default_rng(seed)

        # 透传常用属性，避免策略元信息丢失
        for attr in ["name", "is_vectorized", "required_factors", "params"]:
            if hasattr(wrapped, attr):
                setattr(self, attr, getattr(wrapped, attr))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def generate_signals(self, *args, **kwargs) -> Dict[str, Any]:
        signals = self._wrapped.generate_signals(*args, **kwargs)
        return self._perturb(signals)

    def generate_signals_vectorized(self, *args, **kwargs) -> Any:
        signal_matrix = self._wrapped.generate_signals_vectorized(*args, **kwargs)
        return self._perturb_vectorized(signal_matrix)

    def _perturb(self, signals: Dict[str, Any]) -> Dict[str, Any]:
        if not signals:
            return signals

        result = {}
        for code, sig in signals.items():
            action = sig.get("action") if isinstance(sig, dict) else sig

            if action == "buy" and self._rng.random() < self._dropout_rate:
                continue

            if self._rng.random() < self._flip_rate:
                action = self._flip_action(action)

            if isinstance(sig, dict):
                result[code] = dict(sig, action=action)
            else:
                result[code] = action

        return result

    def _perturb_vectorized(self, signal_matrix: np.ndarray) -> np.ndarray:
        if signal_matrix is None or signal_matrix.size == 0:
            return signal_matrix

        result = signal_matrix.copy()
        buy_mask = result == 1
        sell_mask = (result == -1) | (result == 2)

        if self._dropout_rate > 0:
            drop_mask = buy_mask & (self._rng.random(result.shape) < self._dropout_rate)
            result[drop_mask] = 0

        if self._flip_rate > 0:
            flip_mask = self._rng.random(result.shape) < self._flip_rate
            buy_flip = flip_mask & buy_mask
            sell_flip = flip_mask & sell_mask
            result[buy_flip] = -1
            result[sell_flip] = 1

        return result

    @staticmethod
    def _flip_action(action: str) -> str:
        if action == "buy":
            return "sell"
        if action == "sell":
            return "buy"
        return action


class OverfittingDetector:
    """
    过拟合检测与稳健性测试套件
    """

    def __init__(self, engine: UnifiedBacktestEngine):
        self.engine = engine

    def _run_backtest(
        self,
        strategy_class: Type,
        params: Dict[str, Any],
        start_date: str,
        end_date: str,
    ) -> Optional[Dict[str, Any]]:
        """运行一次回测并返回 final_metrics。"""
        try:
            strategy = strategy_class(**params)
            stream = self.engine.run_backtest_streaming(start_date, end_date, strategy)
            for event in stream:
                if event.get("type") == "final_metrics":
                    return event.get("data")
        except Exception as e:
            logger.error(f"回测 {start_date} ~ {end_date} 失败: {e}")
        return None

    def permutation_test(
        self,
        strategy_class: Type,
        params: Dict[str, Any],
        start_date: str,
        end_date: str,
        n_permutations: int = 100,
        metric: str = "sharpe_ratio",
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        排列测试（Permutation Test）

        通过打乱权益曲线的日收益率序列，生成 N 条“随机能力”的权益曲线，
        计算原始策略指标在随机分布中的分位数（p-value）。

        Args:
            strategy_class: 策略类
            params: 策略参数
            start_date: 开始日期
            end_date: 结束日期
            n_permutations: 排列次数
            metric: 用于比较的指标，默认 sharpe_ratio
            seed: 随机种子

        Returns:
            Dict 包含原始指标、排列分布统计、p-value 等
        """
        logger.info(f"开始排列测试 (n={n_permutations}, metric={metric})...")

        base_metrics = self._run_backtest(strategy_class, params, start_date, end_date)
        if base_metrics is None:
            return {"status": "error", "message": "基准回测失败"}

        base_value = float(base_metrics.get(metric, 0.0))
        equity_curve = base_metrics.get("equity_curve", [])
        if len(equity_curve) < 2:
            return {"status": "error", "message": "权益曲线过短，无法计算排列"}

        equity_values = np.array([pt["equity"] for pt in equity_curve], dtype=np.float64)
        daily_returns = np.diff(equity_values) / equity_values[:-1]
        daily_returns = daily_returns[np.isfinite(daily_returns)]

        if len(daily_returns) < 2:
            return {"status": "error", "message": "日收益率不足"}

        rng = np.random.default_rng(seed)
        permuted_metrics = []

        for i in range(n_permutations):
            shuffled_returns = rng.permutation(daily_returns)
            perm_equity = [equity_values[0]]
            for r in shuffled_returns:
                perm_equity.append(perm_equity[-1] * (1 + r))

            perm_sharpe = self._sharpe_from_equity(perm_equity)
            # 将 sharpe 映射回请求指标；其他指标用等价物近似
            perm_value = perm_sharpe if metric in ("sharpe_ratio", "sortino_ratio") else self._total_return_from_equity(perm_equity)
            permuted_metrics.append(perm_value)

        permuted_metrics = np.array(permuted_metrics, dtype=np.float64)
        p_value = float(np.mean(permuted_metrics >= base_value))
        percentile = float(np.mean(permuted_metrics <= base_value) * 100)

        return {
            "status": "ok",
            "metric": metric,
            "base_value": base_value,
            "p_value": p_value,
            "percentile": percentile,
            "n_permutations": n_permutations,
            "permuted_mean": float(np.mean(permuted_metrics)),
            "permuted_std": float(np.std(permuted_metrics)),
            "permuted_min": float(np.min(permuted_metrics)),
            "permuted_max": float(np.max(permuted_metrics)),
            "permuted_metrics": permuted_metrics.tolist(),
            "interpretation": self._interpret_permutation(p_value, metric),
        }

    def walk_forward_test(
        self,
        strategy_class: Type,
        params: Dict[str, Any],
        start_date: str,
        end_date: str,
        n_windows: int = 5,
        train_ratio: float = 0.7,
        param_grid: Optional[Dict[str, List[Any]]] = None,
        optimize_metric: str = "sharpe_ratio",
    ) -> List[Dict[str, Any]]:
        """
        滚动窗口步进测试（Walk-Forward Analysis）

        将总区间划分为 n_windows 个窗口；每个窗口内再按 train_ratio 切分为
        训练期（In-Sample）与测试期（Out-of-Sample）。

        若提供 param_grid，则在训练期做网格搜索选择最优参数，再在测试期验证；
        否则使用传入 params，仅评估同一参数在不同时段的稳定性。

        Args:
            strategy_class: 策略类
            params: 默认策略参数
            start_date: 总开始日期
            end_date: 总结束日期
            n_windows: 窗口数量
            train_ratio: 每个窗口中训练期占比
            param_grid: 可选的参数网格，用于训练期优化
            optimize_metric: 优化目标指标

        Returns:
            每个窗口的训练/测试结果列表
        """
        logger.info(f"开始 Walk-Forward 测试 (n_windows={n_windows}, train_ratio={train_ratio})...")

        start_ts = pd.to_datetime(start_date)
        end_ts = pd.to_datetime(end_date)
        total_days = max((end_ts - start_ts).days, 1)
        window_size = max(total_days // n_windows, 1)

        results = []

        for i in range(n_windows):
            w_start = start_ts + timedelta(days=i * window_size)
            w_end = min(start_ts + timedelta(days=(i + 1) * window_size), end_ts)
            if w_start >= w_end:
                continue

            split_point = w_start + timedelta(days=int((w_end - w_start).days * train_ratio))
            split_point = min(split_point, w_end)

            train_start = w_start.strftime("%Y-%m-%d")
            train_end = split_point.strftime("%Y-%m-%d")
            test_start = split_point.strftime("%Y-%m-%d")
            test_end = w_end.strftime("%Y-%m-%d")

            logger.info(f"窗口 {i+1}/{n_windows}: 训练 {train_start}~{train_end}, 测试 {test_start}~{test_end}")

            window_result = {
                "window": i + 1,
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            }

            try:
                if param_grid:
                    best_params = self._grid_search(
                        strategy_class, params, param_grid,
                        train_start, train_end, optimize_metric
                    )
                    window_result["best_params"] = best_params
                else:
                    best_params = params

                train_metrics = self._run_backtest(strategy_class, best_params, train_start, train_end)
                test_metrics = self._run_backtest(strategy_class, best_params, test_start, test_end)

                window_result["train_metrics"] = train_metrics
                window_result["test_metrics"] = test_metrics
                window_result["degradation"] = self._compute_degradation(
                    train_metrics, test_metrics, optimize_metric
                )
            except Exception as e:
                logger.error(f"窗口 {i+1} 失败: {e}")
                window_result["error"] = str(e)

            results.append(window_result)

        return results

    def monte_carlo_test(
        self,
        strategy_class: Type,
        params: Dict[str, Any],
        start_date: str,
        end_date: str,
        n_simulations: int = 50,
        commission_noise_std: float = 0.0003,
        slippage_noise_std: float = 0.0005,
        signal_dropout_rate: float = 0.05,
        signal_flip_rate: float = 0.02,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        多维蒙特卡洛压力测试

        同时对以下维度注入随机扰动：
        1. 手续费率（commission_rate）
        2. 滑点率（slippage_rate）
        3. 信号_dropout（随机丢弃买入信号）
        4. 信号翻转（随机翻转少量买卖信号）

        Args:
            strategy_class: 策略类
            params: 策略参数
            start_date: 开始日期
            end_date: 结束日期
            n_simulations: 模拟次数
            commission_noise_std: 手续费噪声标准差
            slippage_noise_std: 滑点噪声标准差
            signal_dropout_rate: 买入信号丢弃概率
            signal_flip_rate: 信号翻转概率
            seed: 随机种子

        Returns:
            Dict 包含各次模拟的指标分布与统计
        """
        logger.info(f"开始多维蒙特卡洛测试 (n={n_simulations})...")

        base_metrics = self._run_backtest(strategy_class, params, start_date, end_date)
        if base_metrics is None:
            return {"status": "error", "message": "基准回测失败"}

        rng = np.random.default_rng(seed)
        original_commission = self.engine.config.commission_rate
        original_slippage = self.engine.config.slippage_rate

        sim_results = []

        for i in range(n_simulations):
            commission_noise = rng.normal(0, commission_noise_std)
            slippage_noise = rng.normal(0, slippage_noise_std)

            self.engine.config.commission_rate = max(0.0, original_commission + commission_noise)
            self.engine.config.slippage_rate = max(0.0, original_slippage + slippage_noise)

            try:
                strategy = strategy_class(**params)
                perturbed_strategy = _SignalPerturbationStrategy(
                    strategy,
                    dropout_rate=signal_dropout_rate,
                    flip_rate=signal_flip_rate,
                    seed=int(rng.integers(0, 2**31)),
                )

                stream = self.engine.run_backtest_streaming(
                    start_date, end_date, perturbed_strategy
                )
                metrics = None
                for event in stream:
                    if event.get("type") == "final_metrics":
                        metrics = event.get("data")
                        break

                if metrics:
                    sim_results.append({
                        "sharpe_ratio": float(metrics.get("sharpe_ratio", 0.0)),
                        "total_return": float(metrics.get("total_return", 0.0)),
                        "max_drawdown": float(metrics.get("max_drawdown", 0.0)),
                        "commission_rate": float(self.engine.config.commission_rate),
                        "slippage_rate": float(self.engine.config.slippage_rate),
                    })
            except Exception as e:
                logger.error(f"Monte Carlo 第 {i+1} 次模拟失败: {e}")
            finally:
                self.engine.config.commission_rate = original_commission
                self.engine.config.slippage_rate = original_slippage

        sharpe_values = [r["sharpe_ratio"] for r in sim_results]

        return {
            "status": "ok",
            "n_simulations": n_simulations,
            "base_sharpe": float(base_metrics.get("sharpe_ratio", 0.0)),
            "base_total_return": float(base_metrics.get("total_return", 0.0)),
            "mean_sharpe": float(np.mean(sharpe_values)) if sharpe_values else 0.0,
            "std_sharpe": float(np.std(sharpe_values)) if sharpe_values else 0.0,
            "min_sharpe": float(np.min(sharpe_values)) if sharpe_values else 0.0,
            "max_sharpe": float(np.max(sharpe_values)) if sharpe_values else 0.0,
            "percentile_5": float(np.percentile(sharpe_values, 5)) if sharpe_values else 0.0,
            "percentile_95": float(np.percentile(sharpe_values, 95)) if sharpe_values else 0.0,
            "simulations": sim_results,
        }

    def _grid_search(
        self,
        strategy_class: Type,
        base_params: Dict[str, Any],
        param_grid: Dict[str, List[Any]],
        start_date: str,
        end_date: str,
        optimize_metric: str,
    ) -> Dict[str, Any]:
        """在训练期上执行网格搜索，返回最优参数组合。"""
        keys = list(param_grid.keys())
        values = [param_grid[k] for k in keys]

        best_value = -np.inf
        best_params = base_params

        for combo in itertools.product(*values):
            candidate = dict(base_params)
            candidate.update(dict(zip(keys, combo)))

            metrics = self._run_backtest(strategy_class, candidate, start_date, end_date)
            if metrics is None:
                continue

            value = float(metrics.get(optimize_metric, 0.0))
            if value > best_value:
                best_value = value
                best_params = candidate

        logger.info(f"训练期最优参数: {best_params}, {optimize_metric}={best_value:.4f}")
        return best_params

    @staticmethod
    def _compute_degradation(
        train_metrics: Optional[Dict[str, Any]],
        test_metrics: Optional[Dict[str, Any]],
        metric: str,
    ) -> Optional[float]:
        """计算测试期相对训练期的性能衰减。"""
        if train_metrics is None or test_metrics is None:
            return None

        train_value = float(train_metrics.get(metric, 0.0))
        test_value = float(test_metrics.get(metric, 0.0))

        if train_value == 0:
            return None

        return float((test_value - train_value) / abs(train_value))

    @staticmethod
    def _sharpe_from_equity(equity: List[float]) -> float:
        """从权益曲线计算简化年化夏普。"""
        values = np.array(equity, dtype=np.float64)
        if len(values) < 2:
            return 0.0
        returns = np.diff(values) / values[:-1]
        returns = returns[np.isfinite(returns)]
        if len(returns) < 2 or np.std(returns) == 0:
            return 0.0
        return float(np.mean(returns) / np.std(returns) * np.sqrt(252))

    @staticmethod
    def _total_return_from_equity(equity: List[float]) -> float:
        """从权益曲线计算总收益。"""
        if len(equity) < 2 or equity[0] == 0:
            return 0.0
        return float((equity[-1] - equity[0]) / equity[0] * 100)

    @staticmethod
    def _interpret_permutation(p_value: float, metric: str) -> str:
        """对排列测试结果给出定性解释。"""
        if p_value <= 0.05:
            return f"p={p_value:.3f}，在 5% 水平显著，策略 {metric} 不太可能来自随机噪声。"
        if p_value <= 0.1:
            return f"p={p_value:.3f}，在 10% 水平边缘显著，需谨慎对待。"
        return f"p={p_value:.3f}，不显著，策略 {metric} 可能受随机性驱动。"
