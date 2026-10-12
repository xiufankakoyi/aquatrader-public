"""
向量化策略基类 - 封装矩阵转换逻辑

设计目标：
- 将复杂的"数据对齐"、"矩阵构建"、"Categorical 映射"逻辑封装进父类
- 子类只需关注策略逻辑，代码量减少 70%
- 保持高性能（45秒变0.2秒的优化方案）
- 支持并行多任务回测（task_id 级别缓存隔离）
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple, Any, Union
import functools
import threading
import pandas as pd
import polars as pl
import numpy as np
import time

from core.quant.strategies.strategy_framework import StrategyBase


# ==============================================================================
# [重构A] Task-scoped Matrix Cache with LRU Global Cap
# ==============================================================================
class TaskMatrixCache:
    """
    任务级矩阵缓存，支持并行多任务回测，带全局 LRU 硬上限兜底。

    架构：
    - 两级缓存: task_id -> cache_key -> matrix_dict
    - 全局 LRU 淘汰：当总条目数超过 MAX_GLOBAL_ENTRIES 时，
      优先淘汰最久未访问的全局条目（无 task_id 的）
    - 线程安全：使用 RLock

    使用方式：
        cache = TaskMatrixCache(max_global_entries=32)
        cache.put(task_id, cache_key, matrix_data)   # 存入
        data = cache.get(task_id, cache_key)          # 读取
        cache.clear_task(task_id)                     # 回测结束清理
    """

    DEFAULT_MAX_GLOBAL_ENTRIES: int = 32

    def __init__(self, max_global_entries: int = DEFAULT_MAX_GLOBAL_ENTRIES):
        self._max_global = max_global_entries
        self._lock = threading.RLock()
        # task_id -> {cache_key: (matrix_data, last_access_time)}
        self._task_caches: Dict[str, Dict[Any, Tuple[Dict[str, Any], float]]] = {}
        # 全局无 task 缓存 (兼容旧调用)
        self._global_cache: Dict[Any, Tuple[Dict[str, Any], float]] = {}
        self._hits: int = 0
        self._misses: int = 0

    def _now(self) -> float:
        return time.monotonic()

    def get(
        self,
        cache_key: Any,
        task_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取缓存数据。

        Args:
            cache_key: 缓存键
            task_id: 任务ID，None 表示全局缓存

        Returns:
            缓存的矩阵字典，未命中返回 None
        """
        with self._lock:
            if task_id is not None:
                task_cache = self._task_caches.get(task_id)
                if task_cache and cache_key in task_cache:
                    data, _ = task_cache[cache_key]
                    task_cache[cache_key] = (data, self._now())
                    self._hits += 1
                    return data
            else:
                if cache_key in self._global_cache:
                    data, _ = self._global_cache[cache_key]
                    self._global_cache[cache_key] = (data, self._now())
                    self._hits += 1
                    return data
            self._misses += 1
            return None

    def put(
        self,
        cache_key: Any,
        data: Dict[str, Any],
        task_id: Optional[str] = None
    ) -> None:
        """
        存入缓存数据。

        Args:
            cache_key: 缓存键
            data: 矩阵字典
            task_id: 任务ID，None 表示全局缓存
        """
        with self._lock:
            now = self._now()
            if task_id is not None:
                if task_id not in self._task_caches:
                    self._task_caches[task_id] = {}
                self._task_caches[task_id][cache_key] = (data, now)
            else:
                self._global_cache[cache_key] = (data, now)

            self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """LRU 淘汰：当总条目超过上限时，淘汰最久未访问的全局条目。"""
        total = len(self._global_cache) + sum(
            len(tc) for tc in self._task_caches.values()
        )
        if total <= self._max_global:
            return

        # 只从全局缓存中淘汰（task 缓存由调用方主动清理）
        if self._global_cache:
            oldest_key = min(
                self._global_cache.keys(),
                key=lambda k: self._global_cache[k][1]
            )
            del self._global_cache[oldest_key]

    def clear_task(self, task_id: str) -> None:
        """
        清理指定任务的所有缓存。

        Args:
            task_id: 任务ID
        """
        with self._lock:
            if task_id in self._task_caches:
                del self._task_caches[task_id]

    def clear_all(self) -> None:
        """清理所有缓存（全局 + 所有任务）。"""
        with self._lock:
            self._task_caches.clear()
            self._global_cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息。"""
        with self._lock:
            total = self._hits + self._misses
            return {
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': self._hits / total * 100 if total > 0 else 0,
                'size': len(self._global_cache) + sum(
                    len(task_cache) for task_cache in self._task_caches.values()
                ),
                'global_size': len(self._global_cache),
                'task_count': len(self._task_caches),
                'task_sizes': {tid: len(tc) for tid, tc in self._task_caches.items()},
            }


# 全局缓存实例（模块级单例）
_task_matrix_cache = TaskMatrixCache()

# Compatibility exports retained for callers that still inspect the legacy
# module-level cache directly. New code should use the public helper functions.
_global_matrix_cache = _task_matrix_cache._global_cache
_global_matrix_cache_hits = 0
_global_matrix_cache_misses = 0


def _get_cache_key(
    preloaded_data: Dict,
    trading_dates: List[str],
    stock_codes: List[str]
) -> Tuple[int, Tuple, Tuple]:
    """生成缓存键（保留 id 用于同一对象复用识别）。"""
    return (id(preloaded_data), tuple(trading_dates), tuple(stock_codes))


def clear_matrix_cache(task_id: Optional[str] = None):
    """
    清除矩阵缓存。

    Args:
        task_id: 指定任务ID则只清理该任务缓存；None 则清理全局缓存。
    """
    if task_id is not None:
        _task_matrix_cache.clear_task(task_id)
    else:
        _task_matrix_cache.clear_all()


def get_matrix_cache_stats() -> Dict[str, Any]:
    """获取缓存统计信息。"""
    return _task_matrix_cache.get_stats()


def safe_matrix_fill(func):
    """
    装饰器：确保 pd.Categorical 产生的映射坐标在有效范围内，并捕获映射失败。

    安全增强：
    1. 禁止静默丢弃数据
    2. 当丢弃比例超过5%时，输出警告日志并列出前5个丢失的Date或Symbol
    """
    @functools.wraps(func)
    def wrapper(self, matrix, row_codes, col_codes, values, name="matrix",
                trading_dates=None, stock_codes=None):
        # 1. 识别映射失败 (Categorical 产生的 -1)
        valid_mask = (row_codes != -1) & (col_codes != -1)

        # 2. 统计缺失情况
        total_count = len(row_codes)
        valid_count = np.sum(valid_mask)
        invalid_count = total_count - valid_count
        invalid_ratio = invalid_count / total_count if total_count > 0 else 0.0

        # 3. 数据完整性保护：丢弃比例超过5%时输出警告
        if invalid_count > 0:
            discard_ratio = invalid_ratio * 100

            if trading_dates is not None and stock_codes is not None:
                invalid_indices = np.where(~valid_mask)[0]
                lost_dates = set()
                lost_codes = set()
                for idx in invalid_indices[:10]:
                    if idx < len(row_codes):
                        date_idx = row_codes[idx]
                        code_idx = col_codes[idx]
                        if 0 <= date_idx < len(trading_dates):
                            lost_dates.add(trading_dates[date_idx])
                        if 0 <= code_idx < len(stock_codes):
                            lost_codes.add(stock_codes[code_idx])

                lost_items = []
                for d in list(lost_dates)[:3]:
                    lost_items.append(f"Date:{d}")
                for c in list(lost_codes)[:3]:
                    lost_items.append(f"Symbol:{c}")

                warning_msg = (
                    f"[{name}] 数据丢弃警告: {invalid_count}/{total_count} "
                    f"({discard_ratio:.2f}%) 映射失败"
                )
                if discard_ratio > 5.0:
                    warning_msg += f" | 丢失项示例: {', '.join(lost_items[:5])}"
                    print(f"[WARN] {warning_msg}")
                else:
                    print(f"[{name}] 轻微映射失败: {invalid_count} points ({discard_ratio:.2f}%)")
            else:
                if invalid_ratio > 0.05:
                    print(
                        f"[WARN] [{name}] 严重数据丢弃: {invalid_count}/{total_count} "
                        f"({discard_ratio:.2f}%)"
                    )
                else:
                    print(f"[{name}] 映射失败: {invalid_count} points ({discard_ratio:.2f}%)")

        # 4. 仅保留有效坐标
        safe_rows = row_codes[valid_mask]
        safe_cols = col_codes[valid_mask]
        safe_vals = values[valid_mask]

        # 5. 执行原有的矩阵填充逻辑 (Fancy Indexing)
        return func(self, matrix, safe_rows, safe_cols, safe_vals)
    return wrapper


class VectorizedStrategyBase(StrategyBase):
    """
    向量化策略基类

    核心功能：
    1. prepare_data() - 将 preloaded_data 字典转换为对齐的 NumPy 矩阵
    2. 自动构建常用矩阵（close, open, volume, total_mv 等）作为实例变量
    3. 处理上市日期转换为天数矩阵
    4. 声明式因子系统：声明 required_factors，自动注入 factors 属性
    5. 自动列推断：无需声明 required_factors，系统自动推断所需列

    使用方式：
        class MyStrategy(VectorizedStrategyBase):
            required_factors = ['rsi_14', 'macd_dif', 'kdj_k']  # 可选

            def generate_signals_vectorized(self, price_matrix, trading_dates, stock_codes, data_query, preloaded_data):
                self.prepare_data(preloaded_data, trading_dates, stock_codes)

                # 直接使用因子，无需计算
                rsi = self.factors['rsi_14']
                macd = self.factors['macd_dif']
                ...
    """

    required_factors: List[str] = []
    _inferred_columns: Optional[List[str]] = None

    @classmethod
    def get_required_columns(cls) -> List[str]:
        """
        获取策略所需列（自动推断 + 手动声明）

        Returns:
            所需列名列表
        """
        from core.utils.column_inference import ColumnInference
        return ColumnInference.get_required_columns(cls)

    def __init__(self, name: str | None = None):
        super().__init__(name)
        # 矩阵数据（在 prepare_data 中初始化）
        self.close: Optional[np.ndarray] = None
        self.open: Optional[np.ndarray] = None
        self.high: Optional[np.ndarray] = None
        self.low: Optional[np.ndarray] = None
        self.volume: Optional[np.ndarray] = None
        self.amount: Optional[np.ndarray] = None
        self.total_mv: Optional[np.ndarray] = None
        self.is_st: Optional[np.ndarray] = None
        self.volume_ratio: Optional[np.ndarray] = None
        self.days_listed: Optional[np.ndarray] = None
        self.turnover_rate: Optional[np.ndarray] = None

        # 数据库已有因子（直接从数据库加载）
        self.ma5: Optional[np.ndarray] = None
        self.ma10: Optional[np.ndarray] = None
        self.ma20: Optional[np.ndarray] = None
        self.volume_ma5: Optional[np.ndarray] = None

        # 维度信息
        self.T: Optional[int] = None
        self.N: Optional[int] = None

        # 声明式因子系统
        self.factors: Dict[str, np.ndarray] = {}
        self._trading_dates: List[str] = []
        self._stock_codes: List[str] = []

    @safe_matrix_fill
    def _execute_fill(self, matrix, r_idx, c_idx, vals, name="matrix"):
        """执行矩阵填充的核心方法，使用装饰器确保安全填充。"""
        matrix[r_idx, c_idx] = vals

    def prepare_data(
        self,
        preloaded_data: Optional[Dict[str, Union[pd.DataFrame, 'pl.DataFrame']]],
        trading_dates: List[str],
        stock_codes: List[str],
        price_matrix: Optional[np.ndarray] = None,
        task_id: Optional[str] = None
    ) -> None:
        """
        将 preloaded_data 字典转换为对齐的 NumPy 矩阵

        Args:
            preloaded_data: 预加载的数据字典 {date: DataFrame}，支持 Pandas 或 Polars
            trading_dates: 交易日期列表
            stock_codes: 股票代码列表
            price_matrix: 价格矩阵 (T, N, 4) - 可选，用于填充缺失的价格数据
            task_id: 任务ID，用于缓存隔离；None 使用全局缓存

        返回:
            None - 所有矩阵作为实例变量存储
        """
        # [防递归] 检查是否已经在 prepare_data 中
        in_prepare = getattr(self, '_in_prepare_data', False)
        if in_prepare:
            if hasattr(self, 'close') and self.close is not None:
                return

        self._in_prepare_data = True

        T = len(trading_dates)
        N = len(stock_codes)
        self.T = T
        self.N = N

        # [因子注入] 检查是否有引擎注入的因子
        injected_factors = getattr(self, 'factors', None)
        if injected_factors and isinstance(injected_factors, dict):
            print(f"[prepare_data] 检测到注入因子: {list(injected_factors.keys())}")
            for factor_name, matrix in injected_factors.items():
                if matrix is not None and isinstance(matrix, np.ndarray):
                    non_nan = np.sum(~np.isnan(matrix))
                    print(f"  {factor_name}: shape={matrix.shape}, non_nan={non_nan}")
                    setattr(self, factor_name, matrix.astype(np.float32))
        else:
            print("[prepare_data] 未检测到注入因子")

        # =====================================================================
        # [性能优化] 首先尝试从矩阵缓存管理器加载（支持内存映射）
        # =====================================================================
        from core.quant.backtest.matrix_cache_manager import get_matrix_cache_manager
        cache_manager = get_matrix_cache_manager()

        start_date = min(preloaded_data.keys()) if preloaded_data else ""
        end_date = max(preloaded_data.keys()) if preloaded_data else ""

        cached_data = cache_manager.load_matrix_mmap(start_date, end_date, stock_codes)

        cache_valid = False
        if cached_data is not None:
            cached_dates = cached_data.get('trading_dates', [])
            cached_codes = cached_data.get('stock_codes', [])
            if len(cached_dates) == T and len(cached_codes) == N:
                cache_valid = True
            else:
                print(
                    f"[VectorizedBase] 缓存形状不匹配: "
                    f"缓存({len(cached_dates)}, {len(cached_codes)}) vs 当前({T}, {N})，重新构建"
                )

        if cache_valid:
            matrices = cached_data['matrices']
            self.close = matrices['close'].copy()
            self.open = matrices['open'].copy()
            self.high = matrices['high'].copy()
            self.low = matrices['low'].copy()
            self.volume = matrices['volume'].copy()
            self.amount = matrices['amount'].copy()
            self.total_mv = matrices.get('total_mv', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.is_st = matrices.get('is_st', np.full((T, N), 0, dtype=np.int8)).copy()
            self.volume_ratio = matrices.get('volume_ratio', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.days_listed = matrices.get('days_listed', np.full((T, N), np.nan, dtype=np.float64)).copy()
            self.turnover_rate = matrices.get('turnover_rate', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.ma5 = matrices.get('ma5', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.ma10 = matrices.get('ma10', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.ma20 = matrices.get('ma20', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.volume_ma5 = matrices.get('volume_ma5', np.full((T, N), np.nan, dtype=np.float32)).copy()
            return

        # =====================================================================
        # [性能优化] 检查内存缓存 - 支持 task_id 隔离
        # =====================================================================
        cache_key = _get_cache_key(preloaded_data, trading_dates, stock_codes)
        cached_data = _task_matrix_cache.get(cache_key, task_id=task_id)

        if cached_data is not None:
            self.close = cached_data['close'].copy()
            self.open = cached_data['open'].copy()
            self.high = cached_data['high'].copy()
            self.low = cached_data['low'].copy()
            self.volume = cached_data['volume'].copy()
            self.amount = cached_data['amount'].copy()
            self.total_mv = cached_data.get('total_mv', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.is_st = cached_data.get('is_st', np.full((T, N), 0, dtype=np.int8)).copy()
            self.volume_ratio = cached_data.get('volume_ratio', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.days_listed = cached_data.get('days_listed', np.full((T, N), np.nan, dtype=np.float64)).copy()
            self.turnover_rate = cached_data.get('turnover_rate', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.ma5 = cached_data.get('ma5', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.ma10 = cached_data.get('ma10', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.ma20 = cached_data.get('ma20', np.full((T, N), np.nan, dtype=np.float32)).copy()
            self.volume_ma5 = cached_data.get('volume_ma5', np.full((T, N), np.nan, dtype=np.float32)).copy()
            return

        # =====================================================================
        # 缓存未命中：初始化矩阵并填充
        # =====================================================================
        self.close = np.full((T, N), np.nan, dtype=np.float32)
        self.open = np.full((T, N), np.nan, dtype=np.float32)
        self.high = np.full((T, N), np.nan, dtype=np.float32)
        self.low = np.full((T, N), np.nan, dtype=np.float32)
        self.volume = np.full((T, N), 0.0, dtype=np.float32)
        self.amount = np.full((T, N), 0.0, dtype=np.float32)
        self.total_mv = np.full((T, N), np.nan, dtype=np.float32)
        self.is_st = np.full((T, N), 0, dtype=np.int8)
        self.volume_ratio = np.full((T, N), np.nan, dtype=np.float32)
        self.days_listed = np.full((T, N), np.nan, dtype=np.float64)
        self.turnover_rate = np.full((T, N), np.nan, dtype=np.float32)
        self.ma5 = np.full((T, N), np.nan, dtype=np.float32)
        self.ma10 = np.full((T, N), np.nan, dtype=np.float32)
        self.ma20 = np.full((T, N), np.nan, dtype=np.float32)
        self.volume_ma5 = np.full((T, N), np.nan, dtype=np.float32)

        if price_matrix is not None and price_matrix.shape == (T, N, 4):
            self.open = price_matrix[:, :, 0].astype(np.float32)
            self.high = price_matrix[:, :, 1].astype(np.float32)
            self.low = price_matrix[:, :, 2].astype(np.float32)
            self.close = price_matrix[:, :, 3].astype(np.float32)

        if preloaded_data is None or len(preloaded_data) == 0:
            return

        # =====================================================================
        # 步骤1: 合并所有 DataFrame（支持 Polars 零拷贝 + Pandas 兼容）
        # =====================================================================
        try:
            first_df = next(iter(preloaded_data.values())) if preloaded_data else None
            is_polars = hasattr(first_df, 'columns') and not isinstance(first_df, pd.DataFrame)

            if is_polars:
                self._fill_from_polars(preloaded_data, trading_dates, stock_codes, price_matrix)
            else:
                self._fill_from_pandas(preloaded_data, trading_dates, stock_codes, price_matrix)
        except Exception as e:
            print(f"[VectorizedStrategyBase] 数据合并失败: {e}")
            return

        # =====================================================================
        # [性能优化] 将构建好的矩阵存入 task-scoped 缓存
        # =====================================================================
        cache_data = {
            'close': self.close.copy(),
            'open': self.open.copy(),
            'high': self.high.copy(),
            'low': self.low.copy(),
            'volume': self.volume.copy(),
            'amount': self.amount.copy(),
            'total_mv': self.total_mv.copy(),
            'is_st': self.is_st.copy(),
            'volume_ratio': self.volume_ratio.copy(),
            'days_listed': self.days_listed.copy(),
            'turnover_rate': self.turnover_rate.copy(),
            'ma5': self.ma5.copy(),
            'ma10': self.ma10.copy(),
            'ma20': self.ma20.copy(),
            'volume_ma5': self.volume_ma5.copy(),
        }
        _task_matrix_cache.put(cache_key, cache_data, task_id=task_id)

        # =====================================================================
        # [声明式因子系统] 加载策略声明的因子
        # =====================================================================
        self._trading_dates = trading_dates
        self._stock_codes = stock_codes
        self._load_required_factors()

    def _fill_from_polars(
        self,
        preloaded_data: Dict[str, Any],
        trading_dates: List[str],
        stock_codes: List[str],
        price_matrix: Optional[np.ndarray]
    ) -> None:
        """从 Polars DataFrame 填充矩阵。"""
        T, N = self.T, self.N

        if 'daily' in preloaded_data:
            all_pl = preloaded_data['daily']
            if all_pl is None or len(all_pl) == 0:
                return
        else:
            valid_dfs = [df for df in preloaded_data.values() if df is not None and len(df) > 0]
            if not valid_dfs:
                return
            all_pl = pl.concat(valid_dfs)

        date_to_idx = {d: i for i, d in enumerate(trading_dates)}

        def normalize_date(d):
            if isinstance(d, str):
                if ' ' in d:
                    return d.split(' ')[0]
                return d
            return str(d)[:10]

        date_to_idx_normalized = {normalize_date(k): v for k, v in date_to_idx.items()}
        code_to_idx = {str(c).zfill(6): i for i, c in enumerate(stock_codes)}

        all_pl = all_pl.with_columns([
            pl.col('trade_date').map_batches(
                lambda col: col.map_elements(lambda x: normalize_date(x), return_dtype=pl.Utf8)
            ).replace_strict(date_to_idx_normalized, default=-1).alias('date_idx'),
            pl.col('stock_code').cast(pl.Utf8).str.strip_chars().str.zfill(6).replace_strict(code_to_idx, default=-1).alias('code_idx')
        ])

        i_row = all_pl['date_idx'].to_numpy()
        j_col = all_pl['code_idx'].to_numpy()

        def fill_matrix_polars(col_name: str, target_matrix: np.ndarray, dtype=np.float32):
            if col_name in all_pl.columns:
                vals = all_pl[col_name].to_numpy().astype(dtype)
                non_nan = np.sum(~np.isnan(vals))
                print(f"    fill {col_name}: non_nan={non_nan}, sample={vals[:5]}")
                self._execute_fill(target_matrix, i_row, j_col, vals, name=col_name)
            else:
                print(f"    skip {col_name}: not in columns")

        fill_matrix_polars('close', self.close)
        fill_matrix_polars('open', self.open)
        fill_matrix_polars('high', self.high)
        fill_matrix_polars('low', self.low)
        fill_matrix_polars('volume', self.volume)
        fill_matrix_polars('amount', self.amount)
        fill_matrix_polars('total_mv', self.total_mv)
        fill_matrix_polars('is_st', self.is_st, dtype=np.int8)
        fill_matrix_polars('volume_ratio', self.volume_ratio)
        fill_matrix_polars('turnover_rate', self.turnover_rate)
        fill_matrix_polars('ma5', self.ma5)
        fill_matrix_polars('ma10', self.ma10)
        fill_matrix_polars('ma20', self.ma20)
        fill_matrix_polars('volume_ma5', self.volume_ma5)

        # [引擎注入因子] 在 fill 之后再次注入，避免被覆盖
        injected_factors = getattr(self, 'factors', None)
        if injected_factors and isinstance(injected_factors, dict):
            print(f"[prepare_data] 重新注入因子（避免被覆盖）: {list(injected_factors.keys())}")
            for factor_name, matrix in injected_factors.items():
                if matrix is not None and isinstance(matrix, np.ndarray):
                    non_nan = np.sum(~np.isnan(matrix))
                    print(f"  {factor_name}: shape={matrix.shape}, non_nan={non_nan}")
                    setattr(self, factor_name, matrix.astype(np.float32))

        if price_matrix is not None:
            if np.all(np.isnan(self.close)):
                self.close = price_matrix[:, :, 3].astype(np.float32)
            if np.all(np.isnan(self.open)):
                self.open = price_matrix[:, :, 0].astype(np.float32)
            if np.all(np.isnan(self.high)):
                self.high = price_matrix[:, :, 1].astype(np.float32)
            if np.all(np.isnan(self.low)):
                self.low = price_matrix[:, :, 2].astype(np.float32)

        if 'list_date' in all_pl.columns:
            unique_dates = all_pl.select(['stock_code', 'list_date']).unique('stock_code')
            list_date_dict = {}
            for row in unique_dates.iter_rows(named=True):
                try:
                    ld_str = str(row['list_date'])
                    if ld_str and ld_str != 'nan':
                        list_date_dict[row['stock_code']] = pd.Timestamp(ld_str).toordinal()
                except Exception:
                    pass

            if list_date_dict:
                list_date_arr = np.array([list_date_dict.get(c, np.nan) for c in stock_codes], dtype=np.float64)
                date_ords = np.array([pd.Timestamp(d).toordinal() for d in trading_dates], dtype=np.float64)
                self.days_listed = (date_ords[:, None] - list_date_arr[None, :]).astype(np.float64)

    def _fill_from_pandas(
        self,
        preloaded_data: Dict[str, Any],
        trading_dates: List[str],
        stock_codes: List[str],
        price_matrix: Optional[np.ndarray]
    ) -> None:
        """从 Pandas DataFrame 填充矩阵。"""
        T, N = self.T, self.N

        valid_dfs = [df for df in preloaded_data.values() if df is not None and not df.empty]
        if not valid_dfs:
            return

        all_df = pd.concat(valid_dfs, ignore_index=True)

        # [修复] 时间精度对齐：强制统一为日期格式
        if 'trade_date' in all_df.columns:
            all_df['trade_date'] = pd.to_datetime(all_df['trade_date']).dt.date.astype(str)
            print("[Data Alignment] trade_date 精度已统一为 YYYY-MM-DD 格式")

        # 构建坐标索引 (Categorical 映射)
        all_df['trade_date_cat'] = pd.Categorical(all_df['trade_date'], categories=trading_dates, ordered=True)
        all_df['stock_code_cat'] = pd.Categorical(all_df['stock_code'], categories=stock_codes, ordered=True)

        i_row = all_df['trade_date_cat'].cat.codes.values
        j_col = all_df['stock_code_cat'].cat.codes.values

        mask = (i_row >= 0) & (j_col >= 0)
        i_row = i_row[mask]
        j_col = j_col[mask]

        def fill_matrix(col_name: str, target_matrix: np.ndarray, dtype=np.float32, default_val=np.nan):
            if col_name in all_df.columns:
                filtered_df = all_df[mask]
                vals = filtered_df[col_name].values.astype(dtype)
                self._execute_fill(target_matrix, i_row, j_col, vals, name=col_name)
            elif default_val is not None:
                pass

        fill_matrix('close', self.close)
        fill_matrix('open', self.open)
        fill_matrix('high', self.high)
        fill_matrix('low', self.low)
        fill_matrix('volume', self.volume)
        fill_matrix('amount', self.amount)
        fill_matrix('total_mv', self.total_mv)
        fill_matrix('is_st', self.is_st, dtype=np.int8)
        fill_matrix('volume_ratio', self.volume_ratio)
        fill_matrix('turnover_rate', self.turnover_rate)
        fill_matrix('ma5', self.ma5)
        fill_matrix('ma10', self.ma10)
        fill_matrix('ma20', self.ma20)
        fill_matrix('volume_ma5', self.volume_ma5)

        if price_matrix is not None:
            if np.all(np.isnan(self.close)):
                self.close = price_matrix[:, :, 3].astype(np.float32)
            if np.all(np.isnan(self.open)):
                self.open = price_matrix[:, :, 0].astype(np.float32)
            if np.all(np.isnan(self.high)):
                self.high = price_matrix[:, :, 1].astype(np.float32)
            if np.all(np.isnan(self.low)):
                self.low = price_matrix[:, :, 2].astype(np.float32)

        # 计算上市天数矩阵
        list_date_dict = {}
        if 'list_date' in all_df.columns:
            unique_dates = all_df[['stock_code', 'list_date']].drop_duplicates('stock_code')
            unique_dates = unique_dates[unique_dates['stock_code'].isin(stock_codes)]

            for row in unique_dates.itertuples(index=False):
                try:
                    ld_val = row.list_date
                    if pd.isna(ld_val) or ld_val == 0:
                        continue

                    if isinstance(ld_val, (int, np.integer)):
                        ld_str = str(int(ld_val))
                        if len(ld_str) == 8:
                            list_date_dt = pd.to_datetime(ld_str, format='%Y%m%d')
                            list_date_dict[row.stock_code] = list_date_dt.toordinal()
                    elif isinstance(ld_val, str) and ld_val != 'nan':
                        list_date_dt = pd.Timestamp(ld_val)
                        list_date_dict[row.stock_code] = list_date_dt.toordinal()
                except Exception:
                    pass

        if list_date_dict:
            list_date_arr = np.array([list_date_dict.get(c, np.nan) for c in stock_codes], dtype=np.float64)
            date_ords = np.array([pd.Timestamp(d).toordinal() for d in trading_dates], dtype=np.float64)
            self.days_listed = (date_ords[:, None] - list_date_arr[None, :]).astype(np.float64)
            print(f"[VectorizedBase] [OK] days_listed calculated: {np.sum(~np.isnan(self.days_listed))} valid entries")
        else:
            print("[VectorizedBase] [WARN] No list_date data found, days_listed will be NaN")

    def _load_required_factors(self) -> None:
        """加载策略声明的因子。"""
        if not self.required_factors:
            return

        try:
            from core.quant.strategies.utils.factor_calculator import get_factor_calculator

            calculator = get_factor_calculator()
            factor_data = calculator.load_factors(
                self.required_factors,
                self._trading_dates,
                self._stock_codes,
            )

            self.factors = factor_data

            for factor_name, matrix in factor_data.items():
                setattr(self, factor_name, matrix)

            missing = set(self.required_factors) - set(factor_data.keys())
            if missing:
                print(f"[VectorizedBase] [WARN] 未找到因子: {missing}")

        except Exception as e:
            print(f"[VectorizedBase] [ERROR] 加载因子失败: {e}")

    def generate_signals_vectorized(
        self,
        price_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
        data_query,
        preloaded_data: Optional[Dict[str, pd.DataFrame]] = None,
        price_matrix_adj: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """
        向量化信号生成（基类实现）

        子类应该重写此方法，但可以调用 super().prepare_data() 来准备数据

        参数:
            price_matrix: 价格矩阵 (T, N, 4) - [open, high, low, close]（不复权价格，用于交易）
            trading_dates: 交易日期列表 (T,)
            stock_codes: 股票代码列表 (N,)
            data_query: 数据查询对象
            preloaded_data: 预加载的全量数据 Dict[str, pd.DataFrame]
            price_matrix_adj: 前复权价格矩阵 (T, N, 4) - [open_adj, high_adj, low_adj, close_adj]
                              用于指标计算（MA等），消除除权除息影响

        返回:
            signal_matrix: (T, N) int32 - 0=hold, 1=buy, 2=sell

        动态复权说明:
            - price_matrix: 不复权价格，用于交易执行（真实成交价）
            - price_matrix_adj: 前复权价格，用于指标计算（避免除权除息跳变）
            - 如果 price_matrix_adj 为 None，则使用 price_matrix
        """
        self.price_matrix = price_matrix
        self.price_matrix_adj = price_matrix_adj if price_matrix_adj is not None else price_matrix

        # 准备数据（子类可以重写此方法，但通常调用 prepare_data 即可）
        self.prepare_data(preloaded_data, trading_dates, stock_codes)

        # 如果子类没有重写，返回空信号矩阵
        T, N = len(trading_dates), len(stock_codes)
        return np.zeros((T, N), dtype=np.int32)

    def get_indicators_at(self, t_idx: int, n_idx: int) -> Dict[str, float]:
        """
        获取指定日期和股票的指标快照

        参数:
            t_idx: 日期索引
            n_idx: 股票索引

        返回:
            Dict: 指标名称 -> 值
        """
        indicators = {}

        if hasattr(self, 'volume_ratio') and self.volume_ratio is not None:
            val = self.volume_ratio[t_idx, n_idx]
            indicators['volume_ratio'] = float(val) if not np.isnan(val) else 0.0

        if hasattr(self, 'turnover_rate') and self.turnover_rate is not None:
            val = self.turnover_rate[t_idx, n_idx]
            indicators['turnover_rate'] = float(val) if not np.isnan(val) else 0.0

        if hasattr(self, 'days_listed') and self.days_listed is not None:
            val = self.days_listed[t_idx, n_idx]
            indicators['days_listed'] = int(val) if not np.isnan(val) else 0

        if hasattr(self, 'gain_3d') and getattr(self, 'gain_3d') is not None:
            val = getattr(self, 'gain_3d')[t_idx, n_idx]
            indicators['gain_3d'] = float(val) if not np.isnan(val) else 0.0

        return indicators
