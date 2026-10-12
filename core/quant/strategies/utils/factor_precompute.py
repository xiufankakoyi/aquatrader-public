"""
因子预计算引擎 - 高性能因子计算与缓存

核心优化：
1. 因子DAG依赖管理 - 避免重复计算
2. Numba JIT加速 - C级别性能
3. Bottleneck优化 - 滚动窗口计算
4. HDF5持久化 - 预计算因子存储
5. 智能批处理 - 批量计算多个因子
"""

import hashlib
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from functools import wraps

import numpy as np

from config.logger import get_logger

logger = get_logger(__name__)

# 尝试导入加速库
try:
    import bottleneck as bn
    BOTTLENECK_AVAILABLE = True
except ImportError:
    BOTTLENECK_AVAILABLE = False
    logger.warning("[FactorPrecompute] bottleneck 未安装，使用 NumPy 降级方案")

try:
    from numba import njit, prange
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    logger.warning("[FactorPrecompute] numba 未安装，使用纯 NumPy 实现")
    
    # 降级装饰器
    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if not args else decorator(args[0])
    prange = range


@dataclass
class FactorNode:
    """因子节点"""
    name: str
    dependencies: Set[str] = field(default_factory=set)
    compute_func: Optional[Callable] = None
    cache_key: str = ""
    
    
class FactorDAG:
    """
    因子依赖图
    
    自动管理因子依赖关系，确保按正确顺序计算
    """
    
    def __init__(self):
        self.nodes: Dict[str, FactorNode] = {}
        self._lock = threading.RLock()
    
    def add_factor(
        self,
        name: str,
        dependencies: List[str],
        compute_func: Callable
    ) -> None:
        """添加因子定义"""
        with self._lock:
            self.nodes[name] = FactorNode(
                name=name,
                dependencies=set(dependencies),
                compute_func=compute_func,
                cache_key=f"factor_{name}"
            )
    
    def get_compute_order(self, target_factors: List[str]) -> List[str]:
        """
        获取计算顺序 (拓扑排序)
        
        确保依赖因子先计算
        """
        with self._lock:
            # 收集所有需要的因子
            required = set()
            queue = list(target_factors)
            
            while queue:
                factor = queue.pop(0)
                if factor not in required and factor in self.nodes:
                    required.add(factor)
                    queue.extend(self.nodes[factor].dependencies)
            
            # Kahn算法拓扑排序
            in_degree = {f: 0 for f in required}
            for f in required:
                for dep in self.nodes[f].dependencies:
                    if dep in required:
                        in_degree[f] += 1
            
            # 找到入度为0的节点
            queue = [f for f in required if in_degree[f] == 0]
            result = []
            
            while queue:
                # 按名称排序确保确定性
                queue.sort()
                factor = queue.pop(0)
                result.append(factor)
                
                # 更新依赖该因子的节点
                for f in required:
                    if factor in self.nodes[f].dependencies:
                        in_degree[f] -= 1
                        if in_degree[f] == 0:
                            queue.append(f)
            
            return result


class FactorPrecomputeEngine:
    """
    因子预计算引擎
    
    单例模式，全局管理因子计算和缓存
    """
    
    _instance: Optional['FactorPrecomputeEngine'] = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(
        self,
        cache_dir: str = "data/cache/factors",
        max_memory_factors: int = 50,
        enable_disk_cache: bool = True
    ):
        if hasattr(self, '_initialized') and self._initialized:
            return
        
        self._initialized = True
        self.logger = get_logger(__name__)
        
        # 配置
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_memory_factors = max_memory_factors
        self.enable_disk_cache = enable_disk_cache
        
        # 因子DAG
        self.dag = FactorDAG()
        
        # 内存缓存
        self._memory_cache: Dict[str, np.ndarray] = {}
        self._cache_access_time: Dict[str, float] = {}
        self._cache_lock = threading.RLock()
        
        # 注册内置因子
        self._register_builtin_factors()
        
        self.logger.info(f"[FactorEngine] 初始化完成，缓存目录: {self.cache_dir}")
    
    # ========================================================================
    # 核心 API
    # ========================================================================
    
    def compute_factors(
        self,
        close_matrix: np.ndarray,
        factor_names: List[str],
        date_range: Tuple[str, str],
        extra_data: Optional[Dict[str, np.ndarray]] = None
    ) -> Dict[str, np.ndarray]:
        """
        计算多个因子
        
        Args:
            close_matrix: 收盘价矩阵 (T, N)
            factor_names: 因子名称列表
            date_range: 日期范围 (start, end)
            extra_data: 额外数据 (volume, high, low等)
            
        Returns:
            因子矩阵字典
        """
        # 获取计算顺序
        compute_order = self.dag.get_compute_order(factor_names)
        
        # 准备基础数据
        base_data = {'close': close_matrix}
        if extra_data:
            base_data.update(extra_data)
        
        # 存储计算结果
        results = {}
        computed = {}
        
        for factor_name in compute_order:
            # 检查缓存
            cached = self._get_cached_factor(factor_name, date_range, close_matrix.shape)
            if cached is not None:
                computed[factor_name] = cached
                results[factor_name] = cached
                continue
            
            # 获取计算函数
            node = self.dag.nodes.get(factor_name)
            if node is None or node.compute_func is None:
                self.logger.warning(f"[FactorEngine] 未知因子: {factor_name}")
                continue
            
            # 准备依赖数据
            dep_data = {}
            for dep in node.dependencies:
                if dep in computed:
                    dep_data[dep] = computed[dep]
                elif dep in base_data:
                    dep_data[dep] = base_data[dep]
                else:
                    self.logger.error(f"[FactorEngine] 缺少依赖: {dep}")
                    break
            else:
                # 计算因子
                t_start = time.perf_counter()
                factor_matrix = node.compute_func(**dep_data)
                t_end = time.perf_counter()
                
                self.logger.debug(f"[FactorEngine] 计算 {factor_name}: {(t_end-t_start)*1000:.2f}ms")
                
                # 缓存结果
                self._cache_factor(factor_name, date_range, factor_matrix)
                
                computed[factor_name] = factor_matrix
                if factor_name in factor_names:
                    results[factor_name] = factor_matrix
        
        return results
    
    def compute_single(
        self,
        close_matrix: np.ndarray,
        factor_name: str,
        params: Optional[Dict[str, Any]] = None
    ) -> Optional[np.ndarray]:
        """
        计算单个因子 (便捷方法)
        """
        # 动态参数因子处理
        if factor_name.startswith('MA'):
            window = int(factor_name[2:])
            return self._calc_ma(close_matrix, window)
        elif factor_name.startswith('RSI'):
            period = int(factor_name[3:])
            return self._calc_rsi(close_matrix, period)
        
        # 标准因子
        results = self.compute_factors(
            close_matrix,
            [factor_name],
            ("", ""),  # 简化日期范围
        )
        
        return results.get(factor_name)
    
    def batch_compute(
        self,
        close_matrix: np.ndarray,
        factor_configs: List[Dict[str, Any]]
    ) -> Dict[str, np.ndarray]:
        """
        批量计算因子
        
        Args:
            factor_configs: [{"name": "MA20", "params": {"window": 20}}, ...]
            
        Returns:
            所有因子结果
        """
        all_results = {}
        
        for config in factor_configs:
            name = config['name']
            params = config.get('params', {})
            
            # 检查是否有专用计算函数
            if name in self.dag.nodes:
                result = self.compute_single(close_matrix, name)
                if result is not None:
                    all_results[name] = result
            else:
                # 动态计算
                result = self._compute_dynamic_factor(close_matrix, name, params)
                if result is not None:
                    all_results[name] = result
        
        return all_results
    
    def clear_cache(self) -> None:
        """清除所有缓存"""
        with self._cache_lock:
            self._memory_cache.clear()
            self._cache_access_time.clear()
        
        if self.enable_disk_cache:
            for f in self.cache_dir.glob("*.h5"):
                f.unlink()
        
        self.logger.info("[FactorEngine] 缓存已清除")
    
    # ========================================================================
    # 内置因子计算 (Numba加速)
    # ========================================================================
    
    def _register_builtin_factors(self) -> None:
        """注册内置因子"""
        # MA 系列
        self.dag.add_factor('MA5', ['close'], self._calc_ma_wrapper(5))
        self.dag.add_factor('MA10', ['close'], self._calc_ma_wrapper(10))
        self.dag.add_factor('MA20', ['close'], self._calc_ma_wrapper(20))
        self.dag.add_factor('MA60', ['close'], self._calc_ma_wrapper(60))
        
        # RSI
        self.dag.add_factor('RSI14', ['close'], self._calc_rsi_wrapper(14))
        self.dag.add_factor('RSI6', ['close'], self._calc_rsi_wrapper(6))
        
        # 涨幅
        self.dag.add_factor('GAIN_1D', ['close'], self._calc_gain_wrapper(1))
        self.dag.add_factor('GAIN_5D', ['close'], self._calc_gain_wrapper(5))
        self.dag.add_factor('GAIN_20D', ['close'], self._calc_gain_wrapper(20))
        
        # 波动率
        self.dag.add_factor('VOLATILITY_20', ['close'], self._calc_volatility_wrapper(20))
        
        # MACD
        self.dag.add_factor('MACD_DIF', ['close'], self._calc_macd_dif)
        self.dag.add_factor('MACD_DEA', ['MACD_DIF'], self._calc_macd_dea)
        self.dag.add_factor('MACD_HIST', ['MACD_DIF', 'MACD_DEA'], self._calc_macd_hist)
        
        # 形态因子
        self._register_pattern_factors()
    
    def _register_pattern_factors(self) -> None:
        """注册形态识别因子"""
        try:
            from core.quant.factors.pattern_factors import PatternFactorCalculator, PATTERN_FACTORS
            
            for factor_name, config in PATTERN_FACTORS.items():
                self.dag.add_factor(
                    factor_name,
                    config['dependencies'],
                    config['function']
                )
                self.logger.debug(f"[FactorEngine] 注册形态因子: {factor_name}")
            
            self.logger.info(f"[FactorEngine] 已注册 {len(PATTERN_FACTORS)} 个形态因子")
        except ImportError as e:
            self.logger.warning(f"[FactorEngine] 形态因子模块未加载: {e}")
    
    def _calc_ma_wrapper(self, window: int) -> Callable:
        """MA计算包装器"""
        def wrapper(close: np.ndarray) -> np.ndarray:
            return self._calc_ma(close, window)
        return wrapper
    
    def _calc_rsi_wrapper(self, period: int) -> Callable:
        """RSI计算包装器"""
        def wrapper(close: np.ndarray) -> np.ndarray:
            return self._calc_rsi(close, period)
        return wrapper
    
    def _calc_gain_wrapper(self, window: int) -> Callable:
        """涨幅计算包装器"""
        def wrapper(close: np.ndarray) -> np.ndarray:
            return self._calc_gain(close, window)
        return wrapper
    
    def _calc_volatility_wrapper(self, window: int) -> Callable:
        """波动率计算包装器"""
        def wrapper(close: np.ndarray) -> np.ndarray:
            return self._calc_volatility(close, window)
        return wrapper
    
    @staticmethod
    def _calc_ma(close: np.ndarray, window: int) -> np.ndarray:
        """移动平均 (使用bottleneck加速)"""
        if BOTTLENECK_AVAILABLE:
            return bn.move_mean(close, window=window, axis=0)
        else:
            # NumPy实现
            kernel = np.ones(window) / window
            result = np.apply_along_axis(
                lambda x: np.convolve(x, kernel, mode='same'),
                axis=0,
                arr=close
            )
            return result
    
    @staticmethod
    def _calc_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
        """RSI相对强弱指标"""
        if NUMBA_AVAILABLE:
            return _calc_rsi_numba(close, period)
        else:
            return _calc_rsi_numpy(close, period)
    
    @staticmethod
    def _calc_gain(close: np.ndarray, window: int) -> np.ndarray:
        """N日涨幅"""
        if window == 1:
            # 当日涨幅
            return np.concatenate([
                np.zeros((1, close.shape[1])),
                (close[1:] - close[:-1]) / close[:-1] * 100
            ], axis=0)
        else:
            # N日涨幅
            result = np.full_like(close, np.nan)
            result[window:] = (close[window:] - close[:-window]) / close[:-window] * 100
            return result
    
    @staticmethod
    def _calc_volatility(close: np.ndarray, window: int = 20) -> np.ndarray:
        """滚动波动率"""
        # 计算收益率
        returns = np.concatenate([
            np.zeros((1, close.shape[1])),
            (close[1:] - close[:-1]) / close[:-1]
        ], axis=0)
        
        if BOTTLENECK_AVAILABLE:
            return bn.move_std(returns, window=window, axis=0) * np.sqrt(252) * 100
        else:
            # NumPy实现
            result = np.full_like(close, np.nan)
            for i in range(window, len(close)):
                result[i] = np.std(returns[i-window:i], axis=0) * np.sqrt(252) * 100
            return result
    
    @staticmethod
    def _calc_macd_dif(close: np.ndarray) -> np.ndarray:
        """MACD DIF线"""
        ema12 = _calc_ema(close, 12)
        ema26 = _calc_ema(close, 26)
        return ema12 - ema26
    
    @staticmethod
    def _calc_macd_dea(dif: np.ndarray) -> np.ndarray:
        """MACD DEA线"""
        return _calc_ema(dif, 9)
    
    @staticmethod
    def _calc_macd_hist(dif: np.ndarray, dea: np.ndarray) -> np.ndarray:
        """MACD柱状图"""
        return (dif - dea) * 2
    
    def _compute_dynamic_factor(
        self,
        close: np.ndarray,
        name: str,
        params: Dict[str, Any]
    ) -> Optional[np.ndarray]:
        """动态计算因子"""
        # 解析因子名称和参数
        if name.startswith('MA'):
            window = params.get('window', int(name[2:]) if len(name) > 2 else 20)
            return self._calc_ma(close, window)
        
        elif name.startswith('RSI'):
            period = params.get('period', int(name[3:]) if len(name) > 3 else 14)
            return self._calc_rsi(close, period)
        
        elif name.startswith('GAIN'):
            window = params.get('window', int(name.split('_')[1][:-1]) if '_' in name else 1)
            return self._calc_gain(close, window)
        
        return None
    
    # ========================================================================
    # 缓存管理
    # ========================================================================
    
    def _get_cache_key(
        self,
        factor_name: str,
        date_range: Tuple[str, str],
        shape: Tuple[int, ...]
    ) -> str:
        """生成缓存key"""
        key_data = {
            'factor': factor_name,
            'start': date_range[0],
            'end': date_range[1],
            'shape': shape
        }
        return hashlib.sha256(json.dumps(key_data).encode()).hexdigest()
    
    def _get_cached_factor(
        self,
        factor_name: str,
        date_range: Tuple[str, str],
        shape: Tuple[int, ...]
    ) -> Optional[np.ndarray]:
        """获取缓存的因子"""
        cache_key = self._get_cache_key(factor_name, date_range, shape)
        
        # 内存缓存
        with self._cache_lock:
            if cache_key in self._memory_cache:
                self._cache_access_time[cache_key] = time.time()
                return self._memory_cache[cache_key]
        
        # 磁盘缓存
        if self.enable_disk_cache:
            disk_data = self._load_from_disk(cache_key)
            if disk_data is not None:
                # 加载到内存
                self._store_to_memory(cache_key, disk_data)
                return disk_data
        
        return None
    
    def _cache_factor(
        self,
        factor_name: str,
        date_range: Tuple[str, str],
        data: np.ndarray
    ) -> None:
        """缓存因子"""
        cache_key = self._get_cache_key(factor_name, date_range, data.shape)
        
        # 内存缓存
        self._store_to_memory(cache_key, data)
        
        # 磁盘缓存
        if self.enable_disk_cache:
            self._save_to_disk(cache_key, data, factor_name)
    
    def _store_to_memory(self, key: str, data: np.ndarray) -> None:
        """存储到内存缓存 (LRU)"""
        with self._cache_lock:
            # 淘汰旧缓存
            while len(self._memory_cache) >= self.max_memory_factors:
                oldest_key = min(self._cache_access_time, key=self._cache_access_time.get)
                del self._memory_cache[oldest_key]
                del self._cache_access_time[oldest_key]
            
            self._memory_cache[key] = data
            self._cache_access_time[key] = time.time()
    
    def _load_from_disk(self, key: str) -> Optional[np.ndarray]:
        """从磁盘加载"""
        try:
            import h5py
            cache_file = self.cache_dir / f"{key}.h5"
            
            if not cache_file.exists():
                return None
            
            with h5py.File(cache_file, 'r') as f:
                return f['data'][:]
                
        except Exception as e:
            self.logger.warning(f"[FactorEngine] 磁盘加载失败: {e}")
            return None
    
    def _save_to_disk(self, key: str, data: np.ndarray, factor_name: str) -> None:
        """保存到磁盘"""
        try:
            import h5py
            cache_file = self.cache_dir / f"{key}.h5"
            
            with h5py.File(cache_file, 'w') as f:
                f.create_dataset('data', data=data, compression='gzip')
                f.attrs['factor_name'] = factor_name
                f.attrs['created_at'] = time.time()
                
        except Exception as e:
            self.logger.warning(f"[FactorEngine] 磁盘保存失败: {e}")


# ========================================================================
# Numba加速函数
# ========================================================================

@njit(parallel=True, cache=True)
def _calc_rsi_numba(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Numba加速的RSI计算"""
    T, N = close.shape
    rsi = np.full_like(close, np.nan)
    
    for j in prange(N):
        gains = 0.0
        losses = 0.0
        
        # 初始化
        for i in range(1, period + 1):
            change = close[i, j] - close[i-1, j]
            if change > 0:
                gains += change
            else:
                losses -= change
        
        avg_gain = gains / period
        avg_loss = losses / period
        
        if avg_loss != 0:
            rs = avg_gain / avg_loss
            rsi[period, j] = 100 - (100 / (1 + rs))
        else:
            rsi[period, j] = 100
        
        # 后续计算
        for i in range(period + 1, T):
            change = close[i, j] - close[i-1, j]
            gain = max(change, 0)
            loss = abs(min(change, 0))
            
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            
            if avg_loss != 0:
                rs = avg_gain / avg_loss
                rsi[i, j] = 100 - (100 / (1 + rs))
            else:
                rsi[i, j] = 100
    
    return rsi


def _calc_rsi_numpy(close: np.ndarray, period: int = 14) -> np.ndarray:
    """纯NumPy RSI实现 (降级方案)"""
    # 计算价格变化
    delta = np.diff(close, axis=0)
    delta = np.concatenate([np.zeros((1, close.shape[1])), delta], axis=0)
    
    # 分离涨跌
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    
    # 计算平均涨跌
    avg_gain = np.full_like(close, np.nan)
    avg_loss = np.full_like(close, np.nan)
    
    for i in range(period, len(close)):
        if i == period:
            avg_gain[i] = np.mean(gain[1:period+1], axis=0)
            avg_loss[i] = np.mean(loss[1:period+1], axis=0)
        else:
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gain[i]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + loss[i]) / period
    
    # 计算RSI
    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def _calc_ema(data: np.ndarray, span: int) -> np.ndarray:
    """计算EMA"""
    alpha = 2 / (span + 1)
    ema = np.full_like(data, np.nan)
    
    # 第一个值用SMA
    ema[span-1] = np.mean(data[:span], axis=0)
    
    # 后续用EMA公式
    for i in range(span, len(data)):
        ema[i] = alpha * data[i] + (1 - alpha) * ema[i-1]
    
    return ema


# 全局实例
def get_factor_engine(**kwargs) -> FactorPrecomputeEngine:
    """获取因子引擎实例"""
    return FactorPrecomputeEngine(**kwargs)
