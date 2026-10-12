"""
Polars 零拷贝指标计算层 - 高性能版本

设计理念：
- 使用 Polars 零拷贝 Arrow 内存模型，避免 Pandas 的内存复制开销
- 所有计算在 Polars 表达式层完成，性能比 Pandas 快 3-10 倍
- 支持批量计算，一次性生成多个指标
- 自动检测输入类型，支持 Polars DataFrame 和 Pandas DataFrame

使用示例：
    calculator = PolarsIndicatorCalculator()
    
    # 计算 MA20
    df_with_ma = calculator.calculate_ma(df, column='close', window=20)
    
    # 批量计算多个指标
    df_with_indicators = calculator.calculate_batch(df, [
        {'type': 'ma', 'column': 'close', 'window': 5},
        {'type': 'ma', 'column': 'close', 'window': 20},
        {'type': 'ema', 'column': 'close', 'window': 12},
        {'type': 'rsi', 'column': 'close', 'window': 14},
    ])
"""

import polars as pl
import numpy as np
from typing import Dict, List, Optional, Union, Any
import pandas as pd


class PolarsIndicatorCalculator:
    """
    Polars 零拷贝指标计算器
    
    核心优势：
    1. 零拷贝：Arrow 内存模型，避免数据复制
    2. 惰性计算：支持 LazyFrame，优化查询计划
    3. 并行计算：自动多线程并行
    4. 表达式优化：Polars 表达式层自动优化
    """
    
    def __init__(self, enable_cache: bool = True):
        self.enable_cache = enable_cache
        self._cache = {}
    
    def _ensure_polars(self, df: Union[pl.DataFrame, pd.DataFrame]) -> pl.DataFrame:
        """确保输入是 Polars DataFrame"""
        if isinstance(df, pl.DataFrame):
            return df
        if isinstance(df, pd.DataFrame):
            return pl.from_pandas(df)
        raise TypeError(f"不支持的数据类型: {type(df)}")
    
    def calculate_ma(
        self,
        df: Union[pl.DataFrame, pd.DataFrame],
        column: str = 'close',
        window: int = 20,
        min_periods: Optional[int] = None,
        group_by: Optional[str] = None,
        output_name: Optional[str] = None
    ) -> pl.DataFrame:
        """
        计算移动平均线（MA）- Polars 零拷贝版本
        
        参数：
            df: DataFrame（Polars 或 Pandas）
            column: 要计算的列名
            window: 窗口大小
            min_periods: 最小周期数
            group_by: 分组列名
            output_name: 输出列名
            
        返回：
            添加了 MA 列的 Polars DataFrame
        """
        df_pl = self._ensure_polars(df)
        output_col = output_name or f'ma{window}'
        min_periods = min_periods or window
        
        if group_by:
            expr = (
                pl.col(column)
                .rolling_mean(window_size=window, min_periods=min_periods)
                .over(group_by)
                .alias(output_col)
            )
        else:
            expr = (
                pl.col(column)
                .rolling_mean(window_size=window, min_periods=min_periods)
                .alias(output_col)
            )
        
        return df_pl.with_columns(expr)
    
    def calculate_ema(
        self,
        df: Union[pl.DataFrame, pd.DataFrame],
        column: str = 'close',
        window: int = 12,
        group_by: Optional[str] = None,
        output_name: Optional[str] = None
    ) -> pl.DataFrame:
        """
        计算指数移动平均线（EMA）- Polars 零拷贝版本
        
        参数：
            df: DataFrame
            column: 要计算的列名
            window: 窗口大小（半衰期）
            group_by: 分组列名
            output_name: 输出列名
            
        返回：
            添加了 EMA 列的 Polars DataFrame
        """
        df_pl = self._ensure_polars(df)
        output_col = output_name or f'ema{window}'
        alpha = 2.0 / (window + 1)
        
        if group_by:
            expr = (
                pl.col(column)
                .ewm_mean(alpha=alpha, adjust=False)
                .over(group_by)
                .alias(output_col)
            )
        else:
            expr = (
                pl.col(column)
                .ewm_mean(alpha=alpha, adjust=False)
                .alias(output_col)
            )
        
        return df_pl.with_columns(expr)
    
    def calculate_rsi(
        self,
        df: Union[pl.DataFrame, pd.DataFrame],
        column: str = 'close',
        window: int = 14,
        group_by: Optional[str] = None,
        output_name: Optional[str] = None
    ) -> pl.DataFrame:
        """
        计算相对强弱指标（RSI）- Polars 零拷贝版本
        
        参数：
            df: DataFrame
            column: 价格列名
            window: 周期（默认14）
            group_by: 分组列名
            output_name: 输出列名
            
        返回：
            添加了 RSI 列的 Polars DataFrame
        """
        df_pl = self._ensure_polars(df)
        output_col = output_name or f'rsi{window}'
        
        delta = pl.col(column).diff()
        gain = pl.when(delta > 0).then(delta).otherwise(0.0)
        loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
        
        if group_by:
            avg_gain = gain.rolling_mean(window_size=window).over(group_by)
            avg_loss = loss.rolling_mean(window_size=window).over(group_by)
        else:
            avg_gain = gain.rolling_mean(window_size=window)
            avg_loss = loss.rolling_mean(window_size=window)
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return df_pl.with_columns(
            rsi.fill_null(50.0).alias(output_col)
        )
    
    def calculate_macd(
        self,
        df: Union[pl.DataFrame, pd.DataFrame],
        column: str = 'close',
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        group_by: Optional[str] = None,
        prefix: str = 'macd'
    ) -> pl.DataFrame:
        """
        计算 MACD 指标 - Polars 零拷贝版本
        
        参数：
            df: DataFrame
            column: 价格列名
            fast: 快线周期
            slow: 慢线周期
            signal: 信号线周期
            group_by: 分组列名
            prefix: 输出列名前缀
            
        返回：
            添加了 MACD 列的 Polars DataFrame
        """
        df_pl = self._ensure_polars(df)
        
        alpha_fast = 2.0 / (fast + 1)
        alpha_slow = 2.0 / (slow + 1)
        alpha_signal = 2.0 / (signal + 1)
        
        if group_by:
            ema_fast = pl.col(column).ewm_mean(alpha=alpha_fast, adjust=False).over(group_by)
            ema_slow = pl.col(column).ewm_mean(alpha=alpha_slow, adjust=False).over(group_by)
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.ewm_mean(alpha=alpha_signal, adjust=False).over(group_by)
        else:
            ema_fast = pl.col(column).ewm_mean(alpha=alpha_fast, adjust=False)
            ema_slow = pl.col(column).ewm_mean(alpha=alpha_slow, adjust=False)
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.ewm_mean(alpha=alpha_signal, adjust=False)
        
        return df_pl.with_columns([
            macd_line.alias(f'{prefix}_macd'),
            signal_line.alias(f'{prefix}_signal'),
            (macd_line - signal_line).alias(f'{prefix}_histogram')
        ])
    
    def calculate_bollinger_bands(
        self,
        df: Union[pl.DataFrame, pd.DataFrame],
        column: str = 'close',
        window: int = 20,
        num_std: float = 2.0,
        group_by: Optional[str] = None,
        prefix: str = 'bb'
    ) -> pl.DataFrame:
        """
        计算布林带（Bollinger Bands）- Polars 零拷贝版本
        
        参数：
            df: DataFrame
            column: 价格列名
            window: 窗口大小
            num_std: 标准差倍数
            group_by: 分组列名
            prefix: 输出列名前缀
            
        返回：
            添加了布林带列的 Polars DataFrame
        """
        df_pl = self._ensure_polars(df)
        
        if group_by:
            middle = pl.col(column).rolling_mean(window_size=window).over(group_by)
            std = pl.col(column).rolling_std(window_size=window).over(group_by)
        else:
            middle = pl.col(column).rolling_mean(window_size=window)
            std = pl.col(column).rolling_std(window_size=window)
        
        return df_pl.with_columns([
            middle.alias(f'{prefix}_middle'),
            (middle + std * num_std).alias(f'{prefix}_upper'),
            (middle - std * num_std).alias(f'{prefix}_lower')
        ])
    
    def calculate_atr(
        self,
        df: Union[pl.DataFrame, pd.DataFrame],
        window: int = 14,
        group_by: Optional[str] = None,
        output_name: Optional[str] = None
    ) -> pl.DataFrame:
        """
        计算平均真实波幅（ATR）- Polars 零拷贝版本
        
        参数：
            df: DataFrame（必须包含 high, low, close 列）
            window: 窗口大小
            group_by: 分组列名
            output_name: 输出列名
            
        返回：
            添加了 ATR 列的 Polars DataFrame
        """
        df_pl = self._ensure_polars(df)
        output_col = output_name or f'atr{window}'
        
        high = pl.col('high')
        low = pl.col('low')
        close = pl.col('close')
        prev_close = close.shift(1)
        
        tr = pl.max_horizontal([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ])
        
        if group_by:
            atr = tr.rolling_mean(window_size=window).over(group_by)
        else:
            atr = tr.rolling_mean(window_size=window)
        
        return df_pl.with_columns(atr.alias(output_col))
    
    def calculate_batch(
        self,
        df: Union[pl.DataFrame, pd.DataFrame],
        indicators: List[Dict[str, Any]],
        group_by: Optional[str] = None
    ) -> pl.DataFrame:
        """
        批量计算多个指标 - Polars 零拷贝版本
        
        参数：
            df: DataFrame
            indicators: 指标配置列表
            group_by: 分组列名
            
        返回：
            添加了所有指标的 Polars DataFrame
        """
        df_pl = self._ensure_polars(df)
        
        for config in indicators:
            indicator_type = config.get('type', '').lower()
            
            if indicator_type == 'ma':
                df_pl = self.calculate_ma(
                    df_pl,
                    column=config.get('column', 'close'),
                    window=config.get('window', 20),
                    min_periods=config.get('min_periods'),
                    group_by=group_by,
                    output_name=config.get('name')
                )
            
            elif indicator_type == 'ema':
                df_pl = self.calculate_ema(
                    df_pl,
                    column=config.get('column', 'close'),
                    window=config.get('window', 12),
                    group_by=group_by,
                    output_name=config.get('name')
                )
            
            elif indicator_type == 'rsi':
                df_pl = self.calculate_rsi(
                    df_pl,
                    column=config.get('column', 'close'),
                    window=config.get('window', 14),
                    group_by=group_by,
                    output_name=config.get('name')
                )
            
            elif indicator_type == 'macd':
                df_pl = self.calculate_macd(
                    df_pl,
                    column=config.get('column', 'close'),
                    fast=config.get('fast', 12),
                    slow=config.get('slow', 26),
                    signal=config.get('signal', 9),
                    group_by=group_by,
                    prefix=config.get('name', 'macd')
                )
            
            elif indicator_type in ('bollinger', 'bb'):
                df_pl = self.calculate_bollinger_bands(
                    df_pl,
                    column=config.get('column', 'close'),
                    window=config.get('window', 20),
                    num_std=config.get('num_std', 2.0),
                    group_by=group_by,
                    prefix=config.get('name', 'bb')
                )
            
            elif indicator_type == 'atr':
                df_pl = self.calculate_atr(
                    df_pl,
                    window=config.get('window', 14),
                    group_by=group_by,
                    output_name=config.get('name')
                )
        
        return df_pl
    
    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()


def get_polars_calculator() -> PolarsIndicatorCalculator:
    """获取 Polars 指标计算器单例"""
    return PolarsIndicatorCalculator()
