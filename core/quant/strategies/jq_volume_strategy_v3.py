"""
聚宽量比策略 - 声明式因子版本

策略逻辑：
- 买入：市值20-60亿 + 量比>3 + 上市>60天 + 非ST + 大阳线
- 卖出：跌破MA5

代码量对比：
- 原版：757 行
- v2 版：242 行
- 声明式版本：~80 行
"""
import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from core.quant.strategies.strategy_layers import SimpleStrategy
from core.quant.strategies.utils.signal_utils import below


@dataclass
class JQVolumeParams:
    """策略参数"""
    market_cap_min: float = 20_0000  # 20亿（万元）
    market_cap_max: float = 60_0000  # 60亿（万元）
    volume_ratio_threshold: float = 3.0
    ma_days: int = 5
    min_list_days: int = 60
    max_candidates: int = 1500
    position_ratio: float = 0.2
    max_stocks_per_day: int = 5


class JQVolumeStrategy(SimpleStrategy):
    """
    聚宽量比策略（声明式因子版本）
    
    使用方式：
        strategy = JQVolumeStrategy()
        
    或自定义参数：
        strategy = JQVolumeStrategy(
            market_cap_min=30_0000,
            volume_ratio_threshold=4.0
        )
    """
    
    strategy_name = "聚宽量比策略"
    required_factors = ['close', 'open', 'high', 'volume', 'amount',
                        'total_mv', 'volume_ratio', 'turnover_rate',
                        'is_st', 'ma5', 'days_listed']
    
    def __init__(self, **kwargs):
        super().__init__()
        self.params = JQVolumeParams(**kwargs)
        self.required_days = self.params.min_list_days
    
    def _generate_signals(
        self,
        factors: Dict[str, np.ndarray],
        trading_dates: List[str],
        stock_codes: List[str]
    ) -> np.ndarray:
        """
        向量化信号生成
        
        核心逻辑：
        1. 买入条件：市值 + 量比 + 上市天数 + 非ST + 大阳线
        2. 卖出条件：收盘价跌破 MA5
        """
        T, N = len(trading_dates), len(stock_codes)
        signals = np.zeros((T, N), dtype=np.int32)
        
        # 获取因子
        close = factors.get('close')
        open_p = factors.get('open')
        high = factors.get('high')
        total_mv = factors.get('total_mv')
        volume_ratio = factors.get('volume_ratio')
        is_st = factors.get('is_st')
        ma = factors.get(f'ma{self.params.ma_days}')
        days_listed = factors.get('days_listed')
        
        if close is None or ma is None:
            return signals
        
        # ========== 买入条件 ==========
        # 1. 市值筛选
        cap_ok = (
            (total_mv >= self.params.market_cap_min) &
            (total_mv <= self.params.market_cap_max)
        )
        
        # 2. 量比筛选
        volume_ok = volume_ratio > self.params.volume_ratio_threshold
        
        # 3. 基础过滤：非ST + 上市天数
        basic_ok = (is_st == 0) & (days_listed >= self.params.min_list_days)
        
        # 4. 大阳线：收盘价 >= 开盘价 * 1.03
        if open_p is not None:
            big_yang = close >= open_p * 1.03
        else:
            big_yang = np.ones((T, N), dtype=bool)
        
        # 5. 排除冲高回落：(high - close) / close <= 0.03
        if high is not None:
            no_pullback = (high - close) / np.maximum(close, 1e-6) <= 0.03
        else:
            no_pullback = np.ones((T, N), dtype=bool)
        
        # 合并买入条件
        buy_condition = cap_ok & volume_ok & basic_ok & big_yang & no_pullback
        
        # ========== 卖出条件 ==========
        # 收盘价跌破 MA5
        sell_condition = below(close, ma) & ~np.isnan(close) & ~np.isnan(ma)
        
        # ========== 生成信号 ==========
        # T+1 执行：今天产生信号，明天执行
        signals[1:][buy_condition[:-1]] = 1  # buy
        signals[1:][sell_condition[:-1]] = 2  # sell
        
        return signals


# ============================================================================
# 函数策略版本（第二层）- 更简单
# ============================================================================

def jq_volume_func(date: str, factors: dict, position, history) -> tuple:
    """
    聚宽量比策略 - 函数版本
    
    只需 15 行核心逻辑！
    """
    # 买入条件
    if not position.has_position:
        cap_ok = 20_0000 <= factors['total_mv'] <= 60_0000
        volume_ok = factors['volume_ratio'] > 3.0
        not_st = factors['is_st'] == 0
        listed_ok = factors['days_listed'] >= 60
        big_yang = factors['close'] >= factors['open'] * 1.03
        
        if cap_ok and volume_ok and not_st and listed_ok and big_yang:
            return 'buy', 0.2
    
    # 卖出条件
    else:
        if factors['close'] < factors['ma5']:
            return 'sell', None
    
    return 'hold', None


# 使用方式
# from core.quant.strategies.strategy_layers import FunctionStrategy
# strategy = FunctionStrategy(jq_volume_func, required_factors=[
#     'close', 'open', 'total_mv', 'volume_ratio', 'is_st', 'ma5', 'days_listed'
# ])
