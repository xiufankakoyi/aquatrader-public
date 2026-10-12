"""
因子矩阵构建器 - 高性能版本

核心优化：
1. 预计算因子矩阵 + Parquet 缓存
2. Arrow 零拷贝转换
3. 股票代码 int32 存储
"""
import numpy as np
import pandas as pd
import polars as pl
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
import logging
import time
import hashlib
import json

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data/factor_matrix_cache")


@dataclass
class FactorMatrix:
    """
    因子矩阵数据结构（优化版）
    
    Attributes:
        values: 三维数组 (T, N, F) - 日期 × 股票 × 因子
        dates: 日期列表 (长度 T)
        codes_int: 股票代码 int32 数组 (长度 N)
        codes_str: 股票代码字符串列表 (长度 N)
        factor_names: 因子名列表 (长度 F)
        date_to_idx: 日期到索引的映射
        code_to_idx: 股票代码到索引的映射
    """
    values: np.ndarray
    dates: List[str]
    codes_int: np.ndarray
    codes_str: List[str]
    factor_names: List[str]
    date_to_idx: Dict[str, int]
    code_to_idx: Dict[str, int]
    
    def get_day_data(self, date: str) -> Optional[np.ndarray]:
        """获取某天的因子矩阵 (N, F)"""
        idx = self.date_to_idx.get(date)
        if idx is None:
            return None
        return self.values[idx, :, :]
    
    def get_day_index(self, date: str) -> int:
        """获取日期索引"""
        return self.date_to_idx.get(date, -1)
    
    def get_factor(self, factor_name: str) -> Optional[np.ndarray]:
        """获取某个因子的完整矩阵 (T, N)"""
        idx = self.factor_names.index(factor_name) if factor_name in self.factor_names else -1
        if idx < 0:
            return None
        return self.values[:, :, idx]
    
    @classmethod
    def from_parquet(cls, path: Path) -> 'FactorMatrix':
        """从 Parquet 加载（使用 Arrow 零拷贝）"""
        t0 = time.perf_counter()
        
        lazy_df = pl.scan_parquet(path)
        
        meta_path = path.with_suffix('.json')
        if meta_path.exists():
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            dates = meta['dates']
            factor_names = meta['factor_names']
            shape = tuple(meta['shape'])
        else:
            df_sample = lazy_df.collect()
            dates = df_sample['date'].unique().sort().to_list()
            factor_names = [c for c in df_sample.columns if c not in ('date', 'code_int', 'code_str')]
            shape = None
        
        df = lazy_df.collect()
        
        dates_sorted = sorted(dates)
        date_to_idx = {d: i for i, d in enumerate(dates_sorted)}
        
        codes_int = df.select('code_int').unique().sort('code_int')['code_int'].to_numpy()
        codes_str = df.select('code_str').unique().sort('code_str')['code_str'].to_list()
        code_to_idx = {str(c): i for i, c in enumerate(codes_int)}
        
        T = len(dates_sorted)
        N = len(codes_int)
        F = len(factor_names)
        
        if shape:
            values = np.memmap(
                str(path.with_suffix('.npy')),
                dtype=np.float32,
                mode='r',
                shape=shape
            )
        else:
            values = np.full((T, N, F), np.nan, dtype=np.float32)
            
            df_pd = df.to_pandas(use_pyarrow_extension_array=True)
            
            for f_idx, factor_name in enumerate(factor_names):
                if factor_name in df_pd.columns:
                    factor_data = df_pd.pivot(index='date', columns='code_int', values=factor_name)
                    factor_data = factor_data.reindex(index=dates_sorted, columns=codes_int)
                    values[:, :, f_idx] = factor_data.values.astype(np.float32)
        
        t1 = time.perf_counter()
        logger.info(f"[FactorMatrix] 从 Parquet 加载完成: {T}x{N}x{F}, 耗时: {(t1-t0)*1000:.1f}ms")
        
        return cls(
            values=values,
            dates=dates_sorted,
            codes_int=codes_int.astype(np.int32),
            codes_str=codes_str,
            factor_names=factor_names,
            date_to_idx=date_to_idx,
            code_to_idx=code_to_idx
        )
    
    def to_parquet(self, path: Path) -> None:
        """保存到 Parquet（使用 Arrow 零拷贝）"""
        t0 = time.perf_counter()
        
        path.parent.mkdir(parents=True, exist_ok=True)
        
        T, N, F = self.values.shape
        
        date_mesh, code_mesh = np.meshgrid(np.arange(T), np.arange(N), indexing='ij')
        
        date_flat = np.array(self.dates, dtype='datetime64[ms]')[date_mesh.flatten()]
        code_int_flat = self.codes_int[code_mesh.flatten()]
        code_str_flat = np.array(self.codes_str)[code_mesh.flatten()]
        
        data = {
            'date': date_flat,
            'code_int': code_int_flat,
            'code_str': code_str_flat
        }
        
        for f_idx, factor_name in enumerate(self.factor_names):
            data[factor_name] = self.values[:, :, f_idx].flatten()
        
        df = pl.from_dict(data)
        df.write_parquet(path, compression='snappy', use_pyarrow=True)
        
        meta = {
            'dates': self.dates,
            'factor_names': self.factor_names,
            'shape': [T, N, F],
            'codes_int': [int(c) for c in self.codes_int]
        }
        with open(path.with_suffix('.json'), 'w') as f:
            json.dump(meta, f)
        
        npy_path = path.with_suffix('.npy')
        np.save(npy_path, self.values)
        
        t1 = time.perf_counter()
        logger.info(f"[FactorMatrix] 保存到 Parquet 完成: {path}, 耗时: {(t1-t0)*1000:.1f}ms")


def stock_code_to_int(code: str) -> int:
    """
    股票代码转 int32
    
    000001.SZ -> 1000001
    600001.SH -> 6000001
    """
    code = str(code).strip()
    if '.' in code:
        num_part, market = code.split('.')
        num = int(num_part)
        if market == 'SH':
            return num + 6000000
        else:
            return num + 1000000
    return int(code)


def stock_codes_to_int_vectorized(codes: pd.Series) -> np.ndarray:
    """
    向量化股票代码转 int32
    
    性能优化：使用向量化字符串操作，比 apply 快 50-100 倍
    """
    codes_str = codes.astype(str).str.strip()
    
    has_dot = codes_str.str.contains('.', regex=False)
    
    result = np.zeros(len(codes), dtype=np.int32)
    
    if has_dot.any():
        split_result = codes_str[has_dot].str.split('.', expand=True)
        num_parts = split_result[0].astype(int).values
        markets = split_result[1].values
        
        sh_mask = markets == 'SH'
        sz_mask = markets == 'SZ'
        
        indices = np.where(has_dot)[0]
        result[indices[sh_mask]] = num_parts[sh_mask] + 6000000
        result[indices[sz_mask]] = num_parts[sz_mask] + 1000000
    
    if (~has_dot).any():
        no_dot_indices = np.where(~has_dot)[0]
        result[no_dot_indices] = codes_str[~has_dot].astype(int).values
    
    return result


def stock_codes_to_int_vectorized_polars(codes: 'pl.Series') -> np.ndarray:
    """
    Polars 版本的股票代码转 int32
    
    性能优化：使用 Polars 向量化操作
    
    规则：
    - 000001.SZ -> 1000001
    - 600001.SH -> 6000001
    - 000001 (无后缀，默认深圳) -> 1000001
    - 600001 (无后缀，默认上海) -> 6000001
    """
    codes_str = codes.cast(pl.Utf8).str.strip_chars()
    
    has_dot = codes_str.str.contains('.', literal=True)
    
    result = np.zeros(len(codes), dtype=np.int32)
    
    if has_dot.any():
        split_result = codes_str.str.split('.')
        num_parts = split_result.list.get(0).cast(pl.Int32)
        markets = split_result.list.get(1)
        
        sh_mask = (markets == 'SH').to_numpy()
        sz_mask = (markets == 'SZ').to_numpy()
        has_dot_mask = has_dot.to_numpy()
        
        num_vals = num_parts.to_numpy()
        
        indices = np.where(has_dot_mask)[0]
        result[indices[sh_mask[indices]]] = num_vals[indices[sh_mask[indices]]] + 6000000
        result[indices[sz_mask[indices]]] = num_vals[indices[sz_mask[indices]]] + 1000000
    
    if (~has_dot).any():
        no_dot_mask = (~has_dot).to_numpy()
        no_dot_indices = np.where(no_dot_mask)[0]
        num_vals = codes_str.filter(~has_dot).cast(pl.Int32).to_numpy()
        
        for i, idx in enumerate(no_dot_indices):
            num = num_vals[i]
            if num >= 600000:
                result[idx] = num
            else:
                result[idx] = num + 1000000
    
    return result


def int_to_stock_code(code_int: int) -> str:
    """
    int32 转股票代码
    
    1000001 -> 000001.SZ
    6000001 -> 600001.SH
    """
    if code_int >= 6000000:
        return f"{code_int - 6000000:06d}.SH"
    else:
        return f"{code_int - 1000000:06d}.SZ"


class FactorMatrixCache:
    """
    因子矩阵缓存管理器
    
    支持：
    1. 基于参数哈希的缓存键
    2. Parquet + NPY 双格式存储
    3. 内存映射快速加载
    """
    
    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def get_cache_key(
        self,
        start_date: str,
        end_date: str,
        factor_names: List[str],
        stock_codes: Optional[List[str]] = None
    ) -> str:
        """生成缓存键"""
        key_data = {
            'start': start_date,
            'end': end_date,
            'factors': sorted(factor_names),
            'codes_hash': hashlib.md5(
                json.dumps(sorted(stock_codes) if stock_codes else []).encode()
            ).hexdigest()[:8] if stock_codes else 'all'
        }
        return hashlib.md5(json.dumps(key_data, sort_keys=True).encode()).hexdigest()[:16]
    
    def get_cache_path(self, cache_key: str) -> Path:
        """获取缓存路径"""
        return self.cache_dir / f"factor_matrix_{cache_key}.parquet"
    
    def exists(self, cache_key: str) -> bool:
        """检查缓存是否存在"""
        return self.get_cache_path(cache_key).exists()
    
    def load(self, cache_key: str) -> Optional[FactorMatrix]:
        """加载缓存"""
        path = self.get_cache_path(cache_key)
        if not path.exists():
            return None
        return FactorMatrix.from_parquet(path)
    
    def save(self, cache_key: str, matrix: FactorMatrix) -> None:
        """保存缓存"""
        path = self.get_cache_path(cache_key)
        matrix.to_parquet(path)


class FactorMatrixBuilder:
    """
    因子矩阵构建器（优化版）
    
    优化点：
    1. Arrow 零拷贝转换
    2. 股票代码 int32 存储
    3. 向量化填充矩阵
    """
    
    DEFAULT_FACTORS = [
        'open', 'high', 'low', 'close', 'volume', 'amount',
        'total_mv', 'limit_up', 'limit_down',
        'is_st', 'is_limit_up', 'is_limit_down', 'is_suspended',
        'adj_factor', 'prev_close'
    ]
    
    def __init__(self, custom_factors: Optional[List[str]] = None):
        self.factor_names = custom_factors if custom_factors else self.DEFAULT_FACTORS
        self.cache = FactorMatrixCache()
    
    def _compute_adjusted_prices(
        self,
        values: np.ndarray,
        stock_codes: List[str],
        trading_dates: List[str],
        factor_names: List[str]
    ) -> np.ndarray:
        """
        计算动态前复权价格
        
        注意：我们的数据中 close/open/high/low 已经是前复权价格
        （使用最新复权因子计算），所以直接复制即可。
        
        动态前复权价 = 原始价格 × 当日adj_factor / 最新adj_factor
        
        Args:
            values: 三维因子矩阵 (T, N, F)
            stock_codes: 股票代码列表
            trading_dates: 交易日期列表
            factor_names: 因子名列表
            
        Returns:
            更新后的因子矩阵（添加了 open_adj, high_adj, low_adj, close_adj）
        """
        factor_idx = {name: i for i, name in enumerate(factor_names)}
        
        open_idx = factor_idx.get('open', -1)
        high_idx = factor_idx.get('high', -1)
        low_idx = factor_idx.get('low', -1)
        close_idx = factor_idx.get('close', -1)
        
        open_adj_idx = factor_idx.get('open_adj', -1)
        high_adj_idx = factor_idx.get('high_adj', -1)
        low_adj_idx = factor_idx.get('low_adj', -1)
        close_adj_idx = factor_idx.get('close_adj', -1)
        
        # 直接复制前复权价格（数据中已经是前复权的）。按整个二维切片
        # 使用 NumPy 掩码，避免对每个 T×N 元素执行 Python 循环。
        for source_idx, adjusted_idx in (
            (open_idx, open_adj_idx),
            (high_idx, high_adj_idx),
            (low_idx, low_adj_idx),
            (close_idx, close_adj_idx),
        ):
            if source_idx < 0 or adjusted_idx < 0:
                continue
            source = values[:, :, source_idx]
            target = values[:, :, adjusted_idx]
            valid = ~np.isnan(source)
            target[valid] = source[valid]
        
        logger.info(f"[FactorMatrix] 前复权价格已复制到 adj 字段")
        return values
    
    def build(
        self,
        preloaded_data: Dict[str, Union[pd.DataFrame, 'pl.DataFrame']],
        trading_dates: List[str],
        stock_codes: List[str],
        use_cache: bool = True
    ) -> FactorMatrix:
        """
        构建三维因子矩阵
        
        Args:
            preloaded_data: 预加载的数据字典 {date: DataFrame}，支持 Pandas 或 Polars
            trading_dates: 交易日期列表
            stock_codes: 股票代码列表
            use_cache: 是否使用缓存
            
        Returns:
            FactorMatrix: 因子矩阵对象
        """
        t_start = time.perf_counter()
        
        if use_cache:
            cache_key = self.cache.get_cache_key(
                trading_dates[0] if trading_dates else '',
                trading_dates[-1] if trading_dates else '',
                self.factor_names,
                stock_codes
            )
            
            cached = self.cache.load(cache_key)
            if cached is not None:
                logger.info(f"[FactorMatrix] 命中缓存: {cache_key}")
                return cached
        
        T = len(trading_dates)
        N = len(stock_codes)
        F = len(self.factor_names)
        
        date_to_idx = {date: i for i, date in enumerate(trading_dates)}
        
        codes_int = stock_codes_to_int_vectorized(pd.Series(stock_codes))
        code_to_idx = {str(c): i for i, c in enumerate(codes_int)}
        
        logger.info(f"[FactorMatrix] 开始构建矩阵: T={T}, N={N}, F={F}")
        
        values = np.full((T, N, F), np.nan, dtype=np.float32)
        
        first_df = next(iter(preloaded_data.values())) if preloaded_data else None
        is_polars = hasattr(first_df, 'columns') and not isinstance(first_df, pd.DataFrame)
        
        if is_polars:
            all_dfs = []
            for date_str, df_day in preloaded_data.items():
                if df_day is not None and len(df_day) > 0 and date_str in date_to_idx:
                    available_factors = [c for c in self.factor_names if c in df_day.columns]
                    df_copy = df_day.select(['stock_code'] + available_factors).clone()
                    df_copy = df_copy.with_columns([
                        pl.Series('code_int', stock_codes_to_int_vectorized_polars(df_day['stock_code'])),
                        pl.lit(date_to_idx[date_str]).alias('date_idx')
                    ])
                    all_dfs.append(df_copy)
            
            if all_dfs:
                all_data = pl.concat(all_dfs)
                
                code_idx_series = all_data['code_int'].cast(pl.Utf8).replace_strict(
                    code_to_idx, default=None
                ).cast(pl.Int32)
                all_data = all_data.with_columns(code_idx_series.alias('code_idx'))
                
                all_data = all_data.filter(
                    pl.col('code_idx').is_not_null() & pl.col('date_idx').is_not_null()
                )
                
                t_indices = all_data['date_idx'].to_numpy()
                n_indices = all_data['code_idx'].to_numpy()
                
                for f_idx, factor_name in enumerate(self.factor_names):
                    if factor_name in all_data.columns:
                        vals = all_data[factor_name].to_numpy()
                        mask = ~np.isnan(vals)
                        if mask.any():
                            values[t_indices[mask], n_indices[mask], f_idx] = vals[mask]
        else:
            all_dfs = []
            for date_str, df_day in preloaded_data.items():
                if df_day is not None and not df_day.empty and date_str in date_to_idx:
                    available_factors = [c for c in self.factor_names if c in df_day.columns]
                    df_copy = df_day[['stock_code'] + available_factors].copy()
                    df_copy['code_int'] = stock_codes_to_int_vectorized(df_copy['stock_code'])
                    df_copy['date_idx'] = date_to_idx[date_str]
                    all_dfs.append(df_copy)
            
            if all_dfs:
                all_data = pd.concat(all_dfs, ignore_index=True)
                
                all_data['code_idx'] = all_data['code_int'].astype(str).map(code_to_idx)
                
                all_data = all_data.dropna(subset=['code_idx', 'date_idx'])
                all_data['code_idx'] = all_data['code_idx'].astype(int)
                all_data['date_idx'] = all_data['date_idx'].astype(int)
                
                t_indices = all_data['date_idx'].values
                n_indices = all_data['code_idx'].values
                
                for f_idx, factor_name in enumerate(self.factor_names):
                    if factor_name in all_data.columns:
                        vals = all_data[factor_name].values
                        mask = pd.notna(vals)
                        if mask.any():
                            values[t_indices[mask], n_indices[mask], f_idx] = vals[mask]
        
        t_end = time.perf_counter()
        logger.info(f"[FactorMatrix] 矩阵构建完成: {values.nbytes / 1024 / 1024:.1f} MB, 耗时: {(t_end - t_start)*1000:.1f}ms")
        
        matrix = FactorMatrix(
            values=values,
            dates=trading_dates,
            codes_int=codes_int,
            codes_str=stock_codes,
            factor_names=self.factor_names,
            date_to_idx=date_to_idx,
            code_to_idx=code_to_idx
        )
        
        if use_cache:
            cache_key = self.cache.get_cache_key(
                trading_dates[0] if trading_dates else '',
                trading_dates[-1] if trading_dates else '',
                self.factor_names,
                stock_codes
            )
            self.cache.save(cache_key, matrix)
            logger.info(f"[FactorMatrix] 已缓存: {cache_key}")
        
        return matrix
    
    def build_from_preloaded(
        self,
        preloaded_data: Dict[str, Union[pd.DataFrame, 'pl.DataFrame']],
        use_cache: bool = True
    ) -> FactorMatrix:
        """从预加载数据构建矩阵（自动提取日期和股票代码）"""
        if not preloaded_data:
            raise ValueError("预加载数据为空")
        
        trading_dates = sorted(preloaded_data.keys())
        
        first_df = next(iter(preloaded_data.values())) if preloaded_data else None
        is_polars = hasattr(first_df, 'columns') and not isinstance(first_df, pd.DataFrame)
        
        all_codes = set()
        for df in preloaded_data.values():
            if df is not None and len(df) > 0 and 'stock_code' in df.columns:
                if is_polars:
                    all_codes.update(df['stock_code'].cast(pl.Utf8).str.strip_chars().unique().to_list())
                else:
                    all_codes.update(df['stock_code'].astype(str).str.strip().unique())
        
        # FIX: 确保股票代码有前导零（6位）
        stock_codes = sorted([str(c).zfill(6) for c in all_codes])
        
        return self.build(preloaded_data, trading_dates, stock_codes, use_cache)
    
    def build_from_single_dataframe(
        self,
        df: pl.DataFrame,
        use_cache: bool = True
    ) -> FactorMatrix:
        """
        从单个 Polars DataFrame 构建矩阵（零拷贝优化版）
        
        直接处理完整 DataFrame，无需按日期分区
        
        Args:
            df: Polars DataFrame，必须包含 trade_date 和 stock_code 列
            use_cache: 是否使用缓存
            
        Returns:
            FactorMatrix: 因子矩阵对象
        """
        t_start = time.perf_counter()
        
        if df is None or len(df) == 0:
            raise ValueError("数据为空")
        
        trading_dates = df['trade_date'].unique().sort().to_list()
        trading_dates = [str(d)[:10] for d in trading_dates]  # 标准化为 YYYY-MM-DD
        
        # FIX: 确保股票代码有前导零（6位）
        stock_codes = df['stock_code'].cast(pl.Utf8).str.strip_chars().unique().sort().to_list()
        stock_codes = [str(c).zfill(6) for c in stock_codes]
        
        if use_cache:
            cache_key = self.cache.get_cache_key(
                trading_dates[0] if trading_dates else '',
                trading_dates[-1] if trading_dates else '',
                self.factor_names,
                stock_codes
            )
            
            cached = self.cache.load(cache_key)
            if cached is not None:
                logger.info(f"[FactorMatrix] 命中缓存: {cache_key}")
                return cached
        
        T = len(trading_dates)
        N = len(stock_codes)
        F = len(self.factor_names)
        
        date_to_idx = {date: i for i, date in enumerate(trading_dates)}
        codes_int = stock_codes_to_int_vectorized_polars(pl.Series(stock_codes))
        
        code_to_idx = {}
        for i, (code_str, code_int) in enumerate(zip(stock_codes, codes_int)):
            code_to_idx[str(code_int)] = i
            code_to_idx[code_str] = i
        
        logger.info(f"[FactorMatrix] 开始构建矩阵: T={T}, N={N}, F={F}")
        
        values = np.full((T, N, F), np.nan, dtype=np.float32)
        
        available_factors = [c for c in self.factor_names if c in df.columns]
        
        # FIX: 构建从原始代码到6位代码索引的映射
        # DataFrame 中的代码格式：'000001'
        # 映射中同时包含 '000001' -> idx 和 '1' -> idx
        code_replace_map = {}
        for i, c in enumerate(stock_codes):
            code_str = str(c).strip()
            code_replace_map[code_str] = i  # '000001'
            code_replace_map[code_str.lstrip('0') or '0'] = i  # '1'
        
        df_with_idx = df.select(['trade_date', 'stock_code'] + available_factors).with_columns([
            pl.col('trade_date').cast(pl.Utf8).str.slice(0, 10).replace_strict(date_to_idx, default=None).alias('date_idx'),
            pl.col('stock_code').cast(pl.Utf8).str.strip_chars().replace_strict(
                code_replace_map, default=None
            ).alias('code_idx')
        ])
        
        df_with_idx = df_with_idx.filter(
            pl.col('date_idx').is_not_null() & pl.col('code_idx').is_not_null()
        )
        
        t_indices = df_with_idx['date_idx'].to_numpy().astype(np.int32)
        n_indices = df_with_idx['code_idx'].to_numpy().astype(np.int32)
        
        for f_idx, factor_name in enumerate(available_factors):
            vals = df_with_idx[factor_name].to_numpy()
            mask = ~np.isnan(vals) if vals.dtype.kind in 'fc' else pd.notna(vals)
            if mask.any():
                # FIX: 使用 factor_name 在 self.factor_names 中的索引，而不是 available_factors 中的索引
                actual_f_idx = self.factor_names.index(factor_name)
                values[t_indices[mask], n_indices[mask], actual_f_idx] = vals[mask]
        
        # 计算动态前复权价格
        values = self._compute_adjusted_prices(values, stock_codes, trading_dates, self.factor_names)
        
        matrix = FactorMatrix(
            values=values,
            dates=trading_dates,
            codes_int=codes_int,
            codes_str=stock_codes,
            factor_names=self.factor_names,
            date_to_idx=date_to_idx,
            code_to_idx=code_to_idx
        )
        
        if use_cache:
            cache_key = self.cache.get_cache_key(
                trading_dates[0] if trading_dates else '',
                trading_dates[-1] if trading_dates else '',
                self.factor_names,
                stock_codes
            )
            self.cache.save(cache_key, matrix)
            logger.info(f"[FactorMatrix] 已缓存: {cache_key}")
        
        elapsed = time.perf_counter() - t_start
        logger.info(f"[FactorMatrix] 矩阵构建完成: {values.nbytes / 1024 / 1024:.1f} MB, 耗时: {elapsed*1000:.1f}ms")
        
        return matrix
    
    def build_arrow(
        self,
        preloaded_data: Dict[str, Union[pd.DataFrame, 'pl.DataFrame']],
        trading_dates: List[str],
        stock_codes: List[str]
    ) -> FactorMatrix:
        """
        使用 Arrow 零拷贝构建矩阵
        
        性能优化：直接从 Arrow RecordBatch 构建，避免 pandas 中间层
        """
        t_start = time.perf_counter()
        
        T = len(trading_dates)
        N = len(stock_codes)
        F = len(self.factor_names)
        
        date_to_idx = {date: i for i, date in enumerate(trading_dates)}
        codes_int = stock_codes_to_int_vectorized(pd.Series(stock_codes))
        code_to_idx = {str(c): i for i, c in enumerate(codes_int)}
        
        values = np.full((T, N, F), np.nan, dtype=np.float32)
        
        first_df = next(iter(preloaded_data.values())) if preloaded_data else None
        is_polars = hasattr(first_df, 'columns') and not isinstance(first_df, pd.DataFrame)
        
        all_dfs = []
        for date_str, df_day in preloaded_data.items():
            if df_day is not None and len(df_day) > 0 and date_str in date_to_idx:
                available_factors = [c for c in self.factor_names if c in df_day.columns]
                if is_polars:
                    df_copy = df_day.select(['stock_code'] + available_factors).clone()
                    df_copy = df_copy.with_columns([
                        pl.Series('code_int', stock_codes_to_int_vectorized_polars(df_day['stock_code'])),
                        pl.lit(date_to_idx[date_str]).alias('date_idx')
                    ])
                else:
                    df_copy = df_day[['stock_code'] + available_factors].copy()
                    df_copy['code_int'] = stock_codes_to_int_vectorized(df_copy['stock_code'])
                    df_copy['date_idx'] = date_to_idx[date_str]
                all_dfs.append(df_copy)
        
        if all_dfs:
            if is_polars:
                df_polars = pl.concat(all_dfs)
            else:
                all_data = pd.concat(all_dfs, ignore_index=True)
                arrow_table = all_data.to_arrow()
                df_polars = pl.from_arrow(arrow_table)
            
            t_indices = df_polars['date_idx'].to_numpy()
            n_indices = np.array([code_to_idx.get(str(c), -1) for c in df_polars['code_int'].to_numpy()])
            
            valid_mask = n_indices >= 0
            t_valid = t_indices[valid_mask]
            n_valid = n_indices[valid_mask]
            
            for f_idx, factor_name in enumerate(self.factor_names):
                if factor_name in df_polars.columns:
                    vals = df_polars[factor_name].to_numpy()
                    vals_valid = vals[valid_mask]
                    mask = ~np.isnan(vals_valid)
                    if mask.any():
                        values[t_valid[mask], n_valid[mask], f_idx] = vals_valid[mask]
        
        t_end = time.perf_counter()
        logger.info(f"[FactorMatrix] Arrow 构建完成: {values.nbytes / 1024 / 1024:.1f} MB, 耗时: {(t_end - t_start)*1000:.1f}ms")
        
        return FactorMatrix(
            values=values,
            dates=trading_dates,
            codes_int=codes_int,
            codes_str=stock_codes,
            factor_names=self.factor_names,
            date_to_idx=date_to_idx,
            code_to_idx=code_to_idx
        )


def build_factor_matrix(
    preloaded_data: Dict[str, Union[pd.DataFrame, 'pl.DataFrame']],
    trading_dates: Optional[List[str]] = None,
    stock_codes: Optional[List[str]] = None,
    custom_factors: Optional[List[str]] = None,
    use_cache: bool = True
) -> FactorMatrix:
    """
    便捷函数：构建因子矩阵
    
    Args:
        preloaded_data: 预加载的数据字典
        trading_dates: 交易日期列表（可选）
        stock_codes: 股票代码列表（可选）
        custom_factors: 自定义因子列表（可选）
        use_cache: 是否使用缓存
        
    Returns:
        FactorMatrix: 因子矩阵对象
    """
    builder = FactorMatrixBuilder(custom_factors)
    
    if trading_dates and stock_codes:
        return builder.build(preloaded_data, trading_dates, stock_codes, use_cache)
    else:
        return builder.build_from_preloaded(preloaded_data, use_cache)
