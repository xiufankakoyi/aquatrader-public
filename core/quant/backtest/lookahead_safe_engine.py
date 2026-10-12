"""
防未来函数回测引擎 - Lookahead Safe Backtest Engine

核心原则：
1. 信号只能基于过去和当前已知的数据
2. 买入只能用次日开盘价（T+1）
3. 卖出只能用当日开盘价（假设开盘即判断）或前一日收盘后的决策
4. 禁止使用 high/low/close 等盘中才能确定的价格进行决策

时序规则：
- T日收盘后：计算信号（基于T日及之前的数据）
- T+1日开盘：执行买入（用T+1开盘价）
- 卖出决策：基于T日收盘后的持仓状态决定T+1日开盘卖出
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Callable, Any
from dataclasses import dataclass, field
from collections import defaultdict
from enum import Enum
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')


class ExecutionTiming(Enum):
    """执行时点枚举"""
    NEXT_DAY_OPEN = "next_day_open"  # 次日开盘（T+1）
    CURRENT_DAY_OPEN = "current_day_open"  # 当日开盘
    CURRENT_DAY_CLOSE = "current_day_close"  # 当日收盘（仅用于基准对比，不推荐）


@dataclass
class Trade:
    """交易记录"""
    stock_code: str
    action: str  # 'buy' or 'sell'
    date: str
    price: float
    shares: int = 0
    amount: float = 0.0
    reason: str = ""  # 交易原因


@dataclass
class Position:
    """持仓记录"""
    stock_code: str
    entry_date: str
    entry_price: float
    shares: int
    current_price: float = 0.0
    peak_price: float = 0.0  # 用于追踪止盈
    triggered: bool = False  # 是否触发止盈条件


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 100000.0
    max_positions: int = 5
    position_ratio: float = 0.18  # 每只股票的仓位比例
    
    # 执行时点配置
    buy_timing: ExecutionTiming = ExecutionTiming.NEXT_DAY_OPEN  # 买入：次日开盘
    sell_timing: ExecutionTiming = ExecutionTiming.CURRENT_DAY_OPEN  # 卖出：当日开盘
    
    # 止盈止损配置
    take_profit_pct: float = 0.03  # 止盈触发比例
    trailing_stop_pct: float = 0.02  # 追踪止盈回撤比例
    max_holding_days: int = 10  # 最大持仓天数
    
    # 交易成本
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    sell_tax: float = 0.001  # 印花税


class LookaheadSafeBacktestEngine:
    """
    防未来函数回测引擎
    
    强制实施正确的数据时序，防止使用未来数据
    """
    
    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.capital = self.config.initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []
        
    def run_backtest(
        self,
        daily_data: Dict[str, Dict],
        start_date: str,
        end_date: str,
        signal_generator: Callable,
        progress_callback: Optional[Callable] = None
    ) -> Dict:
        """
        运行回测
        
        Args:
            daily_data: 股票数据字典 {stock_code: {dates, open, high, low, close, volume}}
            start_date: 回测开始日期
            end_date: 回测结束日期
            signal_generator: 信号生成函数，必须遵循防未来函数规则
            progress_callback: 进度回调函数
            
        Returns:
            回测结果字典
        """
        # 1. 准备交易日历
        all_dates = self._build_trading_calendar(daily_data, start_date, end_date)
        if not all_dates:
            return self._empty_result()
        
        # 2. 预处理数据
        processed_data = self._preprocess_data(daily_data, all_dates)
        
        # 3. 主回测循环
        for i, current_date in enumerate(all_dates):
            # 报告进度
            if progress_callback and i % 10 == 0:
                progress_callback(i / len(all_dates) * 100)
            
            # 获取当前可用的数据（截止到昨日收盘）
            available_data = self._get_available_data(processed_data, current_date, i)
            
            # 生成信号（只能基于available_data）
            signals = signal_generator(available_data, current_date, self.positions)
            
            # 执行卖出（基于昨日收盘后的决策，今日开盘执行）
            self._execute_sells(current_date, processed_data, i)
            
            # 执行买入（基于昨日收盘信号，今日开盘执行T+1）
            self._execute_buys(current_date, signals, processed_data, i)
            
            # 更新持仓状态（收盘后）
            self._update_positions(current_date, processed_data, i)
            
            # 记录权益
            self._record_equity(current_date, processed_data, i)
        
        # 4. 最后清仓
        self._liquidate_all(all_dates[-1], processed_data, len(all_dates) - 1)
        
        return self._build_result()
    
    def _build_trading_calendar(
        self,
        daily_data: Dict[str, Dict],
        start_date: str,
        end_date: str
    ) -> List[str]:
        """构建交易日历"""
        all_dates = set()
        for sc, data in daily_data.items():
            dates = data.get('dates', [])
            for d in dates:
                date_str = str(d)[:10]
                if start_date <= date_str <= end_date:
                    all_dates.add(date_str)
        return sorted(list(all_dates))
    
    def _preprocess_data(
        self,
        daily_data: Dict[str, Dict],
        all_dates: List[str]
    ) -> Dict[str, Dict]:
        """预处理数据，建立日期索引"""
        processed = {}
        date_to_idx = {d: i for i, d in enumerate(all_dates)}
        
        for sc, data in daily_data.items():
            dates = np.array([str(d)[:10] for d in data['dates']])
            
            # 只保留回测区间内的数据
            mask = np.isin(dates, all_dates)
            if not np.any(mask):
                continue
            
            filtered_dates = dates[mask]
            
            # 建立日期到索引的映射
            idx_map = {}
            for i, d in enumerate(filtered_dates):
                if d in date_to_idx:
                    idx_map[d] = i
            
            processed[sc] = {
                'dates': filtered_dates,
                'open': data['open'][mask],
                'high': data['high'][mask],
                'low': data['low'][mask],
                'close': data['close'][mask],
                'volume': data['volume'][mask],
                'date_to_idx': idx_map,
            }
        
        return processed
    
    def _get_available_data(
        self,
        processed_data: Dict[str, Dict],
        current_date: str,
        current_idx: int
    ) -> Dict[str, Dict]:
        """
        获取当前可用的数据（截止到昨日收盘）
        
        这是防未来函数的核心：只能看到昨天的数据，不能看到今天的数据
        """
        available = {}
        
        for sc, data in processed_data.items():
            if current_date not in data['date_to_idx']:
                continue
            
            today_idx = data['date_to_idx'][current_date]
            
            # 只能获取昨日及之前的数据
            if today_idx <= 0:
                continue
            
            available[sc] = {
                'dates': data['dates'][:today_idx],
                'open': data['open'][:today_idx],
                'high': data['high'][:today_idx],
                'low': data['low'][:today_idx],
                'close': data['close'][:today_idx],
                'volume': data['volume'][:today_idx],
            }
        
        return available
    
    def _execute_sells(
        self,
        current_date: str,
        processed_data: Dict[str, Dict],
        current_idx: int
    ):
        """
        执行卖出
        
        使用当日开盘价执行卖出
        """
        sells_to_execute = []
        
        for sc, pos in list(self.positions.items()):
            if sc not in processed_data:
                continue
            
            data = processed_data[sc]
            if current_date not in data['date_to_idx']:
                continue
            
            idx = data['date_to_idx'][current_date]
            open_price = data['open'][idx]
            
            # 检查是否需要卖出
            sell_reason = self._check_sell_conditions(pos, data, idx)
            
            if sell_reason:
                sells_to_execute.append((sc, open_price, sell_reason))
        
        # 执行卖出
        for sc, price, reason in sells_to_execute:
            self._sell_stock(sc, current_date, price, reason)
    
    def _check_sell_conditions(
        self,
        position: Position,
        data: Dict,
        idx: int
    ) -> str:
        """
        检查卖出条件
        
        注意：这里只能使用开盘价做决策，不能使用high/low/close
        """
        open_price = data['open'][idx]
        
        # 计算持仓天数
        entry_idx = np.where(data['dates'] == position.entry_date)[0]
        if len(entry_idx) == 0:
            return ""
        hold_days = idx - entry_idx[0]
        
        # 条件1：最大持仓天数
        if hold_days >= self.config.max_holding_days:
            return f"max_hold_days({hold_days})"
        
        # 条件2：追踪止盈（基于昨日收盘后的peak_price）
        if position.triggered and position.peak_price > 0:
            drawdown = (position.peak_price - open_price) / position.peak_price
            if drawdown >= self.config.trailing_stop_pct:
                return f"trailing_stop({drawdown:.2%})"
        
        # 条件3：固定止损（可选）
        loss_pct = (open_price - position.entry_price) / position.entry_price
        if loss_pct <= -0.05:  # 5%止损
            return f"stop_loss({loss_pct:.2%})"
        
        return ""
    
    def _execute_buys(
        self,
        current_date: str,
        signals: Dict[str, Dict],
        processed_data: Dict[str, Dict],
        current_idx: int
    ):
        """
        执行买入
        
        使用当日开盘价执行买入（T+1规则）
        """
        # 检查持仓限制
        if len(self.positions) >= self.config.max_positions:
            return
        
        # 计算可用仓位
        position_value = self.config.initial_capital * self.config.position_ratio
        
        for sc, signal in signals.items():
            if sc in self.positions:
                continue
            
            if len(self.positions) >= self.config.max_positions:
                break
            
            if sc not in processed_data:
                continue
            
            data = processed_data[sc]
            if current_date not in data['date_to_idx']:
                continue
            
            idx = data['date_to_idx'][current_date]
            open_price = data['open'][idx]
            
            # 检查资金
            if self.capital < position_value:
                break
            
            self._buy_stock(sc, current_date, open_price, signal.get('reason', ''))
    
    def _buy_stock(self, stock_code: str, date: str, price: float, reason: str):
        """买入股票"""
        position_value = self.config.initial_capital * self.config.position_ratio
        
        # 计算股数（100股整数倍）
        shares = int(position_value / price / 100) * 100
        if shares < 100:
            return
        
        amount = shares * price
        commission = max(amount * self.config.commission_rate, self.config.min_commission)
        total_cost = amount + commission
        
        if total_cost > self.capital:
            return
        
        self.capital -= total_cost
        
        self.positions[stock_code] = Position(
            stock_code=stock_code,
            entry_date=date,
            entry_price=price,
            shares=shares,
            current_price=price,
            peak_price=price
        )
        
        self.trades.append(Trade(
            stock_code=stock_code,
            action='buy',
            date=date,
            price=price,
            shares=shares,
            amount=total_cost,
            reason=reason
        ))
    
    def _sell_stock(self, stock_code: str, date: str, price: float, reason: str):
        """卖出股票"""
        if stock_code not in self.positions:
            return
        
        pos = self.positions[stock_code]
        amount = pos.shares * price
        commission = max(amount * self.config.commission_rate, self.config.min_commission)
        tax = amount * self.config.sell_tax
        net_revenue = amount - commission - tax
        
        self.capital += net_revenue
        
        self.trades.append(Trade(
            stock_code=stock_code,
            action='sell',
            date=date,
            price=price,
            shares=pos.shares,
            amount=net_revenue,
            reason=reason
        ))
        
        del self.positions[stock_code]
    
    def _update_positions(
        self,
        current_date: str,
        processed_data: Dict[str, Dict],
        current_idx: int
    ):
        """
        更新持仓状态（收盘后）
        
        这里可以更新peak_price等状态，用于明天的卖出决策
        """
        for sc, pos in self.positions.items():
            if sc not in processed_data:
                continue
            
            data = processed_data[sc]
            if current_date not in data['date_to_idx']:
                continue
            
            idx = data['date_to_idx'][current_date]
            close_price = data['close'][idx]
            high_price = data['high'][idx]
            
            # 更新当前价格
            pos.current_price = close_price
            
            # 更新peak_price（用于追踪止盈）
            if high_price > pos.peak_price:
                pos.peak_price = high_price
            
            # 检查是否触发止盈条件（基于收盘价）
            gain_pct = (close_price - pos.entry_price) / pos.entry_price
            if gain_pct >= self.config.take_profit_pct:
                pos.triggered = True
    
    def _record_equity(
        self,
        current_date: str,
        processed_data: Dict[str, Dict],
        current_idx: int
    ):
        """记录权益"""
        position_value = 0.0
        
        for sc, pos in self.positions.items():
            if sc in processed_data and current_date in processed_data[sc]['date_to_idx']:
                idx = processed_data[sc]['date_to_idx'][current_date]
                close_price = processed_data[sc]['close'][idx]
                position_value += pos.shares * close_price
            else:
                position_value += pos.shares * pos.current_price
        
        total_equity = self.capital + position_value
        
        self.equity_curve.append({
            'date': current_date,
            'equity': total_equity,
            'cash': self.capital,
            'position_value': position_value,
            'position_count': len(self.positions)
        })
    
    def _liquidate_all(
        self,
        last_date: str,
        processed_data: Dict[str, Dict],
        last_idx: int
    ):
        """最后清仓"""
        for sc in list(self.positions.keys()):
            if sc in processed_data and last_date in processed_data[sc]['date_to_idx']:
                idx = processed_data[sc]['date_to_idx'][last_date]
                price = processed_data[sc]['close'][idx]
            else:
                price = self.positions[sc].current_price
            
            self._sell_stock(sc, last_date, price, 'liquidate')
    
    def _empty_result(self) -> Dict:
        """空结果"""
        return {
            'equity_curve': [],
            'trades': [],
            'total_return': 0.0,
            'annualized_return': 0.0,
            'max_drawdown': 0.0,
            'sharpe_ratio': 0.0,
            'win_rate': 0.0,
            'trade_count': 0
        }
    
    def _build_result(self) -> Dict:
        """构建回测结果"""
        if not self.equity_curve:
            return self._empty_result()
        
        equity_values = np.array([e['equity'] for e in self.equity_curve])
        
        # 计算总收益
        total_return = (equity_values[-1] - self.config.initial_capital) / self.config.initial_capital
        
        # 计算年化收益
        days = len(self.equity_curve)
        annualized_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0
        
        # 计算最大回撤
        max_drawdown = self._calc_max_drawdown(equity_values)
        
        # 计算夏普比率
        sharpe_ratio = self._calc_sharpe(equity_values)
        
        # 计算胜率
        sell_trades = [t for t in self.trades if t.action == 'sell']
        if sell_trades:
            profitable = sum(1 for t in sell_trades if 'profit' in t.reason or t.price > 0)
            win_rate = profitable / len(sell_trades)
        else:
            win_rate = 0.0
        
        return {
            'equity_curve': self.equity_curve,
            'trades': self.trades,
            'total_return': total_return * 100,
            'annualized_return': annualized_return * 100,
            'max_drawdown': max_drawdown * 100,
            'sharpe_ratio': sharpe_ratio,
            'win_rate': win_rate * 100,
            'trade_count': len(sell_trades),
            'final_equity': equity_values[-1]
        }
    
    @staticmethod
    def _calc_max_drawdown(equity: np.ndarray) -> float:
        """计算最大回撤"""
        peak = np.maximum.accumulate(equity)
        drawdown = (equity - peak) / peak
        return np.min(drawdown)
    
    @staticmethod
    def _calc_sharpe(equity: np.ndarray, risk_free_rate: float = 0.03) -> float:
        """计算夏普比率"""
        returns = np.diff(equity) / equity[:-1]
        if len(returns) < 2 or np.std(returns) == 0:
            return 0.0
        excess_returns = returns - risk_free_rate / 252
        return np.mean(excess_returns) / np.std(returns) * np.sqrt(252)


def validate_no_lookahead(data: Dict, current_date: str) -> bool:
    """
    验证数据是否包含未来信息
    
    用于调试，确保信号生成器没有使用未来数据
    """
    for sc, stock_data in data.items():
        dates = stock_data.get('dates', [])
        if len(dates) > 0:
            last_date = str(dates[-1])[:10]
            if last_date >= current_date:
                print(f"⚠️ 警告: {sc} 的数据包含未来日期 {last_date} (当前: {current_date})")
                return False
    return True
