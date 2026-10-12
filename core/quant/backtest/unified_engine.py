"""
统一回测引擎 - 向量化优先，配置化驱动

架构说明：
1. DataLayer: 数据加载与缓存 (Polars/Pandas/NumPy)
2. ComputeLayer: 指标计算 (向量化/Numba/GPU)
3. SignalLayer: 信号生成 (配置化规则引擎或策略类)
4. ExecutionLayer: 交易执行 (T+1/涨跌停/停牌/分红)
5. ReportLayer: 结果输出 (权益曲线/交易记录/风险指标)

设计理念：
- 单一引擎：消除 flexible 和 optimized 的割裂感
- 向量化优先：所有计算尽可能使用 NumPy/Pandas/Polars 向量化
- 配置化驱动：策略可以是 Python 类或 YAML/JSON 配置
- 性能一致：无论策略形式如何，都享受相同的性能优化
"""

from datetime import datetime
from threading import Event
from typing import Any, Dict, Generator, List, Optional, Union

import numpy as np
import pandas as pd

from config.logger import get_logger

from .models import (
    TimeGranularity,
    BacktestConfig,
    RejectedOrderStat,
    TradeRecord,
    BacktestResult,
    _make_json_serializable,
    _calculate_drawdown_numba,
    _calculate_sharpe_numba,
    _calculate_sortino_numba,
    _calculate_streaks_numba,
    _trade_to_dict,
)

from .data_layer import DataLayer
from .compute_layer import ComputeLayer
from .signal_layer import SignalLayer
from .execution_layer import ExecutionLayer
from .report_layer import ReportLayer
# P0-6：接入共享成交策略，统一回测与模拟盘的成交规则源
from core.quant.execution_policy import (
    ExecutionPolicy,
    SharedExecutionPolicy,
)

logger = get_logger(__name__)


class UnifiedBacktestEngine:
    """
    统一回测引擎

    核心特性：
    1. 向量化优先：使用 NumPy/Pandas/Polars 进行向量化计算
    2. 配置化驱动：支持 YAML/JSON 配置的策略
    3. 多粒度支持：日线、分钟线、Tick级别
    4. 统一接口：无论策略形式如何，使用相同的 API

    使用示例：
        # 方式1: 使用策略类
        engine = UnifiedBacktestEngine(data_query, config)
        result = engine.run_backtest(start_date, end_date, strategy_instance)

        # 方式2: 使用配置化策略
        engine = UnifiedBacktestEngine(data_query, config)
        result = engine.run_backtest(start_date, end_date, strategy_config)
    """

    def __init__(
        self,
        data_query,
        config: Optional[BacktestConfig] = None
    ):
        """
        初始化统一回测引擎

        参数：
            data_query: 数据查询对象
            config: 回测配置，使用默认配置如果为 None
        """
        self.data_query = data_query
        self.config = config or BacktestConfig()

        # 缓存
        self._stock_pool_cache: Dict[str, pd.DataFrame] = {}
        self._cache_order: List[str] = []
        self._cache_max_size = 1000

        # 权益曲线历史
        self._equity_history: List[Tuple[str, float]] = []

        # 向量化模式状态
        self._vectorized_mode = False
        self._signal_matrix: Optional[np.ndarray] = None
        self._stock_codes_list: Optional[List[str]] = None
        self._date_to_idx: Optional[Dict[str, int]] = None

        # 因子矩阵（三维数组）
        self._factor_matrix: Optional[Any] = None

        # 回测真实性统计（每次 run_backtest 时重置）
        self._rejected_stats = RejectedOrderStat()
        self._slippage_cost_total: float = 0.0

        # P0-6：共享成交策略实例，供 execution_layer 引用统一规则源
        # 回测向量化路径继续读 self.config.* 字段（性能考虑），
        # fallback 路径通过 self._shared_policy.policy 做单笔评估。
        self._shared_policy = SharedExecutionPolicy(
            ExecutionPolicy.from_backtest_config(self.config)
        )

        # 组合各功能层
        self.data_layer = DataLayer(self)
        self.compute_layer = ComputeLayer(self)
        self.signal_layer = SignalLayer(self)
        self.execution_layer = ExecutionLayer(self)
        self.report_layer = ReportLayer(self)

        logger.info(
            f"✅ UnifiedBacktestEngine 初始化完成 "
            f"(资金: {self.config.initial_capital:,.0f}, "
            f"粒度: {self.config.time_granularity.value}, "
            f"滑点: {self.config.slippage_rate*100:.2f}%, "
            f"排除ST: {self.config.exclude_st})"
        )

    def reset_realism_stats(self) -> None:
        """重置回测真实性统计计数器（每次 run_backtest 入口自动调用）"""
        self._rejected_stats = RejectedOrderStat()
        self._slippage_cost_total = 0.0

    def get_rejected_stats(self) -> RejectedOrderStat:
        return self._rejected_stats

    def get_slippage_cost(self) -> float:
        return self._slippage_cost_total

    def _execute_trades(
        self,
        current_time: Union[str, pd.Timestamp, datetime],
        stock_pool: Any,
        signals: Dict[str, Any],
        portfolio: Dict[str, int],
        cash: float,
        position_info: Dict[str, Dict[str, Any]],
        data_dict: Dict[str, Dict[str, Any]]
    ):
        """向后兼容的委托方法：交易执行已迁移至 ExecutionLayer。"""
        return self.execution_layer._execute_trades(
            current_time, stock_pool, signals, portfolio, cash, position_info, data_dict
        )

    @property
    def initial_capital(self) -> float:
        """获取初始资金（便捷属性）"""
        return self.config.initial_capital

    @property
    def commission_rate(self) -> float:
        """获取手续费率（便捷属性）"""
        return self.config.commission_rate

    def run_backtest_streaming(
        self,
        start_date: Union[str, pd.Timestamp, datetime],
        end_date: Union[str, pd.Timestamp, datetime],
        strategy: Any,
        stop_event: Optional[Event] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """
        流式回测（run_backtest 的别名，保持向后兼容）

        参数：
            start_date: 开始日期
            end_date: 结束日期
            strategy: 策略实例或配置
            stop_event: 停止事件

        返回：
            Generator，产出回测事件
        """
        yield from self.run_backtest(start_date, end_date, strategy, stop_event)

    def run_backtest(
        self,
        start_date: Union[str, pd.Timestamp, datetime],
        end_date: Union[str, pd.Timestamp, datetime],
        strategy: Any,
        stop_event: Optional[Event] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """
        运行回测（流式生成器）

        参数：
            start_date: 开始日期
            end_date: 结束日期
            strategy: 策略实例或配置
            stop_event: 停止事件

        返回：
            Generator，产出回测事件
        """
        import time

        # 标准化日期
        start_ts = self.data_layer._normalize_datetime(start_date)
        end_ts = self.data_layer._normalize_datetime(end_date)

        if start_ts >= end_ts:
            yield {"type": "error", "data": {"message": "开始日期必须早于结束日期"}}
            return

        # 重置回测真实性统计
        self.reset_realism_stats()

        # 获取时间序列
        time_series = self.data_layer._get_time_series(start_ts, end_ts)
        if not time_series:
            yield {"type": "error", "data": {"message": "未找到有效的时间序列"}}
            return

        # 清除历史数据
        self._equity_history = []

        # [性能优化] 清除矩阵缓存，确保新回测从头开始
        from core.quant.strategies.vectorized_base import clear_matrix_cache
        clear_matrix_cache()

        # 初始化账户
        portfolio: Dict[str, int] = {}
        cash = self.config.initial_capital
        all_trades: List = []
        position_info: Dict[str, Dict[str, Any]] = {}
        prev_day_data: Dict[str, Dict[str, Any]] = {}

        # 产出开始信号
        yield {
            "type": "backtest_start",
            "data": {
                "initialCapital": self.config.initial_capital,
                "timeGranularity": self.config.time_granularity.value,
                "startDate": start_ts.strftime("%Y-%m-%d"),
                "endDate": end_ts.strftime("%Y-%m-%d")
            }
        }

        # 预加载数据
        t_preload_start = time.perf_counter()
        preloaded_data = self.data_layer._preload_data(start_ts, end_ts, strategy)
        preload_time = (time.perf_counter() - t_preload_start) * 1000
        print(f"[PERF-TRACE] _preload_data 耗时: {preload_time:.1f}ms")
        logger.info(f"[PERF] _preload_data 耗时: {preload_time:.1f}ms")

        # [声明式因子系统] 预加载策略声明的因子
        t_factors_start = time.perf_counter()
        self.data_layer._preload_factors(strategy, preloaded_data)
        factors_time = (time.perf_counter() - t_factors_start) * 1000
        print(f"[PERF-TRACE] _preload_factors 耗时: {factors_time:.1f}ms")
        logger.info(f"[PERF] _preload_factors 耗时: {factors_time:.1f}ms")

        # 主循环
        perf_snapshots = []
        total_steps = len(time_series)

        for idx, current_time in enumerate(time_series, 1):
            if stop_event and stop_event.is_set():
                yield {"type": "backtest_cancelled", "data": {"message": "回测已取消"}}
                return

            day_start = time.perf_counter()
            perf_breakdown = {}

            logger.debug(f"[Day {idx}] 开始加载数据: {current_time}")

            try:
                # 1. 加载当日数据
                t0 = time.perf_counter()
                stock_pool, use_pl, data_dict = self.data_layer._load_day_data(current_time)
                perf_breakdown['data_load'] = (time.perf_counter() - t0) * 1000

                logger.debug(f"[Day {idx}] 数据加载完成: {perf_breakdown['data_load']:.1f}ms")

                if stock_pool is None:
                    continue

                if isinstance(stock_pool, np.ndarray):
                    if stock_pool.size == 0:
                        continue
                elif use_pl and hasattr(stock_pool, 'is_empty'):
                    if stock_pool.is_empty():
                        continue
                elif hasattr(stock_pool, 'empty') and stock_pool.empty:
                    continue

                # 2. 设置策略上下文
                t0 = time.perf_counter()
                self.signal_layer._set_strategy_context(strategy, current_time, portfolio, cash)
                perf_breakdown['set_context'] = (time.perf_counter() - t0) * 1000

                # 3. 生成信号
                t0 = time.perf_counter()
                signals = self.signal_layer._generate_signals(
                    strategy, current_time, stock_pool, preloaded_data, idx, time_series
                )
                perf_breakdown['signal_generation'] = (time.perf_counter() - t0) * 1000
                if idx == 1:
                    logger.info(f"[PERF] Day 1 _generate_signals 耗时: {perf_breakdown['signal_generation']:.1f}ms")

                logger.debug(f"[Day {idx}] signals count: {len(signals)}")

                # 4. 执行交易
                t0 = time.perf_counter()
                portfolio, cash, trades = self.execution_layer._execute_trades(
                    current_time, stock_pool, signals, portfolio, cash, position_info, data_dict
                )
                all_trades.extend(trades)
                perf_breakdown['execute_trades'] = (time.perf_counter() - t0) * 1000

                logger.debug(f"[Day {idx}] signals: {len(signals)}, stock_pool: {type(stock_pool)}, data_dict keys: {len(data_dict) if isinstance(data_dict, dict) else 'N/A'}")

                logger.debug(f"[Day {idx}] trades count: {len(trades)}")

                # 5. 记录并产出交易
                for trade in trades:
                    self.report_layer._log_trade(trade)
                    yield self.report_layer._format_trade_event(trade)

                # 6. 分红结算
                t0 = time.perf_counter()
                cash, dividends = self.execution_layer._process_dividends(
                    current_time, portfolio, prev_day_data, data_dict, cash
                )
                for div in dividends:
                    yield {"type": "dividend_payout", "data": div}
                perf_breakdown['dividend_settlement'] = (time.perf_counter() - t0) * 1000

                # 7. 计算账户价值
                t0 = time.perf_counter()
                portfolio_value = self.report_layer._calculate_portfolio_value(portfolio, data_dict)
                total_value = cash + portfolio_value
                perf_breakdown['calc_value'] = (time.perf_counter() - t0) * 1000

                # 8. 更新前一日数据
                prev_day_data = data_dict

                # 9. 记录权益曲线
                if self.report_layer._should_record_equity(idx):
                    self._equity_history.append((current_time.strftime("%Y-%m-%d"), total_value))
                    yield {
                        "type": "daily_equity_engine",
                        "data": {
                            "date": current_time.strftime("%Y-%m-%d"),
                            "equity": total_value,
                            "strategyReturn": total_value,
                            "cash": cash,
                            "positions": len(portfolio),
                            "trades": len(trades)
                        }
                    }

                # 10. 性能记录
                perf_breakdown['total'] = (time.perf_counter() - day_start) * 1000
                perf_snapshots.append({"day": idx, "date": current_time.strftime('%Y-%m-%d'), "metrics": perf_breakdown})

                # 11. 进度更新
                if idx % 5 == 0 or idx == total_steps:
                    yield {"type": "progress", "data": {"progress": round((idx / total_steps) * 100, 1)}}

                # 12. 慢查询日志
                self.report_layer._log_performance_if_slow(idx, total_steps, current_time, perf_breakdown)

            except Exception as e:
                import traceback
                tb_str = traceback.format_exc()
                logger.error(f"回测过程中出错: {e}\n{tb_str}")
                yield {"type": "error", "data": {"message": f"回测过程中出错: {str(e)}", "traceback": tb_str}}

        # 保存性能报告
        self.report_layer._save_performance_report(perf_snapshots, strategy)

        # 计算最终结果
        result = self.report_layer._calculate_final_result(all_trades, start_ts, end_ts)

        # 产出最终指标
        yield {
            "type": "final_metrics",
            "data": {
                "totalReturn": round(result.total_return, 2),
                "annualizedReturn": round(result.annualized_return, 2),
                "maxDrawdown": round(result.max_drawdown, 2),
                "sharpeRatio": round(result.sharpe_ratio, 2),
                "sortinoRatio": round(result.sortino_ratio, 2),
                "volatility": round(result.volatility, 2),
                "winRate": round(result.win_rate, 1),
                "profitFactor": round(result.profit_factor, 2),
                "tradesCount": result.trade_count,
                "avgTradeReturn": round(result.avg_trade_return, 2),
                "maxWinningStreak": result.max_winning_streak,
                "maxLosingStreak": result.max_losing_streak,
                "calmarRatio": round(result.calmar_ratio, 2),
                # 回测真实性统计
                "rejectedOrders": result.rejected_orders,
                "slippageCost": result.slippage_cost,
                "filterStats": result.filter_stats,
            }
        }

        # 数据持久化
        self.report_layer._persist_results(result, all_trades, strategy, start_ts, end_ts)

        # 【新增】计算月度收益（格式：{year: number, months: (number | null)[]}）
        monthly_returns = []
        if self._equity_history:
            try:
                df = pd.DataFrame(self._equity_history, columns=['date', 'equity'])
                df['date'] = pd.to_datetime(df['date'])
                df['year'] = df['date'].dt.year
                df['month'] = df['date'].dt.month

                for year, year_group in df.groupby('year'):
                    year_data = {
                        'year': int(year),
                        'months': [None] * 12
                    }

                    for month, month_group in year_group.groupby('month'):
                        first_equity = month_group['equity'].iloc[0]
                        last_equity = month_group['equity'].iloc[-1]
                        if first_equity > 0:
                            monthly_return = (last_equity - first_equity) / first_equity * 100
                            year_data['months'][int(month) - 1] = float(round(monthly_return, 2))

                    monthly_returns.append(year_data)
            except Exception as e:
                logger.error(f"[Monthly] 计算月度收益失败: {e}")

        # 【新增】获取基准曲线数据
        benchmark_curve = []
        benchmark_return = 0.0
        try:
            if hasattr(self.data_query, 'conn') and self.data_query.conn:
                start_str = start_ts.strftime("%Y-%m-%d")
                end_str = end_ts.strftime("%Y-%m-%d")
                query = f"""
                    SELECT date, close
                    FROM benchmark_data
                    WHERE date >= '{start_str}' AND date <= '{end_str}'
                    ORDER BY date
                """
                result_rows = self.data_query.conn.execute(query).fetchall()
                if result_rows:
                    initial_benchmark = result_rows[0][1]
                    final_benchmark = result_rows[-1][1]
                    benchmark_return = round((final_benchmark - initial_benchmark) / initial_benchmark * 100, 2)
                    for row in result_rows:
                        benchmark_curve.append({
                            'date': row[0] if isinstance(row[0], str) else row[0].strftime("%Y-%m-%d"),
                            'equity': round(self.config.initial_capital * row[1] / initial_benchmark, 2)
                        })
        except Exception as e:
            logger.error(f"[Benchmark] 获取基准数据失败: {e}")

        # 【新增】计算平均持仓天数
        avg_holding_days = 0.0
        if all_trades:
            sell_trades = [t for t in all_trades if t.action == 'sell']
            if sell_trades:
                total_days = sum(t.holding_days for t in sell_trades if t.holding_days)
                avg_holding_days = total_days / len(sell_trades) if sell_trades else 0.0

        # 产出完成事件
        yield {
            "type": "stream_complete",
            "data": {
                "finalEquity": result.final_equity,
                "totalReturn": round(result.total_return, 2),
                "annualizedReturn": round(result.annualized_return, 2),
                "maxDrawdown": round(result.max_drawdown, 2),
                "sharpeRatio": round(result.sharpe_ratio, 2),
                "sortinoRatio": round(result.sortino_ratio, 2),
                "volatility": round(result.volatility, 2),
                "winRate": round(result.win_rate, 1),
                "profitFactor": round(result.profit_factor, 2),
                "totalTrades": result.trade_count,
                "avgTradeReturn": round(result.avg_trade_return, 2),
                "maxWinningStreak": result.max_winning_streak,
                "maxLosingStreak": result.max_losing_streak,
                "calmarRatio": round(result.calmar_ratio, 2),
                "avgHoldingDays": round(avg_holding_days, 1),
                "benchmarkReturn": benchmark_return,
                "equityCurve": result.equity_curve,
                "benchmarkCurve": benchmark_curve,
                "monthlyReturns": monthly_returns,
                "trades": [_trade_to_dict(t) for t in result.trades],
                # 回测真实性统计（同步 final_metrics）
                "rejectedOrders": result.rejected_orders,
                "slippageCost": result.slippage_cost,
                "filterStats": result.filter_stats,
            }
        }
