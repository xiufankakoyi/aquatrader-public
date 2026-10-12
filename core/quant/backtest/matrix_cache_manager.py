"""
矩阵缓存管理器 - 预计算和持久化回测矩阵

核心功能：
1. 预计算股票×日期×字段的矩阵
2. 使用内存映射(mmap)快速加载
3. 支持增量更新
"""
import numpy as np
import pandas as pd
import os
import pickle
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

class MatrixCacheManager:
    """
    矩阵缓存管理器
    
    将股票数据预计算为矩阵格式并持久化，回测时直接内存映射加载
    """
    
    def __init__(self, cache_dir: str = "data/matrix_cache"):
        """
        初始化矩阵缓存管理器
        
        Args:
            cache_dir: 缓存目录路径
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 矩阵字段定义
        self.matrix_fields = [
            'open', 'high', 'low', 'close', 'volume', 'amount',
            'total_mv', 'float_mv', 'turnover_rate', 'volume_ratio',
            'ma5', 'ma10', 'ma20', 'is_st', 'is_kc', 'is_cy',
            'is_limit_up', 'is_limit_down', 'is_suspended'
        ]
        
    def _get_cache_key(self, start_date: str, end_date: str, 
                       stock_codes: List[str]) -> str:
        """生成缓存键"""
        key_str = f"{start_date}_{end_date}_{len(stock_codes)}_{','.join(sorted(stock_codes)[:10])}"
        return hashlib.md5(key_str.encode()).hexdigest()[:16]
    
    def _get_cache_paths(self, cache_key: str) -> Dict[str, Path]:
        """获取缓存文件路径"""
        cache_subdir = self.cache_dir / cache_key
        cache_subdir.mkdir(exist_ok=True)
        
        return {
            'metadata': cache_subdir / "metadata.pkl",
            'matrix': cache_subdir / "matrix.npy",
            'dates': cache_subdir / "dates.npy",
            'codes': cache_subdir / "codes.npy"
        }
    
    def build_and_save_matrix(self, 
                             preloaded_data: Dict[str, pd.DataFrame],
                             trading_dates: List[str],
                             stock_codes: List[str],
                             force_rebuild: bool = False) -> Optional[Dict]:
        """
        构建矩阵并保存到缓存
        
        Args:
            preloaded_data: 预加载的数据字典 {date: DataFrame} 或 {'stock_daily': DataFrame}
            trading_dates: 交易日期列表
            stock_codes: 股票代码列表
            force_rebuild: 是否强制重建
            
        Returns:
            缓存的矩阵数据字典
        """
        if not preloaded_data:
            return None
        
        if 'stock_daily' in preloaded_data:
            stock_daily = preloaded_data['stock_daily']
            if stock_daily is None or len(stock_daily) == 0:
                return None
            
            import polars as pl
            if hasattr(stock_daily, 'columns'):
                start_date = str(stock_daily['trade_date'].min())
                end_date = str(stock_daily['trade_date'].max())
            else:
                start_date = str(stock_daily['trade_date'].min())
                end_date = str(stock_daily['trade_date'].max())
        else:
            start_date = min(preloaded_data.keys())
            end_date = max(preloaded_data.keys())
        
        cache_key = self._get_cache_key(start_date, end_date, stock_codes)
        paths = self._get_cache_paths(cache_key)
        
        if not force_rebuild and paths['metadata'].exists():
            logger.info(f"[MatrixCache] 缓存已存在: {cache_key}")
            return None
        
        logger.info(f"[MatrixCache] 开始构建矩阵: {cache_key}")
        
        T = len(trading_dates)
        N = len(stock_codes)
        
        date_to_idx = {date: i for i, date in enumerate(trading_dates)}
        code_to_idx = {code: i for i, code in enumerate(stock_codes)}
        
        matrices = {}
        for field in self.matrix_fields:
            if field in ['is_st', 'is_kc', 'is_cy', 'is_limit_up', 'is_limit_down', 'is_suspended']:
                matrices[field] = np.full((T, N), 0, dtype=np.int8)
            else:
                matrices[field] = np.full((T, N), np.nan, dtype=np.float32)
        
        import time
        t_fill_start = time.perf_counter()
        
        if 'stock_daily' in preloaded_data:
            import polars as pl
            all_data_pl = preloaded_data['stock_daily'].clone()
            
            # 修复：计算 is_limit_up, is_limit_down, is_suspended 字段
            # 原始数据中的 limit_up/limit_down 是价格，需要计算是否达到涨跌停
            if 'limit_up' in all_data_pl.columns and 'close' in all_data_pl.columns:
                all_data_pl = all_data_pl.with_columns([
                    (pl.col('close') >= pl.col('limit_up')).cast(pl.Int8).alias('is_limit_up')
                ])
            if 'limit_down' in all_data_pl.columns and 'close' in all_data_pl.columns:
                all_data_pl = all_data_pl.with_columns([
                    (pl.col('close') <= pl.col('limit_down')).cast(pl.Int8).alias('is_limit_down')
                ])
            if 'volume' in all_data_pl.columns and 'close' in all_data_pl.columns:
                all_data_pl = all_data_pl.with_columns([
                    ((pl.col('volume') == 0) | (pl.col('close') == 0)).cast(pl.Int8).alias('is_suspended')
                ])
            
            all_data_pl = all_data_pl.with_columns([
                pl.col('trade_date').cast(pl.Utf8).replace_strict(date_to_idx, default=-1).alias('date_idx'),
                pl.col('stock_code').cast(pl.Utf8).str.strip_chars().replace_strict(code_to_idx, default=-1).alias('code_idx')
            ])
            
            all_data_pl = all_data_pl.filter(
                (pl.col('code_idx') >= 0) & (pl.col('date_idx') >= 0)
            )
            
            t_indices = all_data_pl['date_idx'].to_numpy().astype(np.int32)
            n_indices = all_data_pl['code_idx'].to_numpy().astype(np.int32)
            
            for field in self.matrix_fields:
                if field in all_data_pl.columns:
                    vals = all_data_pl[field].to_numpy()
                    mask = ~np.isnan(vals) if vals.dtype.kind in 'fc' else pd.notna(vals)
                    if mask.any():
                        matrices[field][t_indices[mask], n_indices[mask]] = vals[mask]
            
            fill_count = len(all_data_pl)
        else:
            all_dfs = []
            for date_str, df_day in preloaded_data.items():
                if df_day is not None:
                    is_empty = df_day.is_empty() if hasattr(df_day, 'is_empty') else df_day.empty
                    if not is_empty and date_str in date_to_idx:
                        df_copy = df_day.clone() if hasattr(df_day, 'clone') else df_day.copy()
                        if hasattr(df_copy, 'with_columns'):
                            df_copy = df_copy.with_columns(date_idx=date_to_idx[date_str])
                        else:
                            df_copy['date_idx'] = date_to_idx[date_str]
                        all_dfs.append(df_copy)
            
            if all_dfs:
                first_df = all_dfs[0]
                is_polars = hasattr(first_df, 'columns') and not isinstance(first_df, pd.DataFrame)
                
                if is_polars:
                    import polars as pl
                    all_data_pl = pl.concat(all_dfs)
                    
                    all_data_pl = all_data_pl.with_columns(
                        pl.col('stock_code').cast(pl.Utf8).str.strip_chars().alias('stock_code')
                    )
                    
                    code_idx_series = all_data_pl['stock_code'].cast(pl.Utf8).replace_strict(
                        code_to_idx, default=None
                    )
                    all_data_pl = all_data_pl.with_columns(code_idx=code_idx_series)
                    
                    all_data_pl = all_data_pl.filter(
                        pl.col('code_idx').is_not_null() & pl.col('date_idx').is_not_null()
                    )
                    
                    t_indices = all_data_pl['date_idx'].to_numpy().astype(np.int32)
                    n_indices = all_data_pl['code_idx'].to_numpy().astype(np.int32)
                    
                    for field in self.matrix_fields:
                        if field in all_data_pl.columns:
                            vals = all_data_pl[field].to_numpy()
                            mask = ~np.isnan(vals) if vals.dtype.kind in 'fc' else pd.notna(vals)
                            if mask.any():
                                matrices[field][t_indices[mask], n_indices[mask]] = vals[mask]
                    
                    fill_count = len(all_data_pl)
                else:
                    all_data = pd.concat(all_dfs, ignore_index=True)
                    
                    all_data['stock_code'] = all_data['stock_code'].astype(str).str.strip()
                    all_data['code_idx'] = all_data['stock_code'].map(code_to_idx)
                    
                    all_data = all_data.dropna(subset=['code_idx', 'date_idx'])
                    all_data['code_idx'] = all_data['code_idx'].astype(int)
                    all_data['date_idx'] = all_data['date_idx'].astype(int)
                    
                    t_indices = all_data['date_idx'].values
                    n_indices = all_data['code_idx'].values
                    
                    for field in self.matrix_fields:
                        if field in all_data.columns:
                            vals = all_data[field].values
                            mask = pd.notna(vals)
                            if mask.any():
                                matrices[field][t_indices[mask], n_indices[mask]] = vals[mask]
                    
                    fill_count = len(all_data)
            else:
                fill_count = 0
        
        t_fill_end = time.perf_counter()
        logger.info(f"[MatrixCache] 矩阵填充完成: {fill_count} 个数据点, 耗时: {(t_fill_end - t_fill_start)*1000:.1f}ms")
        
        # 保存元数据
        metadata = {
            'start_date': start_date,
            'end_date': end_date,
            'trading_dates': trading_dates,
            'stock_codes': stock_codes,
            'T': T,
            'N': N,
            'fields': self.matrix_fields,
            'fill_count': fill_count
        }
        
        with open(paths['metadata'], 'wb') as f:
            pickle.dump(metadata, f)
        
        # 保存矩阵 (使用 np.save，后续可以改为内存映射)
        # 将所有字段保存为一个大的 numpy 数组
        matrix_array = np.stack([matrices[field] for field in self.matrix_fields], axis=2)
        np.save(paths['matrix'], matrix_array)
        
        # 保存日期和股票代码
        np.save(paths['dates'], np.array(trading_dates))
        np.save(paths['codes'], np.array(stock_codes))
        
        logger.info(f"[MatrixCache] 矩阵缓存已保存: {cache_key}, 大小: {matrix_array.nbytes / 1024 / 1024:.1f} MB")
        
        return {
            'matrices': matrices,
            'metadata': metadata,
            'cache_key': cache_key
        }
    
    def load_matrix_mmap(self, start_date: str, end_date: str, 
                         stock_codes: List[str]) -> Optional[Dict]:
        """
        使用内存映射加载矩阵
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            stock_codes: 股票代码列表
            
        Returns:
            矩阵数据字典，或 None 如果缓存不存在
        """
        cache_key = self._get_cache_key(start_date, end_date, stock_codes)
        paths = self._get_cache_paths(cache_key)
        
        if not paths['metadata'].exists():
            return None
        
        logger.info(f"[MatrixCache] 加载矩阵缓存: {cache_key}")
        
        # 加载元数据
        with open(paths['metadata'], 'rb') as f:
            metadata = pickle.load(f)
        
        # 使用内存映射加载矩阵
        matrix_array = np.load(paths['matrix'], mmap_mode='r')
        
        # 加载日期和股票代码
        trading_dates = np.load(paths['dates']).tolist()
        stock_codes = np.load(paths['codes']).tolist()
        
        # 重构矩阵字典
        matrices = {}
        for i, field in enumerate(self.matrix_fields):
            matrices[field] = matrix_array[:, :, i]
        
        logger.info(f"[MatrixCache] 矩阵加载完成: T={metadata['T']}, N={metadata['N']}, "
                   f"内存映射: {matrix_array.nbytes / 1024 / 1024:.1f} MB")
        
        return {
            'matrices': matrices,
            'metadata': metadata,
            'trading_dates': trading_dates,
            'stock_codes': stock_codes,
            'cache_key': cache_key,
            'mmap_array': matrix_array  # 保留引用防止被垃圾回收
        }
    
    def clear_cache(self, cache_key: Optional[str] = None):
        """
        清除缓存
        
        Args:
            cache_key: 特定缓存键，None 表示清除所有
        """
        if cache_key:
            paths = self._get_cache_paths(cache_key)
            for path in paths.values():
                if path.exists():
                    path.unlink()
            logger.info(f"[MatrixCache] 已清除缓存: {cache_key}")
        else:
            # 清除所有缓存
            import shutil
            for subdir in self.cache_dir.iterdir():
                if subdir.is_dir():
                    shutil.rmtree(subdir)
            logger.info(f"[MatrixCache] 已清除所有缓存")
    
    def get_cache_stats(self) -> Dict:
        """获取缓存统计信息"""
        cache_count = sum(1 for _ in self.cache_dir.iterdir() if _.is_dir())
        total_size = sum(
            f.stat().st_size 
            for subdir in self.cache_dir.iterdir() 
            if subdir.is_dir()
            for f in subdir.iterdir()
        )
        
        return {
            'cache_count': cache_count,
            'total_size_mb': total_size / 1024 / 1024,
            'cache_dir': str(self.cache_dir)
        }


# 全局实例
_matrix_cache_manager: Optional[MatrixCacheManager] = None

def get_matrix_cache_manager(cache_dir: str = "data/matrix_cache") -> MatrixCacheManager:
    """获取全局矩阵缓存管理器实例"""
    global _matrix_cache_manager
    if _matrix_cache_manager is None:
        _matrix_cache_manager = MatrixCacheManager(cache_dir)
    return _matrix_cache_manager
