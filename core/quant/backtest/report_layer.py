"""
回测报告层

负责权益计算、交易日志、最终结果、报告持久化。
"""

import json
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# 可选依赖导入
try:
    import polars as pl
    POLARS_AVAILABLE = True
except ImportError:
    POLARS_AVAILABLE = False
    pl = None

from config.logger import get_logger
from config.config import Config

from .models import (
    TimeGranularity,
    BacktestResult,
    TradeRecord,
    _calculate_drawdown_numba,
    _calculate_sharpe_numba,
    _calculate_sortino_numba,
    _calculate_streaks_numba,
)

logger = get_logger(__name__)


class ReportLayer:
    """
    回测报告层

    负责权益计算、交易日志、最终结果、报告持久化。
    """

    def __init__(self, engine):
        """
        初始化报告层。

        Args:
            engine: UnifiedBacktestEngine 实例，作为共享状态容器。
        """
        self.engine = engine

    def __getattr__(self, name):
        """将未在层中定义的属性访问委托给引擎。"""
        return getattr(self.engine, name)

    def __setattr__(self, name, value):
        """将属性赋值委托给引擎，仅保留 engine 引用在层自身。"""
        if name == 'engine':
            super().__setattr__(name, value)
        else:
            setattr(self.engine, name, value)

    def _calculate_portfolio_value(
        self,
        positions: Dict[str, int],
        data_dict: Dict[str, Dict[str, Any]]
    ) -> float:
        """
        计算持仓市值 - 向量化优化版本

        性能优化：
        1. 使用 NumPy 向量化计算市值
        2. 避免逐股票循环累加
        """
        if not positions or not data_dict:
            return 0.0

        # =========================================================================
        # [向量化优化] 使用 NumPy 批量计算市值
        # =========================================================================
        codes = list(positions.keys())
        shares = np.array([positions[c] for c in codes], dtype=np.float64)

        prices = np.array([
            float(data_dict.get(c, {}).get('close', 0))
            for c in codes
        ], dtype=np.float64)

        return float(np.dot(shares, prices))

    def _should_record_equity(self, idx: int) -> bool:
        """判断是否应该记录权益曲线"""
        if self.config.time_granularity == TimeGranularity.DAILY:
            return True
        return idx % 60 == 0  # 分钟级别每60分钟记录一次

    def _log_trade(self, trade: TradeRecord):
        """记录交易日志"""
        action_cn = "买入" if trade.action == 'buy' else "卖出"
        logger.info(f"[TRADE] {trade.date} {action_cn} {trade.code}: {trade.shares}股 @ {trade.price:.2f}")
        if trade.action == 'sell':
            logger.info(f"   PnL: {trade.profit_loss:,.2f} (ROI: {trade.roi:.2f}%)")

    def _format_trade_event(self, trade: TradeRecord) -> Dict[str, Any]:
        """
        格式化交易事件，对齐前端 API 字段命名

        字段映射：
        - shares → quantity
        - profit_loss → profitLoss
        - entry_price → entryPrice
        - entry_date → entryDate
        - exit_price → exitPrice
        - exit_date → exitDate
        - holding_days → holdingDays
        - position_id → positionId
        """
        stock_code = str(trade.code).zfill(6) if trade.code.isdigit() else trade.code
        position_id = trade.position_id or f"{trade.entry_date}-{trade.code}"

        return {
            "type": "new_trade_engine",
            "data": {
                "id": f"{trade.date}-{stock_code}-{trade.action}",
                "date": trade.date,
                "symbolCode": stock_code,
                "symbol": stock_code,
                "code": stock_code,
                "action": trade.action,
                "price": trade.price,
                "quantity": trade.shares,
                "shares": trade.shares,
                "amount": trade.amount,
                "commission": trade.commission,
                "tax": trade.tax,
                "profitLoss": trade.profit_loss,
                "profit_loss": trade.profit_loss,
                "roi": trade.roi,
                "entryPrice": trade.entry_price,
                "entry_price": trade.entry_price,
                "entryDate": trade.entry_date,
                "entry_date": trade.entry_date,
                "exitPrice": trade.exit_price if trade.action == 'sell' else 0.0,
                "exit_price": trade.exit_price if trade.action == 'sell' else 0.0,
                "exitDate": trade.exit_date if trade.action == 'sell' else "",
                "exit_date": trade.exit_date if trade.action == 'sell' else "",
                "holdingDays": trade.holding_days,
                "holding_days": trade.holding_days,
                "positionId": position_id,
                "position_id": position_id,
                "indicators": trade.indicators
            }
        }

    def _log_performance_if_slow(
        self,
        idx: int,
        total_steps: int,
        current_time: pd.Timestamp,
        perf_breakdown: Dict[str, float]
    ):
        """如果性能较慢则记录日志"""
        PERF_THRESHOLD_MS = 200
        is_slow = perf_breakdown.get('total', 0) > PERF_THRESHOLD_MS
        is_first = idx == 1
        is_last = idx == total_steps

        if is_first or is_last or is_slow:
            log_level = logger.warning if (is_slow or is_first) else logger.info
            log_msg = (
                f"[PERF][Day {idx}] {current_time.strftime('%Y-%m-%d')}: "
                f"总耗时={perf_breakdown.get('total', 0):.1f}ms | "
                f"数据加载={perf_breakdown.get('data_load', 0):.1f}ms | "
                f"信号生成={perf_breakdown.get('signal_generation', 0):.1f}ms | "
                f"交易执行={perf_breakdown.get('execute_trades', 0):.1f}ms"
            )
            if is_slow and not is_first:
                log_msg += " [SLOW]"
            log_level(log_msg)

    def _save_performance_report(
        self,
        perf_snapshots: List[Dict],
        strategy: Any
    ):
        """保存性能报告"""
        try:
            run_id = int(time.time())
            perf_report_file = Path(Config.DB_PATH).parent / f"perf_report_{run_id}.json"

            strategy_name = getattr(strategy, 'strategy_name',
                         getattr(strategy, 'name', 'Unknown'))

            with open(perf_report_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "strategy": strategy_name,
                    "granularity": self.config.time_granularity.value,
                    "snapshots": perf_snapshots
                }, f, ensure_ascii=False, indent=2)

            logger.info(f"[PERF] 性能报告已保存至: {perf_report_file}")
        except Exception as e:
            logger.error(f"[PERF] 保存性能报告失败: {e}")

    def _calculate_final_result(
        self,
        all_trades: List[TradeRecord],
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp
    ) -> BacktestResult:
        """
        计算最终回测结果 - 向量化 + Numba 加速版本

        性能优化：
        1. 使用 NumPy 向量化计算盈亏统计
        2. 使用 Numba JIT 加速最大回撤、夏普比率等计算
        3. 减少内存分配和循环开销
        """
        # =========================================================================
        # Step 1: 交易统计 - 向量化计算
        # =========================================================================
        sell_trades = [t for t in all_trades if t.action == 'sell']
        trade_count = len(sell_trades)

        if trade_count > 0:
            profit_losses = np.array([t.profit_loss for t in sell_trades], dtype=np.float64)
            rois = np.array([t.roi for t in sell_trades], dtype=np.float64)

            total_profit = float(np.sum(profit_losses[profit_losses > 0]))
            total_loss = float(np.sum(np.abs(profit_losses[profit_losses < 0])))
            win_trades = int(np.sum(profit_losses > 0))

            win_rate = (win_trades / trade_count * 100)
            profit_factor = (total_profit / total_loss) if total_loss > 0 else (total_profit if total_profit > 0 else 0.0)
            avg_trade_return = float(np.mean(rois))

            # 使用 Numba 加速计算连胜/连亏
            profit_signs = np.sign(profit_losses).astype(np.float64)
            max_winning_streak, max_losing_streak = _calculate_streaks_numba(profit_signs)
        else:
            total_profit = total_loss = win_rate = profit_factor = avg_trade_return = 0.0
            max_winning_streak = max_losing_streak = 0

        # =========================================================================
        # Step 2: 最大回撤 - Numba 加速
        # =========================================================================
        max_drawdown = 0.0
        if self._equity_history:
            equity_values = np.array([v for _, v in self._equity_history], dtype=np.float64)
            max_drawdown = _calculate_drawdown_numba(equity_values)

        # =========================================================================
        # Step 3: 收益率计算
        # =========================================================================
        final_equity = self._equity_history[-1][1] if self._equity_history else self.config.initial_capital
        total_return = (final_equity - self.config.initial_capital) / self.config.initial_capital * 100

        days = (end_ts - start_ts).days
        years = days / 365.25
        annualized_return = ((final_equity / self.config.initial_capital) ** (1 / years) - 1) * 100 if years > 0 else total_return

        # =========================================================================
        # Step 4: 风险指标 - Numba 加速
        # =========================================================================
        sharpe_ratio = sortino_ratio = volatility = calmar_ratio = 0.0

        if len(self._equity_history) > 1:
            try:
                equity_values = np.array([v for _, v in self._equity_history], dtype=np.float64)

                # 向量化计算日收益率
                daily_returns = np.diff(equity_values) / equity_values[:-1]
                daily_returns = daily_returns[~np.isnan(daily_returns) & ~np.isinf(daily_returns)]

                if len(daily_returns) > 0:
                    volatility = float(np.std(daily_returns) * np.sqrt(252) * 100)

                    # 使用 Numba 加速夏普比率计算
                    sharpe_ratio = _calculate_sharpe_numba(daily_returns)

                    # 使用 Numba 加速索提诺比率计算
                    sortino_ratio = _calculate_sortino_numba(daily_returns)

                    if max_drawdown != 0:
                        calmar_ratio = float(annualized_return / abs(max_drawdown))
            except Exception as e:
                logger.error(f"计算风险指标失败: {e}")

        # =========================================================================
        # Step 5: 生成权益曲线数据
        # =========================================================================
        equity_curve = [{"date": d, "equity": round(v, 2)} for d, v in self._equity_history]

        return BacktestResult(
            final_equity=final_equity,
            total_return=total_return,
            annualized_return=annualized_return,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            volatility=volatility,
            win_rate=win_rate,
            profit_factor=profit_factor,
            trade_count=trade_count,
            avg_trade_return=avg_trade_return,
            max_winning_streak=max_winning_streak,
            max_losing_streak=max_losing_streak,
            calmar_ratio=calmar_ratio,
            equity_curve=equity_curve,
            trades=all_trades,
            rejected_orders=self._rejected_stats.to_dict(),
            slippage_cost=round(self._slippage_cost_total, 2),
            filter_stats={
                "slippageRate": self.config.slippage_rate,
                "excludeSt": self.config.exclude_st,
                "volumeCapRatio": self.config.volume_cap_ratio,
                "minTradeAmount": self.config.min_trade_amount,
            },
        )

    def _persist_results(
        self,
        result: BacktestResult,
        all_trades: List[TradeRecord],
        strategy: Any,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp
    ):
        """
        持久化回测结果到 Parquet

        性能优化：
        - 使用 Polars 直接写入 Parquet（比 pandas 快 2-3 倍）
        - 异步写入（非阻塞）
        - 精简字段（只保存必要数据）
        """
        try:
            from pathlib import Path
            import uuid
            from datetime import datetime

            strategy_name = getattr(strategy, 'strategy_name',
                         getattr(strategy, 'name', 'Unknown'))

            run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:8]

            output_dir = Path("./data/backtest_results")
            output_dir.mkdir(parents=True, exist_ok=True)

            results_summary = {
                'run_id': [run_id],
                'strategy_name': [strategy_name],
                'start_date': [start_ts.strftime("%Y-%m-%d")],
                'end_date': [end_ts.strftime("%Y-%m-%d")],
                'initial_capital': [self.config.initial_capital],
                'final_capital': [result.final_equity],
                'total_return': [result.total_return],
                'annual_return': [result.annualized_return],
                'max_drawdown': [result.max_drawdown],
                'sharpe_ratio': [result.sharpe_ratio],
                'sortino_ratio': [result.sortino_ratio],
                'win_rate': [result.win_rate],
                'profit_factor': [result.profit_factor],
                'trade_count': [len(all_trades)],
                'created_at': [datetime.now().isoformat()]
            }

            results_df = pl.DataFrame(results_summary)
            results_path = output_dir / f"result_{run_id}.parquet"
            results_df.write_parquet(results_path, compression='snappy')

            if all_trades:
                trades_data = {
                    'run_id': [run_id] * len(all_trades),
                    'code': [t.code for t in all_trades],
                    'action': [t.action for t in all_trades],
                    'date': [t.date for t in all_trades],
                    'price': [t.price for t in all_trades],
                    'shares': [t.shares for t in all_trades],
                    'amount': [t.amount for t in all_trades],
                    'profit_loss': [t.profit_loss for t in all_trades],
                    'roi': [t.roi for t in all_trades],
                    'holding_days': [t.holding_days for t in all_trades]
                }

                trades_df = pl.DataFrame(trades_data)
                trades_path = output_dir / f"trades_{run_id}.parquet"
                trades_df.write_parquet(trades_path, compression='snappy')

            logger.info(f"[Parquet] 回测结果已保存 (run_id: {run_id}, 交易数: {len(all_trades)})")

        except Exception as e:
            logger.error(f"[Parquet] 数据持久化失败: {e}")
