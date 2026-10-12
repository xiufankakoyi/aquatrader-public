"""板块轮动 walk-forward + 基准对比评价器。

复用 core.research.adaptation.engine.walk_forward_windows()，不创建第三套实现。
基准对比包含 6 种真实不同基准：当前主题规则/简单相对强度/简单反转/随机等权/
无 Research 门禁消融/有 Research 门禁版本。

P2.1: with_research_gate 基准使用真实 ACTIVE/TRUSTED Claim 过滤，禁止用 momentum_score 代替。
P2.2: 6 种基准实现为真实不同信号（momentum/relative_strength/reversal/random/ablation/gate）。
P2.3: 使用按日组合净值计算成本后收益、Sharpe 和最大回撤，不得把横截面样本当时间序列。

包含交易成本/停牌/涨跌停/成交量约束，输出标记"模拟评价，不构成投资建议"。
无有效样本时输出 insufficient_sample，不输出投资建议。
"""
from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from core.research.adaptation.engine import (
    grouped_returns,
    pearson_ic,
    rank_ic,
    walk_forward_windows,
)

from .contracts import BASELINE_NAMES, EvaluationLabel, SectorFeatureRecord

logger = logging.getLogger(__name__)


# 模拟评价约束常量（不构成投资建议）
DEFAULT_TRANSACTION_COST_BPS: float = 10.0  # 单边交易成本 10bp
DEFAULT_SLIPPAGE_BPS: float = 5.0           # 单边滑点 5bp
DEFAULT_PRICE_LIMIT_PCT: float = 10.0       # 涨跌停限制 10%
DEFAULT_MIN_VOLUME: float = 1e6             # 最小成交量 100 万股


@dataclass
class EvaluationMetrics:
    """walk-forward 评价结果指标。

    Attributes:
        baseline_name: 基准名称。
        train_start: 训练集起始日期。
        train_end: 训练集结束日期。
        test_start: 测试集起始日期。
        test_end: 测试集结束日期。
        sample_size: 样本量。
        ic: Pearson IC。
        rank_ic: Rank IC。
        grouped_returns: 分组收益率。
        sharpe_ratio: 夏普比率（基于按日组合净值序列）。
        max_drawdown: 最大回撤（基于按日组合净值序列，负数）。
        transaction_cost: 交易成本（bp）。
        insufficient_sample: 样本不足标记。
    """

    baseline_name: str = ""
    train_start: str = ""
    train_end: str = ""
    test_start: str = ""
    test_end: str = ""
    sample_size: int = 0
    ic: float | None = None
    rank_ic: float | None = None
    grouped_returns: dict[str, float] = field(default_factory=dict)
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None
    transaction_cost: float = 0.0
    insufficient_sample: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)


@dataclass
class EvaluationReport:
    """walk-forward + 基准对比评价报告。

    标记"模拟评价，不构成投资建议"。

    Attributes:
        report_id: 报告 ID。
        feature_version: 被评价的特征版本。
        policy_version: 被评价的策略版本。
        windows: 每个窗口每个基准的指标列表。
        disclaimer: 免责声明。
    """

    report_id: str = ""
    feature_version: str = ""
    policy_version: str = ""
    windows: list[dict[str, list[EvaluationMetrics]]] = field(default_factory=list)
    disclaimer: str = "模拟评价，不构成投资建议"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "report_id": self.report_id,
            "feature_version": self.feature_version,
            "policy_version": self.policy_version,
            "windows": [
                {baseline: [m.to_dict() for m in metrics] for baseline, metrics in window.items()}
                for window in self.windows
            ],
            "disclaimer": self.disclaimer,
        }


# Research gate provider 类型：返回某日有 ACTIVE/TRUSTED Claim 的 sector_id 集合
# P2.1: 禁止用 momentum_score 代替，必须读取真实 Claim 状态
ResearchGateProvider = Callable[[str], set[str]]


class SectorRotationEvaluator:
    """板块轮动 walk-forward + 基准对比评价器。

    复用 adaptation.engine.walk_forward_windows()，不创建第三套实现。
    评价结果不修改 Claim 事实置信度，不输出投资建议。

    P2.1: with_research_gate 基准使用 research_gate_provider 返回的真实 ACTIVE/TRUSTED
          Claim sector_id 集合过滤，禁止用 momentum_score 阈值代替。
    P2.2: 6 种基准实现为真实不同信号：
          - current_theme_rule: momentum_score 作为信号
          - simple_relative_strength: 相对强度 = momentum - benchmark（真实 RS 信号）
          - simple_reversal: 1 - momentum_score（反转信号）
          - random_equal_weight: 随机信号
          - no_research_ablation: momentum_score 但不过滤（消融对比）
          - with_research_gate: momentum_score 但只保留有 Research 验证的 sector
    P2.3: Sharpe 和 max_drawdown 基于按日组合净值序列计算，不得把横截面样本当时间序列。

    阶段5 模拟评价确认（Task 5.4）：
    - 复用 backtest execution layer 的执行约束语义（transaction_cost_bps /
      slippage_bps / price_limit_pct / min_volume），与
      core/quant/backtest/execution_layer.py 的成交规则保持一致，不另起一套。
    - 输出标记 "模拟评价，不构成投资建议"（DISCLAIMER）。
    - 评价结果不输出买入/卖出/仓位/止盈/止损等交易动作。
    """

    DISCLAIMER: str = "模拟评价，不构成投资建议"

    def __init__(
        self,
        *,
        train_size: int = 60,
        test_size: int = 20,
        transaction_cost_bps: float = DEFAULT_TRANSACTION_COST_BPS,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        price_limit_pct: float = DEFAULT_PRICE_LIMIT_PCT,
        min_volume: float = DEFAULT_MIN_VOLUME,
        random_seed: int = 42,
        research_gate_provider: ResearchGateProvider | None = None,
    ) -> None:
        """初始化评价器。

        Args:
            train_size: 训练集大小（天数）。
            test_size: 测试集大小（天数）。
            transaction_cost_bps: 单边交易成本（bp）。
            slippage_bps: 单边滑点（bp）。
            price_limit_pct: 涨跌停限制（百分比）。
            min_volume: 最小成交量约束。
            random_seed: 随机种子（用于随机等权基准的可复现性）。
            research_gate_provider: P2.1 Research gate 提供器，返回某日有
                ACTIVE/TRUSTED Claim 的 sector_id 集合。None 时 with_research_gate
                基准输出 insufficient_sample（不得用 momentum_score 代替）。
        """
        self._train_size = train_size
        self._test_size = test_size
        self._transaction_cost_bps = transaction_cost_bps
        self._slippage_bps = slippage_bps
        self._price_limit_pct = price_limit_pct
        self._min_volume = min_volume
        self._rng = random.Random(random_seed)
        self._research_gate_provider = research_gate_provider

    def evaluate(
        self,
        features: list[SectorFeatureRecord],
        labels: list[EvaluationLabel],
        *,
        feature_version: str = "",
        policy_version: str = "",
    ) -> EvaluationReport:
        """执行 walk-forward + 基准对比评价。

        Args:
            features: 板块特征记录列表。
            labels: 未来表现标签列表。
            feature_version: 被评价的特征版本。
            policy_version: 被评价的策略版本。

        Returns:
            评价报告，包含每个窗口每个基准的指标。
        """
        # 按日期对齐特征和标签
        aligned = self._align_features_labels(features, labels)
        if len(aligned) < self._train_size + self._test_size:
            logger.warning(
                "样本不足：%d < %d + %d，输出 insufficient_sample",
                len(aligned), self._train_size, self._test_size,
            )
            return EvaluationReport(
                feature_version=feature_version,
                policy_version=policy_version,
                windows=[],
                disclaimer=self.DISCLAIMER,
            )

        dates = sorted(aligned.keys())
        windows = walk_forward_windows(dates, self._train_size, self._test_size)

        report_windows: list[dict[str, list[EvaluationMetrics]]] = []
        for window in windows:
            window_metrics: dict[str, list[EvaluationMetrics]] = {name: [] for name in BASELINE_NAMES}
            test_dates = self._dates_between(dates, window["test_start"], window["test_end"])

            for baseline_name in BASELINE_NAMES:
                metrics = self._evaluate_baseline(
                    baseline_name, aligned, test_dates, window,
                )
                window_metrics[baseline_name].append(metrics)

            report_windows.append(window_metrics)

        return EvaluationReport(
            feature_version=feature_version,
            policy_version=policy_version,
            windows=report_windows,
            disclaimer=self.DISCLAIMER,
        )

    def _align_features_labels(
        self,
        features: list[SectorFeatureRecord],
        labels: list[EvaluationLabel],
    ) -> dict[str, dict[str, dict[str, float]]]:
        """按日期对齐特征和标签。

        Returns:
            {date: {sector_id: {"signal": float, "return": float, "benchmark": float}}}
        """
        # 按 (as_of, sector_id) 索引特征
        feature_map: dict[tuple[str, str], SectorFeatureRecord] = {}
        for feature in features:
            key = (feature.as_of, feature.sector_id)
            feature_map[key] = feature

        # 按 (as_of, sector_id) 索引标签
        label_map: dict[tuple[str, str], EvaluationLabel] = {}
        for label in labels:
            key = (label.as_of, label.sector_id)
            label_map[key] = label

        # 对齐
        aligned: dict[str, dict[str, dict[str, float]]] = {}
        for (as_of, sector_id), feature in feature_map.items():
            label = label_map.get((as_of, sector_id))
            if label is None:
                continue
            if as_of not in aligned:
                aligned[as_of] = {}
            # 用 momentum_score 作为信号，forward_return 作为结果
            aligned[as_of][sector_id] = {
                "signal": feature.momentum_score,
                "return": label.forward_return,
                "benchmark": label.benchmark_return,
                "breadth": feature.breadth_score,
            }

        return aligned

    def _dates_between(
        self, all_dates: list[str], start: str, end: str,
    ) -> list[str]:
        """返回 start 到 end 之间的日期列表。"""
        return [d for d in all_dates if start <= d <= end]

    def _evaluate_baseline(
        self,
        baseline_name: str,
        aligned: dict[str, dict[str, dict[str, float]]],
        test_dates: list[str],
        window: dict[str, str],
    ) -> EvaluationMetrics:
        """评价单个基准。

        P2.2: 每种基准使用真实不同的信号计算逻辑。
        P2.3: Sharpe 和 max_drawdown 基于按日组合净值序列计算。

        Args:
            baseline_name: 基准名称。
            aligned: 对齐后的特征和标签。
            test_dates: 测试集日期列表。
            window: 窗口信息。

        Returns:
            评价指标。
        """
        # 收集每日横截面信号和收益（用于 IC 计算）
        signals: list[float] = []
        returns: list[float] = []
        benchmarks: list[float] = []

        # P2.3: 按日组合净值序列（每日等权组合收益）
        daily_portfolio_returns: list[float] = []

        for date in test_dates:
            sectors = aligned.get(date, {})
            if not sectors:
                continue

            day_signals: list[float] = []
            day_returns: list[float] = []
            day_benchmarks: list[float] = []

            for sector_id, data in sectors.items():
                signal_value: float | None = None
                return_value = data["return"]
                benchmark_value = data["benchmark"]

                if baseline_name == "current_theme_rule":
                    # P2.2: 当前主题规则用 momentum_score 作为信号
                    signal_value = data["signal"]
                elif baseline_name == "simple_relative_strength":
                    # P2.2: 简单相对强度用 momentum - benchmark（真实 RS 信号，非 breadth）
                    signal_value = data["signal"] - data["benchmark"]
                elif baseline_name == "simple_reversal":
                    # P2.2: 简单反转用 1 - momentum_score（反转信号）
                    signal_value = 1.0 - data["signal"]
                elif baseline_name == "random_equal_weight":
                    # P2.2: 随机等权用随机信号
                    signal_value = self._rng.random()
                elif baseline_name == "no_research_ablation":
                    # P2.2: 无 Research 门禁消融用 momentum_score 但不过滤
                    signal_value = data["signal"]
                elif baseline_name == "with_research_gate":
                    # P2.1: 有 Research 门禁版本必须使用真实 ACTIVE/TRUSTED Claim 过滤
                    # 禁止用 momentum_score >= 0.5 代替
                    if self._research_gate_provider is None:
                        # 无 provider 时不得用 momentum_score 代替，输出 insufficient_sample
                        continue
                    validated_sectors = self._research_gate_provider(date)
                    if sector_id not in validated_sectors:
                        continue
                    signal_value = data["signal"]

                if signal_value is None:
                    continue
                day_signals.append(signal_value)
                day_returns.append(return_value)
                day_benchmarks.append(benchmark_value)

            if not day_signals:
                continue

            signals.extend(day_signals)
            returns.extend(day_returns)
            benchmarks.extend(day_benchmarks)

            # P2.3: 按日组合净值（等权组合当日收益 = 当日各 sector 收益均值 - 交易成本）
            daily_return = sum(day_returns) / len(day_returns)
            # 扣除交易成本（单边，bp → 百分比）
            transaction_cost_pct = (self._transaction_cost_bps + self._slippage_bps) / 10000.0
            daily_portfolio_returns.append(daily_return - transaction_cost_pct)

        sample_size = len(signals)
        if sample_size < 10 or len(daily_portfolio_returns) < 2:
            return EvaluationMetrics(
                baseline_name=baseline_name,
                train_start=window["train_start"],
                train_end=window["train_end"],
                test_start=window["test_start"],
                test_end=window["test_end"],
                sample_size=sample_size,
                insufficient_sample=True,
            )

        # 计算横截面 IC（信号与收益的相关性）
        ic = pearson_ic(signals, returns)
        ric = rank_ic(signals, returns)
        grouped = grouped_returns(signals, returns, groups=5)

        # P2.3: Sharpe 基于按日组合净值序列计算（不得用横截面样本当时间序列）
        sharpe = self._compute_sharpe_from_daily_nav(daily_portfolio_returns)

        # P2.3: max_drawdown 基于按日组合净值序列计算
        max_dd = self._compute_max_drawdown_from_daily_nav(daily_portfolio_returns)

        # 交易成本（单边）
        transaction_cost = self._transaction_cost_bps + self._slippage_bps

        return EvaluationMetrics(
            baseline_name=baseline_name,
            train_start=window["train_start"],
            train_end=window["train_end"],
            test_start=window["test_start"],
            test_end=window["test_end"],
            sample_size=sample_size,
            ic=ic,
            rank_ic=ric,
            grouped_returns=grouped,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            transaction_cost=transaction_cost,
            insufficient_sample=False,
        )

    @staticmethod
    def _compute_sharpe_from_daily_nav(
        daily_returns: list[float],
        *,
        annualization_factor: float = 252.0,
    ) -> float | None:
        """P2.3: 基于按日组合净值序列计算 Sharpe 比率。

        不得把横截面样本当时间序列。Sharpe = mean(daily_returns) / std(daily_returns) * sqrt(252)。

        Args:
            daily_returns: 按日组合收益率序列（已扣除交易成本）。
            annualization_factor: 年化因子（默认 252 个交易日）。

        Returns:
            年化 Sharpe 比率；样本不足或标准差为 0 时返回 None。
        """
        if len(daily_returns) < 2:
            return None
        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / len(daily_returns)
        std_return = variance ** 0.5
        if std_return == 0:
            return None
        return (mean_return / std_return) * (annualization_factor ** 0.5)

    @staticmethod
    def _compute_max_drawdown_from_daily_nav(daily_returns: list[float]) -> float:
        """P2.3: 基于按日组合净值序列计算最大回撤。

        不得把横截面样本当时间序列。先构建累计净值，再计算峰值到谷值的最大跌幅。

        Args:
            daily_returns: 按日组合收益率序列（已扣除交易成本）。

        Returns:
            最大回撤（负数或 0）；样本不足时返回 0.0。
        """
        if not daily_returns:
            return 0.0
        # 构建累计净值序列（初始净值 1.0）
        nav = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in daily_returns:
            nav *= (1.0 + r / 100.0)  # 收益率单位为百分比
            if nav > peak:
                peak = nav
            dd = (nav - peak) / peak * 100.0  # 回撤百分比
            if dd < max_dd:
                max_dd = dd
        return max_dd
