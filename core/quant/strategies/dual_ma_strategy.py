"""
双均线校准策略 - 声明式因子版本

改进点：
1. 代码量：130行 -> 30行
2. 性能：逐日查询 -> 向量化一次性计算
3. 因子：手动计算 -> 声明式自动注入
"""
import numpy as np
from typing import Dict, List, Any, Optional
from core.quant.strategies.vectorized_base import VectorizedStrategyBase


class DualMAStrategy(VectorizedStrategyBase):
    """
    双均线策略（声明式因子版本）
    
    策略逻辑：
    - 金叉（MA5上穿MA10）：买入
    - 死叉（MA5下穿MA10）：卖出
    
    使用方式：
        strategy = DualMAStrategy(
            fast_window=5,
            slow_window=10,
            target_code='601988'
        )
    """
    
    strategy_name = "双均线策略"
    required_factors = ['ma5', 'ma10']
    
    def __init__(
        self,
        name: str = None,
        fast_window: int = 5,
        slow_window: int = 10,
        target_code: str = None,
        position_ratio: float = 0.95
    ):
        super().__init__(name)
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.target_code = target_code
        self.position_ratio = position_ratio
        
        self.required_days = max(fast_window, slow_window) + 5
        self.execution_price = {"buy": "open", "sell": "open", "default": "open"}
    
    def generate_signals_vectorized(
        self,
        price_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
        data_query,
        preloaded_data: Optional[Dict[str, Any]] = None,
        price_matrix_adj: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """
        向量化信号生成
        
        Args:
            price_matrix: (T, N, 4) [open, high, low, close] - 不复权价格（用于交易）
            trading_dates: 交易日期列表
            stock_codes: 股票代码列表
            data_query: 数据查询对象
            preloaded_data: 预加载数据
            price_matrix_adj: (T, N, 4) 前复权价格（用于指标计算），可选
        
        Returns:
            signal_matrix: (T, N) int32 - 0=hold, 1=buy, 2=sell
        """
        # 使用复权价格计算指标（如果提供），否则使用不复权价格
        price_for_calc = price_matrix_adj if price_matrix_adj is not None else price_matrix
        self.prepare_data(preloaded_data, trading_dates, stock_codes, price_for_calc)
        
        T, N = len(trading_dates), len(stock_codes)
        signals = np.zeros((T, N), dtype=np.int32)
        
        # 获取因子（已自动注入）
        ma_fast = self.factors.get(f'ma{self.fast_window}')
        ma_slow = self.factors.get(f'ma{self.slow_window}')
        
        # 如果因子不存在，从 close 计算
        if ma_fast is None:
            ma_fast = self._calc_ma(self.close, self.fast_window)
        if ma_slow is None:
            ma_slow = self._calc_ma(self.close, self.slow_window)
        
        if ma_fast is None or ma_slow is None:
            return signals
        
        # 目标股票过滤
        if self.target_code:
            if self.target_code not in stock_codes:
                return signals
            target_idx = stock_codes.index(self.target_code)
            target_mask = np.zeros(N, dtype=bool)
            target_mask[target_idx] = True
        else:
            target_mask = np.ones(N, dtype=bool)
        
        # 金叉检测：T-1日 fast <= slow，T日 fast > slow
        # 死叉检测：T-1日 fast >= slow，T日 fast < slow
        golden_cross = (
            (ma_fast[:-1] <= ma_slow[:-1]) & 
            (ma_fast[1:] > ma_slow[1:]) &
            target_mask[None, :]
        )
        death_cross = (
            (ma_fast[:-1] >= ma_slow[:-1]) & 
            (ma_fast[1:] < ma_slow[1:]) &
            target_mask[None, :]
        )
        
        # 设置信号（从第2天开始，因为需要比较T-1和T）
        signals[1:][golden_cross] = 1  # buy
        signals[1:][death_cross] = 2   # sell
        
        return signals
    
    @staticmethod
    def _calc_ma(close: np.ndarray, window: int) -> np.ndarray:
        """计算移动平均"""
        T, N = close.shape
        ma = np.full((T, N), np.nan, dtype=np.float32)
        for t in range(window - 1, T):
            ma[t] = np.nanmean(close[t-window+1:t+1], axis=0)
        return ma


class MAStrategyV2(VectorizedStrategyBase):
    """
    通用均线策略 - 支持多因子组合
    
    Example:
        strategy = MAStrategyV2(
            required_factors=['ma5', 'ma10', 'ma20'],
            buy_condition='ma5 > ma10 and ma10 > ma20',
            sell_condition='ma5 < ma10'
        )
    """
    
    strategy_name = "通用均线策略"
    
    def __init__(
        self,
        name: str = None,
        fast_window: int = 5,
        slow_window: int = 20,
        position_ratio: float = 0.1,
        max_positions: int = 10
    ):
        super().__init__(name)
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.position_ratio = position_ratio
        self.max_positions = max_positions
        
        # 动态设置 required_factors
        self.required_factors = [f'ma{fast_window}', f'ma{slow_window}']
        
        self.required_days = max(fast_window, slow_window) + 5
    
    def generate_signals_vectorized(
        self,
        price_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
        data_query,
        preloaded_data: Optional[Dict[str, Any]] = None
    ) -> np.ndarray:
        self.prepare_data(preloaded_data, trading_dates, stock_codes, price_matrix)
        
        T, N = len(trading_dates), len(stock_codes)
        signals = np.zeros((T, N), dtype=np.int32)
        
        ma_fast = self.factors.get(f'ma{self.fast_window}')
        ma_slow = self.factors.get(f'ma{self.slow_window}')
        
        if ma_fast is None:
            ma_fast = self._calc_ma(self.close, self.fast_window)
        if ma_slow is None:
            ma_slow = self._calc_ma(self.close, self.slow_window)
        
        if ma_fast is None or ma_slow is None:
            return signals
        
        # 金叉买入
        golden_cross = (ma_fast[:-1] <= ma_slow[:-1]) & (ma_fast[1:] > ma_slow[1:])
        signals[1:][golden_cross] = 1
        
        # 死叉卖出
        death_cross = (ma_fast[:-1] >= ma_slow[:-1]) & (ma_fast[1:] < ma_slow[1:])
        signals[1:][death_cross] = 2
        
        return signals
    
    @staticmethod
    def _calc_ma(close: np.ndarray, window: int) -> np.ndarray:
        T, N = close.shape
        ma = np.full((T, N), np.nan, dtype=np.float32)
        for t in range(window - 1, T):
            ma[t] = np.nanmean(close[t-window+1:t+1], axis=0)
        return ma
