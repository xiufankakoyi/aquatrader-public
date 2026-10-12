"""
三层策略系统

第一层：配置策略（最简单）- YAML 配置
第二层：函数策略（中等）- 简单 Python 函数，逐日调用
第三层：类策略（高级）- 向量化，完全控制

策略开发者可以根据需求选择合适的层级。
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Union, Callable, Tuple
from dataclasses import dataclass, field
from enum import Enum

from core.quant.strategies.vectorized_base import VectorizedStrategyBase
from core.quant.strategies.utils.signal_utils import crossover, crossunder, above, below


class SignalType(Enum):
    HOLD = 0
    BUY = 1
    SELL = 2


@dataclass
class DailyFactors:
    """单日因子数据"""
    date: str
    stock_code: str
    data: Dict[str, float]
    
    def __getitem__(self, key: str) -> float:
        return self.data.get(key, np.nan)
    
    def get(self, key: str, default: float = np.nan) -> float:
        return self.data.get(key, default)


@dataclass  
class Position:
    """持仓状态"""
    has_position: bool = False
    shares: int = 0
    cost_price: float = 0.0
    holding_days: int = 0
    unrealized_pnl: float = 0.0


class FunctionStrategy:
    """
    第二层：函数策略（中等难度）
    
    用户写一个简单的 Python 函数，接收当日数据和持仓状态，返回信号。
    引擎逐日调用该函数。
    
    Example:
        def my_strategy(date: str, factors: Dict[str, float], position: Position, history: List) -> Tuple[str, Optional[float]]:
            '''factors: 当日因子值，包含 ma5, ma10, volume 等'''
            if not position.has_position and factors['ma5'] > factors['ma10']:
                return 'buy', 0.1  # 买入10%仓位
            elif position.has_position and factors['ma5'] < factors['ma10']:
                return 'sell', None  # 全卖
            return 'hold', None
        
        strategy = FunctionStrategy(my_strategy, required_factors=['ma5', 'ma10'])
    """
    
    strategy_name: str = "函数策略"
    
    def __init__(
        self,
        signal_func: Callable[[str, Dict[str, float], Position, List], Tuple[str, Optional[float]]],
        required_factors: List[str] = None,
        name: str = None
    ):
        self.signal_func = signal_func
        self.required_factors = required_factors or []
        self.name = name or signal_func.__name__
        
        self.required_days = max(10, len(required_factors) + 5) if required_factors else 10
        self.execution_price = {"buy": "open", "sell": "open", "default": "open"}
        
        self._position_states: Dict[str, Position] = {}
        self._history: List[Dict] = []
    
    def generate_signals(
        self,
        current_date: str,
        stock_pool_today: Any,
        data_query
    ) -> Dict[str, str]:
        """
        逐日生成信号（兼容传统回测引擎）
        """
        signals = {}
        
        if stock_pool_today is None:
            return signals
        
        is_polars = hasattr(stock_pool_today, 'iter_rows')
        
        if is_polars:
            rows = stock_pool_today.iter_rows(named=True)
        else:
            rows = stock_pool_today.to_dict('records')
        
        for row in rows:
            stock_code = str(row.get('stock_code', ''))
            
            factors = {k: row.get(k, np.nan) for k in self.required_factors}
            factors['close'] = row.get('close', np.nan)
            factors['open'] = row.get('open', np.nan)
            factors['high'] = row.get('high', np.nan)
            factors['low'] = row.get('low', np.nan)
            factors['volume'] = row.get('volume', np.nan)
            
            position = self._position_states.get(stock_code, Position())
            
            try:
                action, ratio = self.signal_func(current_date, factors, position, self._history)
                
                if action == 'buy':
                    signals[stock_code] = 'buy'
                    self._position_states[stock_code] = Position(has_position=True)
                elif action == 'sell':
                    signals[stock_code] = 'sell'
                    self._position_states[stock_code] = Position(has_position=False)
                
                self._history.append({
                    'date': current_date,
                    'code': stock_code,
                    'action': action,
                    'factors': factors
                })
                
            except Exception as e:
                print(f"[FunctionStrategy] {stock_code} 信号生成失败: {e}")
        
        return signals
    
    def reset(self):
        """重置状态"""
        self._position_states.clear()
        self._history.clear()


class SimpleStrategy(VectorizedStrategyBase):
    """
    第三层简化版：声明式向量化策略
    
    策略开发者只需：
    1. 声明 required_factors
    2. 实现 _generate_signals
    
    Example:
        class MyStrategy(SimpleStrategy):
            required_factors = ['ma5', 'ma10']
            
            def _generate_signals(self, factors, trading_dates, stock_codes):
                ma5 = factors['ma5']
                ma10 = factors['ma10']
                
                golden = crossover(ma5, ma10)  # 金叉
                death = crossunder(ma5, ma10)  # 死叉
                
                signals = np.zeros(ma5.shape, dtype=int)
                signals[1:][golden] = 1  # buy
                signals[1:][death] = 2   # sell
                return signals
    """
    
    strategy_name: str = "声明式策略"
    
    def generate_signals_vectorized(
        self,
        price_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
        data_query,
        preloaded_data: Optional[Dict[str, Any]] = None
    ) -> np.ndarray:
        """
        向量化信号生成
        
        子类只需实现：
            def generate_signals_vectorized(self, factors, trading_dates, stock_codes):
                ...
        """
        self.prepare_data(preloaded_data, trading_dates, stock_codes, price_matrix)
        
        # 调用子类实现
        return self._generate_signals(self.factors, trading_dates, stock_codes)
    
    def _generate_signals(
        self,
        factors: Dict[str, np.ndarray],
        trading_dates: List[str],
        stock_codes: List[str]
    ) -> np.ndarray:
        """
        子类实现此方法
        
        Args:
            factors: 因子字典 {factor_name: matrix(T, N)}
            trading_dates: 交易日期列表
            stock_codes: 股票代码列表
        
        Returns:
            signals: 信号矩阵 (T, N), 0=hold, 1=buy, 2=sell
        """
        T, N = len(trading_dates), len(stock_codes)
        return np.zeros((T, N), dtype=np.int32)


# ============================================================================
# 预置策略模板
# ============================================================================

class DualMAStrategy(SimpleStrategy):
    """
    双均线策略模板

    Example:
        strategy = DualMAStrategy(fast=5, slow=10)
    """
    strategy_id = "dual_ma_template"
    required_factors = ['ma5', 'ma10']
    
    def __init__(self, fast: int = 5, slow: int = 10, name: str = None):
        super().__init__(name)
        self.fast = fast
        self.slow = slow
        self.required_factors = [f'ma{fast}', f'ma{slow}']
    
    def _generate_signals(self, factors, trading_dates, stock_codes):
        T, N = len(trading_dates), len(stock_codes)
        signals = np.zeros((T, N), dtype=np.int32)
        
        ma_fast = factors.get(f'ma{self.fast}')
        ma_slow = factors.get(f'ma{self.slow}')
        
        if ma_fast is None or ma_slow is None:
            return signals
        
        # 金叉买入
        golden = crossover(ma_fast, ma_slow)
        signals[1:][golden] = 1
        
        # 死叉卖出
        death = crossunder(ma_fast, ma_slow)
        signals[1:][death] = 2
        
        return signals


class RSIStrategy(SimpleStrategy):
    """
    RSI 策略模板

    Example:
        strategy = RSIStrategy(oversold=30, overbought=70)
    """
    strategy_id = "rsi_template"
    required_factors = ['rsi_14']
    
    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70, name: str = None):
        super().__init__(name)
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.required_factors = [f'rsi_{period}']
    
    def _generate_signals(self, factors, trading_dates, stock_codes):
        T, N = len(trading_dates), len(stock_codes)
        signals = np.zeros((T, N), dtype=np.int32)
        
        rsi = factors.get(f'rsi_{self.period}')
        if rsi is None:
            return signals
        
        # RSI 上穿超卖线买入
        oversold_signal = crossover(rsi, np.full_like(rsi, self.oversold))
        signals[1:][oversold_signal] = 1
        
        # RSI 下穿超买线卖出
        overbought_signal = crossunder(rsi, np.full_like(rsi, self.overbought))
        signals[1:][overbought_signal] = 2
        
        return signals


class MACDStrategy(SimpleStrategy):
    """
    MACD 策略模板

    Example:
        strategy = MACDStrategy()
    """
    strategy_id = "macd_template"
    required_factors = ['macd_dif', 'macd_dea']
    
    def _generate_signals(self, factors, trading_dates, stock_codes):
        T, N = len(trading_dates), len(stock_codes)
        signals = np.zeros((T, N), dtype=np.int32)
        
        dif = factors.get('macd_dif')
        dea = factors.get('macd_dea')
        
        if dif is None or dea is None:
            return signals
        
        # DIF 上穿 DEA 买入
        golden = crossover(dif, dea)
        signals[1:][golden] = 1
        
        # DIF 下穿 DEA 卖出
        death = crossunder(dif, dea)
        signals[1:][death] = 2
        
        return signals


class BollingerStrategy(SimpleStrategy):
    """
    布林带策略模板

    Example:
        strategy = BollingerStrategy()
    """
    strategy_id = "bollinger_template"
    required_factors = ['boll_upper', 'boll_lower', 'close']
    
    def _generate_signals(self, factors, trading_dates, stock_codes):
        T, N = len(trading_dates), len(stock_codes)
        signals = np.zeros((T, N), dtype=np.int32)
        
        upper = factors.get('boll_upper')
        lower = factors.get('boll_lower')
        close = factors.get('close')
        
        if upper is None or lower is None or close is None:
            return signals
        
        # 价格下穿下轨买入
        lower_cross = crossunder(close, lower)
        signals[1:][lower_cross] = 1
        
        # 价格上穿上轨卖出
        upper_cross = crossover(close, upper)
        signals[1:][upper_cross] = 2
        
        return signals
