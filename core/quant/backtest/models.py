"""
回测领域模型与通用工具

被 UnifiedBacktestEngine 和各功能层共享：
- 配置/枚举/数据类
- 通用序列化工具
- Numba 加速的风险指标计算函数
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

# 可选依赖导入
try:
    import polars as pl
    POLARS_AVAILABLE = True
except ImportError:
    POLARS_AVAILABLE = False
    pl = None

try:
    import numba
    from numba import jit, njit
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    njit = lambda *args, **kwargs: lambda f: f


# =============================================================================
# Numba 加速函数
# =============================================================================
if NUMBA_AVAILABLE:
    @njit(cache=True, fastmath=True)
    def _calculate_drawdown_numba(equity_values: np.ndarray) -> float:
        """
        Numba 加速的最大回撤计算

        Args:
            equity_values: 权益曲线数组

        Returns:
            float: 最大回撤百分比（负数）
        """
        n = len(equity_values)
        if n == 0:
            return 0.0

        peak = equity_values[0]
        max_dd = 0.0

        for i in range(n):
            if equity_values[i] > peak:
                peak = equity_values[i]
            if peak > 0:
                dd = (equity_values[i] - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd

        return max_dd

    @njit(cache=True, fastmath=True)
    def _calculate_sharpe_numba(returns: np.ndarray, annual_factor: float = 252.0) -> float:
        """
        Numba 加速的夏普比率计算

        Args:
            returns: 日收益率数组
            annual_factor: 年化因子

        Returns:
            float: 夏普比率
        """
        n = len(returns)
        if n < 2:
            return 0.0

        mean_ret = 0.0
        for i in range(n):
            mean_ret += returns[i]
        mean_ret /= n

        var = 0.0
        for i in range(n):
            diff = returns[i] - mean_ret
            var += diff * diff
        var /= (n - 1)

        std_ret = np.sqrt(var)
        if std_ret <= 0:
            return 0.0

        return (mean_ret / std_ret) * np.sqrt(annual_factor)

    @njit(cache=True, fastmath=True)
    def _calculate_sortino_numba(returns: np.ndarray, annual_factor: float = 252.0) -> float:
        """
        Numba 加速的索提诺比率计算

        Args:
            returns: 日收益率数组
            annual_factor: 年化因子

        Returns:
            float: 索提诺比率
        """
        n = len(returns)
        if n < 2:
            return 0.0

        mean_ret = 0.0
        for i in range(n):
            mean_ret += returns[i]
        mean_ret /= n

        downside_count = 0
        downside_var = 0.0
        for i in range(n):
            if returns[i] < 0:
                diff = returns[i] - mean_ret
                downside_var += diff * diff
                downside_count += 1

        if downside_count < 1:
            return 0.0

        downside_var /= downside_count
        downside_std = np.sqrt(downside_var)

        if downside_std <= 0:
            return 0.0

        return (mean_ret / downside_std) * np.sqrt(annual_factor)

    @njit(cache=True, fastmath=True)
    def _calculate_streaks_numba(profit_signs: np.ndarray) -> Tuple[int, int]:
        """
        Numba 加速的连胜/连亏计算

        Args:
            profit_signs: 盈亏符号数组 (1=盈利, -1=亏损, 0=持平)

        Returns:
            Tuple[int, int]: (最大连胜次数, 最大连亏次数)
        """
        n = len(profit_signs)
        if n == 0:
            return 0, 0

        max_win = 0
        max_loss = 0
        cur_win = 0
        cur_loss = 0

        for i in range(n):
            if profit_signs[i] > 0:
                cur_win += 1
                cur_loss = 0
                if cur_win > max_win:
                    max_win = cur_win
            elif profit_signs[i] < 0:
                cur_loss += 1
                cur_win = 0
                if cur_loss > max_loss:
                    max_loss = cur_loss

        return max_win, max_loss

else:
    def _calculate_drawdown_numba(equity_values: np.ndarray) -> float:
        """回退实现：最大回撤计算"""
        if len(equity_values) == 0:
            return 0.0
        peak = np.maximum.accumulate(equity_values)
        with np.errstate(divide='ignore', invalid='ignore'):
            drawdowns = np.where(peak > 0, (equity_values - peak) / peak * 100, 0)
        return float(np.min(drawdowns))

    def _calculate_sharpe_numba(returns: np.ndarray, annual_factor: float = 252.0) -> float:
        """回退实现：夏普比率计算"""
        if len(returns) < 2:
            return 0.0
        std_ret = np.std(returns)
        if std_ret <= 0:
            return 0.0
        return float((np.mean(returns) / std_ret) * np.sqrt(annual_factor))

    def _calculate_sortino_numba(returns: np.ndarray, annual_factor: float = 252.0) -> float:
        """回退实现：索提诺比率计算"""
        if len(returns) < 2:
            return 0.0
        downside_returns = returns[returns < 0]
        if len(downside_returns) < 1:
            return 0.0
        downside_std = np.std(downside_returns)
        if downside_std <= 0:
            return 0.0
        return float((np.mean(returns) / downside_std) * np.sqrt(annual_factor))

    def _calculate_streaks_numba(profit_signs: np.ndarray) -> Tuple[int, int]:
        """回退实现：连胜/连亏计算"""
        max_win = max_loss = cur_win = cur_loss = 0
        for sign in profit_signs:
            if sign > 0:
                cur_win += 1
                cur_loss = 0
                max_win = max(max_win, cur_win)
            elif sign < 0:
                cur_loss += 1
                cur_win = 0
                max_loss = max(max_loss, cur_loss)
        return max_win, max_loss


# =============================================================================
# 枚举与配置
# =============================================================================
class TimeGranularity(Enum):
    """时间粒度枚举"""
    DAILY = "daily"
    MINUTE = "minute"
    TICK = "tick"


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 1_000_000.0
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    sell_tax: float = 0.001  # 印花税
    time_granularity: TimeGranularity = TimeGranularity.DAILY

    # 风控参数
    max_positions: int = None
    position_ratio: float = 0.1
    max_stocks_per_day: int = None  # 单日最大买入股票数
    stop_loss: float = None
    take_profit: float = None  # 止盈比例
    max_holding_days: int = None

    # 执行参数
    warmup_days: int = 30  # 预热期天数
    execution_price: str = "open"  # 执行价格: open/close

    # 回测真实性参数
    slippage_rate: float = 0.001  # 0.1%，A股市场常见水平
    exclude_st: bool = True  # 是否启用 ST 股票过滤
    volume_cap_ratio: float = 0.05  # 单笔不超过日成交量 5%
    min_trade_amount: float = 1000.0  # 最小可买入金额


@dataclass
class RejectedOrderStat:
    """不可成交订单统计"""
    suspended: int = 0       # 停牌
    limit_up: int = 0        # 涨停未开板
    limit_down: int = 0      # 跌停
    st: int = 0              # ST 股
    volume: int = 0          # 成交量不足
    insufficient_cash: int = 0  # 现金不足
    invalid_price: int = 0   # 价格无效
    other: int = 0

    def total(self) -> int:
        return (
            self.suspended + self.limit_up + self.limit_down
            + self.st + self.volume + self.insufficient_cash
            + self.invalid_price + self.other
        )

    def to_dict(self) -> Dict[str, int]:
        return {
            "suspended": self.suspended,
            "limitUp": self.limit_up,
            "limitDown": self.limit_down,
            "st": self.st,
            "volume": self.volume,
            "insufficientCash": self.insufficient_cash,
            "invalidPrice": self.invalid_price,
            "other": self.other,
            "total": self.total(),
        }


@dataclass
class TradeRecord:
    """
    交易记录

    字段说明：
    - date: 交易日期
    - code: 股票代码（6位数字）
    - action: 交易方向 (buy/sell)
    - shares: 成交股数
    - price: 成交价格（前复权）
    - amount: 成交金额
    - commission: 佣金
    - tax: 印花税（仅卖出）
    - profit_loss: 盈亏金额（仅卖出）
    - roi: 收益率（仅卖出）
    - entry_price: 开仓价格
    - entry_date: 开仓日期
    - exit_price: 平仓价格（仅卖出）
    - exit_date: 平仓日期（仅卖出）
    - holding_days: 持仓天数
    - position_id: 持仓ID（用于买卖配对）
    - indicators: 策略指标快照
    """
    date: str
    code: str
    action: str  # buy/sell
    shares: int
    price: float
    amount: float
    commission: float
    tax: float = 0.0
    profit_loss: float = 0.0
    roi: float = 0.0
    entry_price: float = 0.0
    entry_date: str = ""
    exit_price: float = 0.0
    exit_date: str = ""
    holding_days: int = 0
    position_id: str = ""
    indicators: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestResult:
    """回测结果"""
    final_equity: float
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    volatility: float
    win_rate: float
    profit_factor: float
    trade_count: int
    avg_trade_return: float
    max_winning_streak: int
    max_losing_streak: int
    calmar_ratio: float
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[TradeRecord] = field(default_factory=list)
    rejected_orders: Dict[str, int] = field(default_factory=dict)
    slippage_cost: float = 0.0
    filter_stats: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# 通用工具
# =============================================================================
def _make_json_serializable(obj: Any, max_depth: int = 10, _current_depth: int = 0) -> Any:
    """递归将对象转换为 JSON 可序列化的类型"""
    if _current_depth > max_depth:
        return None

    if obj is None or isinstance(obj, bool):
        return obj
    elif isinstance(obj, (int, float)):
        if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
            return None
        elif hasattr(obj, 'item'):
            try:
                val = obj.item()
                if isinstance(val, float) and (math.isinf(val) or math.isnan(val)):
                    return None
            except Exception:
                pass
        return obj
    elif isinstance(obj, str):
        return obj

    if hasattr(obj, 'tolist'):
        try:
            return obj.tolist()
        except (AttributeError, ValueError):
            pass
    elif isinstance(obj, np.generic):
        try:
            return float(obj) if isinstance(obj, (np.floating, np.integer)) else str(obj)
        except (ValueError, TypeError):
            return None

    if isinstance(obj, pd.DataFrame):
        return _make_json_serializable(obj.to_dict('records'), max_depth, _current_depth)
    if isinstance(obj, pd.Series):
        return _make_json_serializable(obj.to_dict(), max_depth, _current_depth)

    if isinstance(obj, (pd.Timestamp, pd.DatetimeIndex)):
        try:
            return obj.isoformat()
        except (AttributeError, ValueError):
            return str(obj)

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()

    if isinstance(obj, (list, tuple)):
        result = []
        for item in obj:
            try:
                result.append(_make_json_serializable(item, max_depth, _current_depth + 1))
            except Exception:
                result.append(str(item))
        return result if isinstance(obj, list) else tuple(result)

    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            try:
                key = _make_json_serializable(k, max_depth, _current_depth + 1) if not isinstance(k, (str, int, float, bool)) else k
                if key is not None:
                    result[key] = _make_json_serializable(v, max_depth, _current_depth + 1)
            except Exception:
                pass
        return result

    if isinstance(obj, (set, frozenset)):
        try:
            sorted_items = sorted(_make_json_serializable(list(obj), max_depth, _current_depth + 1))
            return sorted_items
        except (TypeError, ValueError):
            return list(obj)

    if hasattr(obj, '__dict__'):
        try:
            result = {}
            for k, v in obj.__dict__.items():
                if k.startswith('_'):
                    continue
                try:
                    result[k] = _make_json_serializable(v, max_depth, _current_depth + 1)
                except Exception:
                    pass
            return result
        except Exception:
            pass

    try:
        return str(obj)
    except Exception:
        return None


def _trade_to_dict(trade: TradeRecord) -> Dict[str, Any]:
    """转换交易记录为字典"""
    stock_code = str(trade.code).zfill(6) if trade.code.isdigit() else trade.code
    return {
        "id": f"{trade.date}_{stock_code}_{trade.action}",
        "date": trade.date,
        "symbol": stock_code,
        "symbolCode": stock_code,
        "code": stock_code,
        "action": trade.action,
        "price": trade.price,
        "quantity": trade.shares,
        "shares": trade.shares,
        "commission": trade.commission,
        "cost": trade.amount if trade.action == 'buy' else 0,
        "revenue": trade.amount if trade.action == 'sell' else 0,
        "profitLoss": trade.profit_loss,
        "profit_loss": trade.profit_loss,
        "roi": trade.roi,
        "entry_price": trade.entry_price,
        "holdingDays": trade.holding_days
    }
