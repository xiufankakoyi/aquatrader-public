# strategies/jq_volume_strategy_v2.py
"""
聚宽量比策略 - 因子库重构版

策略逻辑：
- 买入：市值20-60亿 + 量比>3 + 上市>60天 + 非ST
- 卖出：跌破MA5

代码量：从757行减少到150行（-80%）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import pandas as pd
import numpy as np

from core.quant.strategies.vectorized_base import VectorizedStrategyBase
from core.quant.strategies.utils import FactorLoader as FL
from config.config import Config


@dataclass(frozen=True)
class JQVolumeConfigpro:
    """聚宽策略参数配置"""
    warmup_days = 30
    max_positions = 5
    position_ratio = 0.2
    
    # 市值筛选（单位：万元）
    market_cap_min: float = field(
        default=20 * 10_000,
        metadata={"label": "最小市值", "group": "市值筛选", "optimize": True}
    )
    market_cap_max: float = field(
        default=60 * 10_000,
        metadata={"label": "最大市值", "group": "市值筛选", "optimize": True}
    )
    
    # 量比筛选
    volume_ratio_threshold: float = field(
        default=3.0,
        metadata={"label": "量比阈值", "group": "量能筛选", "optimize": True}
    )
    
    # 均线设置
    ma_days: int = field(
        default=5,
        metadata={"label": "均线天数", "group": "技术指标", "optimize": True}
    )
    
    # 基础过滤
    min_list_days: int = field(
        default=60,
        metadata={"label": "最小上市天数", "group": "股票筛选", "optimize": False}
    )
    
    # 仓位管理
    max_candidates: int = field(
        default=1500,
        metadata={"label": "最大候选数", "group": "股票筛选", "optimize": False}
    )
    position_ratio: float = field(
        default=0.2,
        metadata={"label": "仓位比例", "group": "仓位管理", "optimize": True}
    )
    max_stocks_per_day: int = field(
        default=5,
        metadata={"label": "每日最大买入数", "group": "仓位管理", "optimize": False}
    )


class JQVolumeStrategypro(VectorizedStrategyBase):
    """聚宽量比市值策略（因子库版本）"""
    
    strategy_id = "jq_volume_v1pro"
    strategy_name = "聚宽量比市值策略pro"
    
    def __init__(self, config: JQVolumeConfigpro | None = None):
        super().__init__(name=self.strategy_name)
        self.config = config or JQVolumeConfigpro()
        self.required_days = self.config.min_list_days
        self.position_ratio = self.config.position_ratio
        self.max_stocks_per_day = self.config.max_stocks_per_day
    
    # 属性代理：让优化器能修改 config
    @property
    def ma_days(self):
        return self.config.ma_days
    
    @ma_days.setter
    def ma_days(self, value):
        object.__setattr__(self.config, 'ma_days', int(value))
    
    @property
    def market_cap_min(self):
        return self.config.market_cap_min
    
    @market_cap_min.setter
    def market_cap_min(self, value):
        object.__setattr__(self.config, 'market_cap_min', float(value))
    
    @property
    def market_cap_max(self):
        return self.config.market_cap_max
    
    @market_cap_max.setter
    def market_cap_max(self, value):
        object.__setattr__(self.config, 'market_cap_max', float(value))
    
    @property
    def volume_ratio_threshold(self):
        return self.config.volume_ratio_threshold
    
    @volume_ratio_threshold.setter
    def volume_ratio_threshold(self, value):
        object.__setattr__(self.config, 'volume_ratio_threshold', float(value))
    
    @property
    def max_positions(self):
        return getattr(self.config, 'max_positions', 5)
    
    @max_positions.setter
    def max_positions(self, value):
        object.__setattr__(self.config, 'max_positions', int(value))
    
    @property
    def position_ratio(self):
        return getattr(self.config, 'position_ratio', 0.2)
    
    @position_ratio.setter
    def position_ratio(self, value):
        object.__setattr__(self.config, 'position_ratio', float(value))
    
    @property
    def max_stocks_per_day(self):
        return getattr(self.config, 'max_stocks_per_day', 5)
    
    @max_stocks_per_day.setter
    def max_stocks_per_day(self, value):
        object.__setattr__(self.config, 'max_stocks_per_day', int(value))
    
    def generate_signals_vectorized(
        self,
        price_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
        data_query,
        preloaded_data: Optional[Dict[str, pd.DataFrame]] = None
    ) -> np.ndarray:
        """
        向量化信号生成 - 因子库重构版
        
        代码量：从186行减少到45行（-75%）
        """
        T, N = len(trading_dates), len(stock_codes)
        signal_matrix = np.zeros((T, N), dtype=np.int32)
        
        if preloaded_data is None or len(preloaded_data) == 0:
            return signal_matrix
        
        # 1. 准备数据
        self.prepare_data(preloaded_data, trading_dates, stock_codes, price_matrix)
        
        # 2. 使用因子库获取所有因子（带缓存，只计算一次）
        import time
        import logging
        logger = logging.getLogger(__name__)
        
        start_time = time.time()
        
        # 数据库因子（直接读取）
        ma5 = FL.get_factor(f'ma{self.config.ma_days}', self)
        close_prices = FL.get_factor('close', self)
        volume_ratio = FL.get_factor('volume_ratio', self)
        total_mv = FL.get_factor('total_mv', self)
        is_st = FL.get_factor('is_st', self)
        days_listed = FL.get_factor('days_listed', self)
        volume = FL.get_factor('volume', self)
        amount = FL.get_factor('amount', self)
        
        # 计算型因子（带缓存，首次计算，后续直接返回缓存结果）
        gain_3d = FL.get_factor('gain_3d', self)
        turnover_ma5 = FL.get_factor('turnover_ma5', self)
        
        logger.debug(f"[Factor] All factors loaded in {time.time()-start_time:.3f}s")

        
        # 3. 纯粹的交易逻辑
        buy_condition = (
            (total_mv >= self.config.market_cap_min) &
            (total_mv <= self.config.market_cap_max) &
            (is_st == 0) &
            (days_listed >= self.required_days) &
            (volume_ratio > self.config.volume_ratio_threshold) &
            (volume > 0)
        )
        
        sell_condition = (
            (close_prices < ma5) &
            ~np.isnan(close_prices) &
            ~np.isnan(ma5)
        )
        
        # 4. 候选截断（按3日涨幅排序 + 换手率过滤）
        if self.config.max_candidates < N:
            daily_counts = np.sum(buy_condition, axis=1)
            days_to_prune = np.where(daily_counts > self.config.max_candidates)[0]
            
            for t in days_to_prune:
                if t < 3:
                    # 历史数据不足，按成交额排序
                    candidates_idx = np.where(buy_condition[t])[0]
                    amounts = amount[t, candidates_idx]
                    if len(amounts) > self.config.max_candidates:
                        keep_idx = candidates_idx[np.argpartition(amounts, -self.config.max_candidates)[-self.config.max_candidates:]]
                        buy_condition[t, :] = False
                        buy_condition[t, keep_idx] = True
                    continue
                
                candidates_idx = np.where(buy_condition[t])[0]
                gains_3d = gain_3d[t, candidates_idx]
                turnover_avg = turnover_ma5[t, candidates_idx] if t >= 5 else np.ones(len(candidates_idx))
                valid_mask = ~np.isnan(gains_3d) & (turnover_avg > 3.0)
                
                if np.sum(valid_mask) > self.config.max_candidates:
                    filtered_idx = candidates_idx[valid_mask]
                    sorted_idx = np.argsort(-gains_3d[valid_mask])[:self.config.max_candidates]
                    buy_condition[t, :] = False
                    buy_condition[t, filtered_idx[sorted_idx]] = True
                elif np.sum(valid_mask) > 0:
                    buy_condition[t, :] = False
                    buy_condition[t, candidates_idx[valid_mask]] = True
        
        # 5. 合成信号矩阵（T+1逻辑）
        raw_signal_matrix = np.zeros_like(signal_matrix)
        raw_signal_matrix[buy_condition] = 1
        raw_signal_matrix[sell_condition] = 2
        signal_matrix[1:] = raw_signal_matrix[:-1]
        
        return signal_matrix
