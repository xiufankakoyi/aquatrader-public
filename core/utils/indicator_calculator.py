# utils/indicator_calculator.py
"""
动态指标计算层 - 解决"数据预计算僵化"问题

设计理念：
- 指标在内存中动态计算，而不是预存在数据库中
- 支持任意指标（MA、EMA、RSI、MACD等），无需修改数据库schema
- 利用 NumPy/Pandas 向量化，计算速度远快于 IO 读取速度
- 支持缓存机制，避免重复计算

性能优化：
- 优先使用 Polars 零拷贝版本（快 3-10 倍）
- 自动检测输入类型，选择最优计算引擎

使用示例：
    calculator = get_indicator_calculator()
    
    # 计算 MA20
    ma20 = calculator.calculate_ma(df, column='close', window=20)
    
    # 计算 EMA12
    ema12 = calculator.calculate_ema(df, column='close', window=12)
    
    # 批量计算多个指标
    indicators = calculator.calculate_batch(df, [
        {'type': 'ma', 'column': 'close', 'window': 5},
        {'type': 'ma', 'column': 'close', 'window': 20},
        {'type': 'ema', 'column': 'close', 'window': 12},
        {'type': 'rsi', 'column': 'close', 'window': 14},
    ])
"""

import pandas as pd
import numpy as np
import polars as pl
from typing import Dict, List, Optional, Union, Any
from functools import lru_cache
import hashlib
import os

USE_POLARS = os.getenv('USE_POLARS', '1') not in ('0', 'false', 'False')


class IndicatorCalculator:
    """
    动态指标计算器
    
    核心功能：
    1. 支持常见技术指标（MA、EMA、RSI、MACD、BOLL等）
    2. 向量化计算，性能优异
    3. 支持缓存，避免重复计算
    4. 支持按股票分组计算（groupby）
    """
    
    def __init__(self, enable_cache: bool = True, cache_size: int = 128):
        """
        参数：
            enable_cache: 是否启用缓存
            cache_size: 缓存大小（LRU缓存的最大条目数）
        """
        self.enable_cache = enable_cache
        self._cache = {}
    
    def calculate_ma(
        self, 
        df: pd.DataFrame, 
        column: str = 'close',
        window: int = 20,
        min_periods: Optional[int] = None,
        group_by: Optional[str] = None
    ) -> pd.Series:
        """
        计算移动平均线（MA）
        
        参数：
            df: DataFrame，必须包含 column 列
            column: 要计算的列名
            window: 窗口大小
            min_periods: 最小周期数（默认等于window）
            group_by: 如果指定，按此列分组计算（例如 'stock_code'）
        
        返回：
            pd.Series，与 df 的 index 对齐
        """
        if min_periods is None:
            min_periods = window
        
        if group_by:
            return df.groupby(group_by)[column].transform(
                lambda x: x.rolling(window=window, min_periods=min_periods).mean()
            )
        else:
            return df[column].rolling(window=window, min_periods=min_periods).mean()
    
    def calculate_ema(
        self,
        df: pd.DataFrame,
        column: str = 'close',
        window: int = 12,
        group_by: Optional[str] = None
    ) -> pd.Series:
        """
        计算指数移动平均线（EMA）
        
        参数：
            df: DataFrame
            column: 要计算的列名
            window: 窗口大小（半衰期）
            group_by: 分组列名
        
        返回：
            pd.Series
        """
        if group_by:
            return df.groupby(group_by)[column].transform(
                lambda x: x.ewm(span=window, adjust=False).mean()
            )
        else:
            return df[column].ewm(span=window, adjust=False).mean()
    
    def calculate_rsi(
        self,
        df: pd.DataFrame,
        column: str = 'close',
        window: int = 14,
        group_by: Optional[str] = None
    ) -> pd.Series:
        """
        计算相对强弱指标（RSI）
        
        参数：
            df: DataFrame
            column: 价格列名
            window: 周期（默认14）
            group_by: 分组列名
        
        返回：
            pd.Series，值范围 0-100
        """
        def _rsi(series: pd.Series) -> pd.Series:
            delta = series.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            return rsi.fillna(50)  # 初始值设为50（中性）
        
        if group_by:
            return df.groupby(group_by)[column].transform(_rsi)
        else:
            return _rsi(df[column])
    
    def calculate_macd(
        self,
        df: pd.DataFrame,
        column: str = 'close',
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        group_by: Optional[str] = None
    ) -> pd.DataFrame:
        """
        计算MACD指标
        
        返回：
            pd.DataFrame with columns: ['macd', 'signal', 'histogram']
        """
        def _macd(series: pd.Series) -> pd.DataFrame:
            ema_fast = series.ewm(span=fast, adjust=False).mean()
            ema_slow = series.ewm(span=slow, adjust=False).mean()
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.ewm(span=signal, adjust=False).mean()
            histogram = macd_line - signal_line
            
            return pd.DataFrame({
                'macd': macd_line,
                'signal': signal_line,
                'histogram': histogram
            })
        
        if group_by:
            grouped = df.groupby(group_by, sort=False)[column]
            ema_fast = grouped.transform(lambda series: series.ewm(span=fast, adjust=False).mean())
            ema_slow = grouped.transform(lambda series: series.ewm(span=slow, adjust=False).mean())
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.groupby(df[group_by], sort=False).transform(
                lambda series: series.ewm(span=signal, adjust=False).mean()
            )
            return pd.DataFrame(
                {
                    'macd': macd_line.to_numpy(),
                    'signal': signal_line.to_numpy(),
                    'histogram': (macd_line - signal_line).to_numpy(),
                },
                index=df.index,
            )
        else:
            return _macd(df[column])
    
    def calculate_bollinger_bands(
        self,
        df: pd.DataFrame,
        column: str = 'close',
        window: int = 20,
        num_std: float = 2.0,
        group_by: Optional[str] = None
    ) -> pd.DataFrame:
        """
        计算布林带（Bollinger Bands）
        
        返回：
            pd.DataFrame with columns: ['upper', 'middle', 'lower']
        """
        def _bollinger(series: pd.Series) -> pd.DataFrame:
            middle = series.rolling(window=window).mean()
            std = series.rolling(window=window).std()
            upper = middle + (std * num_std)
            lower = middle - (std * num_std)
            
            return pd.DataFrame({
                'upper': upper,
                'middle': middle,
                'lower': lower
            })
        
        if group_by:
            grouped = df.groupby(group_by, sort=False)[column]
            middle = grouped.transform(lambda series: series.rolling(window=window).mean())
            std = grouped.transform(lambda series: series.rolling(window=window).std())
            return pd.DataFrame(
                {
                    'upper': (middle + (std * num_std)).to_numpy(),
                    'middle': middle.to_numpy(),
                    'lower': (middle - (std * num_std)).to_numpy(),
                },
                index=df.index,
            )
        else:
            return _bollinger(df[column])
    
    def calculate_atr(
        self,
        df: pd.DataFrame,
        window: int = 14,
        group_by: Optional[str] = None
    ) -> pd.Series:
        """
        计算平均真实波幅（ATR）
        
        参数：
            df: 必须包含 'high', 'low', 'close' 列
            window: 周期
            group_by: 分组列名
        
        返回：
            pd.Series
        """
        def _atr(group: pd.DataFrame) -> pd.Series:
            high_low = group['high'] - group['low']
            high_close = np.abs(group['high'] - group['close'].shift())
            low_close = np.abs(group['low'] - group['close'].shift())
            
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            atr = tr.rolling(window=window).mean()
            
            return atr
        
        if group_by:
            return df.groupby(group_by).apply(_atr).reset_index(level=0, drop=True)
        else:
            return _atr(df)
    
    def calculate_batch(
        self,
        df: pd.DataFrame,
        indicators: List[Dict[str, Any]],
        group_by: Optional[str] = None
    ) -> pd.DataFrame:
        """
        批量计算多个指标
        
        参数：
            df: DataFrame
            indicators: 指标配置列表，每个元素为字典，例如：
                [
                    {'type': 'ma', 'column': 'close', 'window': 5, 'name': 'ma5'},
                    {'type': 'ma', 'column': 'close', 'window': 20, 'name': 'ma20'},
                    {'type': 'ema', 'column': 'close', 'window': 12, 'name': 'ema12'},
                    {'type': 'rsi', 'column': 'close', 'window': 14, 'name': 'rsi14'},
                ]
            group_by: 分组列名
        
        返回：
            pd.DataFrame，包含所有计算的指标列
        """
        result_df = df.copy()
        
        for indicator_config in indicators:
            indicator_type = indicator_config.get('type', '').lower()
            column_name = indicator_config.get('name') or f"{indicator_type}_{indicator_config.get('window', '')}"
            
            if indicator_type == 'ma':
                values = self.calculate_ma(
                    df,
                    column=indicator_config.get('column', 'close'),
                    window=indicator_config.get('window', 20),
                    group_by=group_by
                )
                result_df[column_name] = values
            
            elif indicator_type == 'ema':
                values = self.calculate_ema(
                    df,
                    column=indicator_config.get('column', 'close'),
                    window=indicator_config.get('window', 12),
                    group_by=group_by
                )
                result_df[column_name] = values
            
            elif indicator_type == 'rsi':
                values = self.calculate_rsi(
                    df,
                    column=indicator_config.get('column', 'close'),
                    window=indicator_config.get('window', 14),
                    group_by=group_by
                )
                result_df[column_name] = values
            
            elif indicator_type == 'macd':
                macd_df = self.calculate_macd(
                    df,
                    column=indicator_config.get('column', 'close'),
                    fast=indicator_config.get('fast', 12),
                    slow=indicator_config.get('slow', 26),
                    signal=indicator_config.get('signal', 9),
                    group_by=group_by
                )
                prefix = indicator_config.get('name', 'macd')
                result_df[f'{prefix}_macd'] = macd_df['macd']
                result_df[f'{prefix}_signal'] = macd_df['signal']
                result_df[f'{prefix}_histogram'] = macd_df['histogram']
            
            elif indicator_type == 'bollinger' or indicator_type == 'bb':
                bb_df = self.calculate_bollinger_bands(
                    df,
                    column=indicator_config.get('column', 'close'),
                    window=indicator_config.get('window', 20),
                    num_std=indicator_config.get('num_std', 2.0),
                    group_by=group_by
                )
                prefix = indicator_config.get('name', 'bb')
                result_df[f'{prefix}_upper'] = bb_df['upper']
                result_df[f'{prefix}_middle'] = bb_df['middle']
                result_df[f'{prefix}_lower'] = bb_df['lower']
            
            elif indicator_type == 'atr':
                values = self.calculate_atr(
                    df,
                    window=indicator_config.get('window', 14),
                    group_by=group_by
                )
                result_df[column_name] = values
            
            else:
                print(f"[WARN] 未知的指标类型: {indicator_type}")
        
        return result_df
    
    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()


def get_indicator_calculator(
    use_polars: Optional[bool] = None,
    enable_cache: bool = True
) -> Union['IndicatorCalculator', 'PolarsIndicatorCalculator']:
    """
    获取指标计算器（自动选择最优引擎）
    
    参数：
        use_polars: 是否使用 Polars 引擎，None 表示自动选择
        enable_cache: 是否启用缓存
        
    返回：
        IndicatorCalculator (Pandas) 或 PolarsIndicatorCalculator (Polars)
        
    性能说明：
        - Polars 引擎：快 3-10 倍，零拷贝，适合大数据集
        - Pandas 引擎：兼容性好，适合小数据集或需要 Pandas 特定功能
    """
    if use_polars is None:
        use_polars = USE_POLARS
    
    if use_polars:
        try:
            from core.utils.indicator_calculator_polars import PolarsIndicatorCalculator
            return PolarsIndicatorCalculator(enable_cache=enable_cache)
        except ImportError:
            pass
    
    return IndicatorCalculator(enable_cache=enable_cache, cache_size=128)









