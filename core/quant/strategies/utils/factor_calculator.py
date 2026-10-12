"""
声明式因子系统

策略只需声明 required_factors，系统自动注入因子数据。

Example:
    class MyStrategy(VectorizedStrategyBase):
        required_factors = ['rsi_14', 'macd_dif', 'kdj_k']
        
        def on_data(self, data, factors):
            rsi = factors['rsi_14']  # 直接使用，无需计算
            macd = factors['macd_dif']
"""
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Any
from dataclasses import dataclass, field
import threading
import logging

import numpy as np
import polars as pl
import pandas as pd

logger = logging.getLogger(__name__)

PARQUET_DIR = Path(__file__).parent.parent.parent.parent / "data" / "parquet_data"

ALL_DB_FACTORS = {
    'rsi_14', 'kdj_k', 'kdj_d', 'kdj_j',
    'macd_dif', 'macd_dea', 'macd_histogram',
    'atr_14', 'ma5', 'ma10', 'ma20', 'ma60', 'ma120', 'ma250',
    'boll_upper', 'boll_mid', 'boll_lower',
    'bias_5', 'bias_10', 'bias_20',
}

COMPUTE_FACTORS = {
    'gain_1d', 'gain_3d', 'gain_5d', 'gain_10d',
    'volatility_10', 'volatility_20',
    'volume_ma5', 'volume_ma10',
    'rsi_6', 'rsi_24',
}


@dataclass
class FactorData:
    """因子数据容器"""
    factor_name: str
    matrix: np.ndarray
    source: str
    shape: tuple = field(init=False)
    
    def __post_init__(self):
        self.shape = self.matrix.shape


class FactorCalculator:
    """
    因子计算器
    
    核心功能：
    1. 从 parquet 加载预计算因子（数据库优先）
    2. 按需计算缺失因子
    3. 全局缓存，参数优化时共享
    """
    _instance: Optional['FactorCalculator'] = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized') and self._initialized:
            return
        
        self._initialized = True
        self.logger = logging.getLogger(__name__)
        
        self._factor_cache: Dict[str, pl.DataFrame] = {}
        self._matrix_cache: Dict[str, np.ndarray] = {}
        
        self._hot_df: Optional[pl.DataFrame] = None
        self._archive_df: Optional[pl.DataFrame] = None
        self._loaded = False
        
        self.logger.info(f"[FactorCalculator] 初始化完成，数据库因子: {len(ALL_DB_FACTORS)}")
    
    def _load_parquet_data(self) -> None:
        """延迟加载 parquet 数据"""
        if self._loaded:
            return
        
        hot_path = PARQUET_DIR / "factors_momentum_hot.parquet"
        archive_path = PARQUET_DIR / "factors_momentum_archive.parquet"
        
        if hot_path.exists():
            self._hot_df = pl.read_parquet(hot_path)
            self.logger.info(f"[FactorCalculator] 加载 hot 数据: {self._hot_df.shape}")
        
        if archive_path.exists():
            self._archive_df = pl.read_parquet(archive_path)
            self.logger.info(f"[FactorCalculator] 加载 archive 数据: {self._archive_df.shape}")
        
        self._loaded = True
    
    def get_available_factors(self) -> Set[str]:
        """获取所有可用因子"""
        return ALL_DB_FACTORS | COMPUTE_FACTORS
    
    def load_factors(
        self,
        factor_names: List[str],
        trading_dates: List[str],
        stock_codes: List[str],
    ) -> Dict[str, np.ndarray]:
        """
        加载因子数据
        
        Args:
            factor_names: 需要的因子名称列表
            trading_dates: 交易日期列表 (T,)
            stock_codes: 股票代码列表 (N,)
        
        Returns:
            因子字典 {factor_name: matrix(T, N)}
        """
        self._load_parquet_data()
        
        T, N = len(trading_dates), len(stock_codes)
        results: Dict[str, np.ndarray] = {}
        
        date_set = set(trading_dates)
        code_set = set(stock_codes)
        
        for factor_name in factor_names:
            if factor_name in results:
                continue
            
            if factor_name in ALL_DB_FACTORS:
                matrix = self._load_db_factor(factor_name, date_set, code_set, T, N, trading_dates, stock_codes)
                if matrix is not None:
                    results[factor_name] = matrix
                    continue
            
            self.logger.warning(f"[FactorCalculator] 因子 {factor_name} 未找到")
        
        return results
    
    def _load_db_factor(
        self,
        factor_name: str,
        date_set: Set[str],
        code_set: Set[str],
        T: int,
        N: int,
        trading_dates: List[str],
        stock_codes: List[str],
    ) -> Optional[np.ndarray]:
        """从 parquet 加载因子"""
        cache_key = f"{factor_name}_{T}_{N}"
        if cache_key in self._matrix_cache:
            return self._matrix_cache[cache_key]
        
        dfs_to_concat = []
        
        if self._hot_df is not None and factor_name in self._hot_df.columns:
            filtered = self._hot_df.filter(
                (pl.col('trade_date').is_in(date_set)) &
                (pl.col('stock_code').is_in(code_set))
            )
            if len(filtered) > 0:
                dfs_to_concat.append(filtered)
        
        if self._archive_df is not None and factor_name in self._archive_df.columns:
            filtered = self._archive_df.filter(
                (pl.col('trade_date').is_in(date_set)) &
                (pl.col('stock_code').is_in(code_set))
            )
            if len(filtered) > 0:
                dfs_to_concat.append(filtered)
        
        if not dfs_to_concat:
            return None
        
        combined = pl.concat(dfs_to_concat)
        combined_pd = combined.select(['trade_date', 'stock_code', factor_name]).to_pandas()
        
        matrix = self._build_matrix(combined_pd, factor_name, trading_dates, stock_codes, T, N)
        
        self._matrix_cache[cache_key] = matrix
        
        return matrix
    
    def _build_matrix(
        self,
        df: pd.DataFrame,
        factor_name: str,
        trading_dates: List[str],
        stock_codes: List[str],
        T: int,
        N: int,
    ) -> np.ndarray:
        """构建因子矩阵"""
        matrix = np.full((T, N), np.nan, dtype=np.float32)
        
        df['trade_date_cat'] = pd.Categorical(df['trade_date'], categories=trading_dates, ordered=True)
        df['stock_code_cat'] = pd.Categorical(df['stock_code'], categories=stock_codes, ordered=True)
        
        i_row = df['trade_date_cat'].cat.codes.values
        j_col = df['stock_code_cat'].cat.codes.values
        
        mask = (i_row >= 0) & (j_col >= 0)
        
        if mask.any():
            vals = df.loc[mask, factor_name].values.astype(np.float32)
            matrix[i_row[mask], j_col[mask]] = vals
        
        return matrix
    
    def clear_cache(self) -> None:
        """清除缓存"""
        self._matrix_cache.clear()
        self.logger.info("[FactorCalculator] 缓存已清除")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        return {
            'matrix_cache_size': len(self._matrix_cache),
            'loaded': self._loaded,
            'hot_rows': self._hot_df.shape[0] if self._hot_df is not None else 0,
            'archive_rows': self._archive_df.shape[0] if self._archive_df is not None else 0,
        }


def get_factor_calculator() -> FactorCalculator:
    """获取全局因子计算器实例"""
    return FactorCalculator()


def get_available_factors() -> Set[str]:
    """获取所有可用因子名称"""
    return ALL_DB_FACTORS | COMPUTE_FACTORS
