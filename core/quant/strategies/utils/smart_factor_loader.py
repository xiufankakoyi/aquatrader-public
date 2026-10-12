"""
因子智能加载器 - 数据库优先 + 按需计算 + 结果缓存

设计原则：
1. 数据库已有因子直接加载（ma5, ma10, ma20, volume_ma5 等）
2. 缺失因子按需计算（RSI, KDJ, MACD, ATR, BOLL, BIAS 等）
3. 计算结果缓存，避免重复计算
4. 向量化计算，使用 Numba 加速

性能优化：
- 数据库因子：O(1) 直接读取
- 计算因子：O(T*N) 向量化 + Numba JIT
- 缓存命中：O(1)
"""

import numpy as np
from typing import Dict, List, Optional, Tuple, Any, Set
from functools import lru_cache
import time

from config.logger import get_logger

logger = get_logger(__name__)

# 尝试导入 Numba
try:
    from numba import njit, prange
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if not args else decorator(args[0])
    prange = range


# =============================================================================
# 数据库已有因子映射
# =============================================================================
DB_AVAILABLE_FACTORS: Set[str] = {
    # 价格数据
    'close', 'open', 'high', 'low', 'prev_close',
    # 成交量数据
    'volume', 'amount', 'volume_ratio', 'turnover_rate', 'turnover_free',
    # 市值与基本面
    'total_mv', 'float_mv', 'pe', 'pe_ttm', 'pb', 'ps', 'ps_ttm',
    'dividend_yield', 'dividend_yield_ttm',
    # 技术指标 - 已在数据库
    'ma5', 'ma10', 'ma20',
    'ma3_avg_price', 'ma5_avg_price', 'ma10_avg_price',
    'volume_ma5',
    # 涨跌停
    'limit_up', 'limit_down',
    # 复权因子
    'adj_factor',
}

# 需要计算的因子及其依赖
COMPUTE_FACTORS: Dict[str, Dict[str, Any]] = {
    # RSI 系列
    'rsi_6': {'deps': ['close'], 'params': {'period': 6}},
    'rsi_14': {'deps': ['close'], 'params': {'period': 14}},
    'rsi_24': {'deps': ['close'], 'params': {'period': 24}},
    
    # KDJ 系列
    'kdj_k': {'deps': ['high', 'low', 'close'], 'params': {'n': 9}},
    'kdj_d': {'deps': ['kdj_k'], 'params': {'m1': 3}},
    'kdj_j': {'deps': ['kdj_k', 'kdj_d'], 'params': {}},
    
    # MACD 系列
    'macd_dif': {'deps': ['close'], 'params': {'fast': 12, 'slow': 26}},
    'macd_dea': {'deps': ['macd_dif'], 'params': {'signal': 9}},
    'macd_hist': {'deps': ['macd_dif', 'macd_dea'], 'params': {}},
    
    # ATR
    'atr_14': {'deps': ['high', 'low', 'close'], 'params': {'period': 14}},
    
    # BOLL 系列
    'boll_upper': {'deps': ['close'], 'params': {'period': 20, 'std_dev': 2}},
    'boll_mid': {'deps': ['close'], 'params': {'period': 20}},
    'boll_lower': {'deps': ['close'], 'params': {'period': 20, 'std_dev': 2}},
    
    # BIAS 系列
    'bias_5': {'deps': ['close', 'ma5'], 'params': {}},
    'bias_10': {'deps': ['close', 'ma10'], 'params': {}},
    'bias_20': {'deps': ['close', 'ma20'], 'params': {}},
    
    # 涨幅
    'gain_1d': {'deps': ['close'], 'params': {'window': 1}},
    'gain_3d': {'deps': ['close'], 'params': {'window': 3}},
    'gain_5d': {'deps': ['close'], 'params': {'window': 5}},
    'gain_10d': {'deps': ['close'], 'params': {'window': 10}},
    
    # 波动率
    'volatility_10': {'deps': ['close'], 'params': {'window': 10}},
    'volatility_20': {'deps': ['close'], 'params': {'window': 20}},
    
    # MA 扩展
    'ma60': {'deps': ['close'], 'params': {'window': 60}},
    'ma120': {'deps': ['close'], 'params': {'window': 120}},
}


class SmartFactorLoader:
    """
    智能因子加载器
    
    核心功能：
    1. 数据库因子直接读取
    2. 计算因子按需生成
    3. 结果缓存
    """
    
    _instance: Optional['SmartFactorLoader'] = None
    _lock = __import__('threading').Lock()
    
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
        self.logger = get_logger(__name__)
        
        # 因子缓存：{(strategy_id, factor_name): matrix}
        self._factor_cache: Dict[Tuple[int, str], np.ndarray] = {}
        
        # 策略数据引用
        self._strategy_data: Dict[int, Dict[str, np.ndarray]] = {}
        
        self.logger.info(f"[SmartFactorLoader] 初始化完成，数据库因子: {len(DB_AVAILABLE_FACTORS)}，计算因子: {len(COMPUTE_FACTORS)}")
    
    def clear_cache(self):
        """清除缓存"""
        self._factor_cache.clear()
        self._strategy_data.clear()
    
    def register_strategy_data(
        self,
        strategy_id: int,
        data: Dict[str, np.ndarray]
    ) -> None:
        """
        注册策略的基础数据
        
        Args:
            strategy_id: 策略实例 id
            data: 基础数据字典 {factor_name: matrix}
        """
        self._strategy_data[strategy_id] = data
    
    def get_factor(
        self,
        factor_name: str,
        strategy_id: int,
        strategy_data: Optional[Dict[str, np.ndarray]] = None
    ) -> Optional[np.ndarray]:
        """
        获取因子数据
        
        Args:
            factor_name: 因子名称
            strategy_id: 策略实例 id
            strategy_data: 策略数据字典（可选，用于首次注册）
        
        Returns:
            因子矩阵 (T, N) 或 None
        """
        # 注册数据
        if strategy_data is not None and strategy_id not in self._strategy_data:
            self._strategy_data[strategy_id] = strategy_data
        
        # 检查缓存
        cache_key = (strategy_id, factor_name)
        if cache_key in self._factor_cache:
            return self._factor_cache[cache_key]
        
        # 获取策略数据
        data = self._strategy_data.get(strategy_id)
        if data is None:
            self.logger.warning(f"[SmartFactorLoader] 策略 {strategy_id} 未注册数据")
            return None
        
        # 1. 检查数据库因子
        if factor_name in DB_AVAILABLE_FACTORS:
            factor_matrix = data.get(factor_name)
            if factor_matrix is not None:
                self._factor_cache[cache_key] = factor_matrix
                return factor_matrix
            else:
                self.logger.debug(f"[SmartFactorLoader] 数据库因子 {factor_name} 未在数据中")
        
        # 2. 检查计算因子
        if factor_name in COMPUTE_FACTORS:
            factor_matrix = self._compute_factor(factor_name, data)
            if factor_matrix is not None:
                self._factor_cache[cache_key] = factor_matrix
                return factor_matrix
        
        # 3. 尝试从策略数据直接获取
        factor_matrix = data.get(factor_name)
        if factor_matrix is not None:
            self._factor_cache[cache_key] = factor_matrix
            return factor_matrix
        
        self.logger.warning(f"[SmartFactorLoader] 未知因子: {factor_name}")
        return None
    
    def _compute_factor(
        self,
        factor_name: str,
        data: Dict[str, np.ndarray]
    ) -> Optional[np.ndarray]:
        """
        计算因子
        
        Args:
            factor_name: 因子名称
            data: 策略数据字典
        
        Returns:
            计算的因子矩阵
        """
        config = COMPUTE_FACTORS.get(factor_name)
        if config is None:
            return None
        
        # 检查依赖
        deps = config.get('deps', [])
        dep_data = {}
        for dep in deps:
            # 递归获取依赖因子
            dep_matrix = data.get(dep)
            if dep_matrix is None:
                # 尝试计算依赖因子
                dep_matrix = self._compute_factor(dep, data)
            if dep_matrix is None:
                self.logger.debug(f"[SmartFactorLoader] 缺少依赖 {dep}，无法计算 {factor_name}")
                return None
            dep_data[dep] = dep_matrix
        
        # 计算因子
        params = config.get('params', {})
        t_start = time.perf_counter()
        
        try:
            if factor_name.startswith('rsi'):
                result = self._calc_rsi(dep_data['close'], params.get('period', 14))
            elif factor_name.startswith('kdj'):
                result = self._calc_kdj(
                    dep_data['high'], dep_data['low'], dep_data['close'],
                    factor_name, params
                )
            elif factor_name.startswith('macd'):
                result = self._calc_macd(dep_data.get('close'), dep_data, factor_name, params)
            elif factor_name.startswith('atr'):
                result = self._calc_atr(dep_data['high'], dep_data['low'], dep_data['close'], params.get('period', 14))
            elif factor_name.startswith('boll'):
                result = self._calc_boll(dep_data['close'], factor_name, params)
            elif factor_name.startswith('bias'):
                result = self._calc_bias(dep_data['close'], dep_data.get(factor_name.split('_')[1] if '_' in factor_name else 'ma20'), params)
            elif factor_name.startswith('gain'):
                result = self._calc_gain(dep_data['close'], params.get('window', 5))
            elif factor_name.startswith('volatility'):
                result = self._calc_volatility(dep_data['close'], params.get('window', 20))
            elif factor_name.startswith('ma'):
                result = self._calc_ma(dep_data['close'], params.get('window', 20))
            else:
                return None
            
            t_end = time.perf_counter()
            self.logger.debug(f"[SmartFactorLoader] 计算 {factor_name}: {(t_end-t_start)*1000:.2f}ms")
            return result
            
        except Exception as e:
            self.logger.error(f"[SmartFactorLoader] 计算 {factor_name} 失败: {e}")
            return None
    
    # =========================================================================
    # 因子计算函数（Numba 加速）
    # =========================================================================
    
    @staticmethod
    def _calc_ma(close: np.ndarray, window: int) -> np.ndarray:
        """移动平均"""
        T, N = close.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        if NUMBA_AVAILABLE:
            return _calc_ma_numba(close, window)
        
        # NumPy 实现
        for t in range(window - 1, T):
            result[t] = np.nanmean(close[t-window+1:t+1], axis=0)
        
        return result
    
    @staticmethod
    def _calc_rsi(close: np.ndarray, period: int) -> np.ndarray:
        """RSI 相对强弱指标 - 使用纯 NumPy 向量化实现"""
        T, N = close.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        # 计算价格变化
        delta = np.zeros((T, N), dtype=np.float32)
        delta[1:] = close[1:] - close[:-1]
        
        # 分离涨跌
        gain = np.where(delta > 0, delta, 0).astype(np.float32)
        loss = np.where(delta < 0, -delta, 0).astype(np.float32)
        
        # 使用指数移动平均计算（Wilder 平滑）
        alpha = 1.0 / period
        
        # 初始化 EMA
        avg_gain = np.full((N,), np.nan, dtype=np.float32)
        avg_loss = np.full((N,), np.nan, dtype=np.float32)
        
        # 前 period 天使用简单平均
        for col in range(N):
            valid_gains = gain[1:period+1, col]
            valid_losses = loss[1:period+1, col]
            if np.any(~np.isnan(valid_gains)):
                avg_gain[col] = np.nanmean(valid_gains)
            if np.any(~np.isnan(valid_losses)):
                avg_loss[col] = np.nanmean(valid_losses)
        
        # 计算 RSI
        for t in range(period + 1, T):
            # 更新 EMA
            avg_gain = np.where(
                np.isnan(avg_gain), 
                gain[t], 
                alpha * gain[t] + (1 - alpha) * avg_gain
            )
            avg_loss = np.where(
                np.isnan(avg_loss), 
                loss[t], 
                alpha * loss[t] + (1 - alpha) * avg_loss
            )
            
            # 计算 RSI
            with np.errstate(divide='ignore', invalid='ignore'):
                rs = np.where(avg_loss > 1e-10, avg_gain / avg_loss, 100.0)
            result[t] = 100 - 100 / (1 + rs)
        
        # 第 period+1 天也计算
        if period + 1 < T:
            with np.errstate(divide='ignore', invalid='ignore'):
                rs = np.where(avg_loss > 1e-10, avg_gain / avg_loss, 100.0)
            result[period] = 100 - 100 / (1 + rs)
        
        return result
    
    @staticmethod
    def _calc_kdj(high: np.ndarray, low: np.ndarray, close: np.ndarray, factor_name: str, params: dict) -> np.ndarray:
        """KDJ 随机指标"""
        T, N = close.shape
        n = params.get('n', 9)
        m1 = params.get('m1', 3)
        m2 = params.get('m2', 3)
        
        if NUMBA_AVAILABLE:
            return _calc_kdj_numba(high, low, close, n, m1, m2, factor_name)
        
        # NumPy 实现
        k = np.full((T, N), 50.0, dtype=np.float32)
        d = np.full((T, N), 50.0, dtype=np.float32)
        j = np.full((T, N), 50.0, dtype=np.float32)
        
        for t in range(n - 1, T):
            llv = np.nanmin(low[t-n+1:t+1], axis=0)
            hhv = np.nanmax(high[t-n+1:t+1], axis=0)
            
            rsv = np.where(hhv > llv, (close[t] - llv) / (hhv - llv) * 100, 50)
            
            k[t] = k[t-1] * 2/3 + rsv * 1/3 if t > 0 else rsv
            d[t] = d[t-1] * 2/3 + k[t] * 1/3 if t > 0 else k[t]
            j[t] = 3 * k[t] - 2 * d[t]
        
        if 'k' in factor_name.lower():
            return k
        elif 'd' in factor_name.lower():
            return d
        else:
            return j
    
    @staticmethod
    def _calc_macd(close: Optional[np.ndarray], dep_data: dict, factor_name: str, params: dict) -> np.ndarray:
        """MACD 指标"""
        T, N = dep_data['close'].shape if close is None else close.shape
        fast = params.get('fast', 12)
        slow = params.get('slow', 26)
        signal = params.get('signal', 9)
        
        if NUMBA_AVAILABLE:
            return _calc_macd_numba(dep_data.get('close', close), fast, slow, signal, factor_name)
        
        # NumPy 实现
        close_data = dep_data.get('close', close)
        
        # EMA
        def ema(data, period):
            alpha = 2 / (period + 1)
            result = np.zeros_like(data)
            result[0] = data[0]
            for t in range(1, T):
                result[t] = alpha * data[t] + (1 - alpha) * result[t-1]
            return result
        
        ema_fast = ema(close_data, fast)
        ema_slow = ema(close_data, slow)
        dif = ema_fast - ema_slow
        dea = ema(dif, signal)
        hist = (dif - dea) * 2
        
        if 'dif' in factor_name.lower():
            return dif.astype(np.float32)
        elif 'dea' in factor_name.lower():
            return dea.astype(np.float32)
        else:
            return hist.astype(np.float32)
    
    @staticmethod
    def _calc_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
        """ATR 平均真实波幅"""
        T, N = close.shape
        
        if NUMBA_AVAILABLE:
            return _calc_atr_numba(high, low, close, period)
        
        # NumPy 实现
        tr = np.zeros((T, N), dtype=np.float32)
        tr[0] = high[0] - low[0]
        
        for t in range(1, T):
            tr[t] = np.maximum(
                high[t] - low[t],
                np.maximum(
                    np.abs(high[t] - close[t-1]),
                    np.abs(low[t] - close[t-1])
                )
            )
        
        # ATR = EMA(TR)
        result = np.full((T, N), np.nan, dtype=np.float32)
        alpha = 1 / period
        atr = tr[0]
        
        for t in range(period, T):
            if t == period:
                atr = np.nanmean(tr[:period], axis=0)
            else:
                atr = alpha * tr[t] + (1 - alpha) * atr
            result[t] = atr
        
        return result
    
    @staticmethod
    def _calc_boll(close: np.ndarray, factor_name: str, params: dict) -> np.ndarray:
        """布林带"""
        T, N = close.shape
        period = params.get('period', 20)
        std_dev = params.get('std_dev', 2)
        
        if NUMBA_AVAILABLE:
            return _calc_boll_numba(close, period, std_dev, factor_name)
        
        # NumPy 实现
        mid = np.full((T, N), np.nan, dtype=np.float32)
        upper = np.full((T, N), np.nan, dtype=np.float32)
        lower = np.full((T, N), np.nan, dtype=np.float32)
        
        for t in range(period - 1, T):
            window_data = close[t-period+1:t+1]
            mid[t] = np.nanmean(window_data, axis=0)
            std = np.nanstd(window_data, axis=0)
            upper[t] = mid[t] + std_dev * std
            lower[t] = mid[t] - std_dev * std
        
        if 'upper' in factor_name.lower():
            return upper
        elif 'lower' in factor_name.lower():
            return lower
        else:
            return mid
    
    @staticmethod
    def _calc_bias(close: np.ndarray, ma: np.ndarray, params: dict) -> np.ndarray:
        """乖离率"""
        if ma is None:
            return np.full_like(close, np.nan, dtype=np.float32)
        
        with np.errstate(divide='ignore', invalid='ignore'):
            bias = (close - ma) / ma * 100
        return bias.astype(np.float32)
    
    @staticmethod
    def _calc_gain(close: np.ndarray, window: int) -> np.ndarray:
        """N日涨幅"""
        T, N = close.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        if window == 1:
            result[1:] = (close[1:] - close[:-1]) / close[:-1] * 100
        else:
            result[window:] = (close[window:] - close[:-window]) / close[:-window] * 100
        
        return result
    
    @staticmethod
    def _calc_volatility(close: np.ndarray, window: int) -> np.ndarray:
        """滚动波动率"""
        T, N = close.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        # 计算日收益率
        returns = np.zeros((T, N), dtype=np.float32)
        returns[1:] = (close[1:] - close[:-1]) / close[:-1]
        
        # 滚动标准差
        for t in range(window, T):
            result[t] = np.nanstd(returns[t-window:t], axis=0) * np.sqrt(252) * 100
        
        return result
    
    def batch_get_factors(
        self,
        factor_names: List[str],
        strategy_id: int,
        strategy_data: Optional[Dict[str, np.ndarray]] = None
    ) -> Dict[str, np.ndarray]:
        """
        批量获取因子
        
        Args:
            factor_names: 因子名称列表
            strategy_id: 策略实例 id
            strategy_data: 策略数据字典
        
        Returns:
            因子字典 {factor_name: matrix}
        """
        results = {}
        for name in factor_names:
            factor = self.get_factor(name, strategy_id, strategy_data)
            if factor is not None:
                results[name] = factor
        return results


# =============================================================================
# Numba 加速函数
# =============================================================================

if NUMBA_AVAILABLE:
    @njit(cache=True, parallel=True)
    def _calc_ma_numba(close: np.ndarray, window: int) -> np.ndarray:
        T, N = close.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        for j in prange(N):
            for t in range(window - 1, T):
                s = 0.0
                count = 0
                for i in range(window):
                    val = close[t - i, j]
                    if not np.isnan(val):
                        s += val
                        count += 1
                if count > 0:
                    result[t, j] = s / count
        
        return result
    
    @njit(cache=True, parallel=True)
    def _calc_rsi_numba(close: np.ndarray, period: int) -> np.ndarray:
        T, N = close.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        for j in prange(N):
            gains = 0.0
            losses = 0.0
            
            for t in range(1, T):
                change = close[t, j] - close[t-1, j]
                
                if t <= period:
                    if change > 0:
                        gains += change
                    else:
                        losses -= change
                    
                    if t == period:
                        avg_gain = gains / period
                        avg_loss = losses / period
                        if avg_loss > 0:
                            result[t, j] = 100 - 100 / (1 + avg_gain / avg_loss)
                        else:
                            result[t, j] = 100.0
                else:
                    if change > 0:
                        avg_gain = (avg_gain * (period - 1) + change) / period
                        avg_loss = avg_gain * (period - 1) / period
                    else:
                        avg_gain = avg_gain * (period - 1) / period
                        avg_loss = (avg_loss * (period - 1) - change) / period
                    
                    if avg_loss > 0:
                        result[t, j] = 100 - 100 / (1 + avg_gain / avg_loss)
                    else:
                        result[t, j] = 100.0
        
        return result
    
    @njit(cache=True, parallel=True)
    def _calc_kdj_numba(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int, m1: int, m2: int, factor_name: str) -> np.ndarray:
        T, N = close.shape
        k = np.full((T, N), 50.0, dtype=np.float32)
        d = np.full((T, N), 50.0, dtype=np.float32)
        j = np.full((T, N), 50.0, dtype=np.float32)
        
        for col in prange(N):
            for t in range(n - 1, T):
                llv = low[t, col]
                hhv = high[t, col]
                
                for i in range(n):
                    if low[t - i, col] < llv:
                        llv = low[t - i, col]
                    if high[t - i, col] > hhv:
                        hhv = high[t - i, col]
                
                if hhv > llv:
                    rsv = (close[t, col] - llv) / (hhv - llv) * 100
                else:
                    rsv = 50.0
                
                k[t, col] = k[t-1, col] * 2/3 + rsv * 1/3 if t > 0 else rsv
                d[t, col] = d[t-1, col] * 2/3 + k[t, col] * 1/3 if t > 0 else k[t, col]
                j[t, col] = 3 * k[t, col] - 2 * d[t, col]
        
        if 'k' in factor_name.lower():
            return k
        elif 'd' in factor_name.lower():
            return d
        else:
            return j
    
    @njit(cache=True, parallel=True)
    def _calc_macd_numba(close: np.ndarray, fast: int, slow: int, signal: int, factor_name: str) -> np.ndarray:
        T, N = close.shape
        
        alpha_fast = 2.0 / (fast + 1)
        alpha_slow = 2.0 / (slow + 1)
        alpha_signal = 2.0 / (signal + 1)
        
        dif = np.zeros((T, N), dtype=np.float32)
        dea = np.zeros((T, N), dtype=np.float32)
        hist = np.zeros((T, N), dtype=np.float32)
        
        for col in prange(N):
            ema_f = close[0, col]
            ema_s = close[0, col]
            
            for t in range(1, T):
                ema_f = alpha_fast * close[t, col] + (1 - alpha_fast) * ema_f
                ema_s = alpha_slow * close[t, col] + (1 - alpha_slow) * ema_s
                dif[t, col] = ema_f - ema_s
            
            ema_d = dif[slow, col]
            for t in range(slow, T):
                ema_d = alpha_signal * dif[t, col] + (1 - alpha_signal) * ema_d
                dea[t, col] = ema_d
                hist[t, col] = (dif[t, col] - dea[t, col]) * 2
        
        if 'dif' in factor_name.lower():
            return dif
        elif 'dea' in factor_name.lower():
            return dea
        else:
            return hist
    
    @njit(cache=True, parallel=True)
    def _calc_atr_numba(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
        T, N = close.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        for col in prange(N):
            tr_sum = 0.0
            
            for t in range(T):
                if t == 0:
                    tr = high[t, col] - low[t, col]
                else:
                    tr = max(high[t, col] - low[t, col],
                            max(abs(high[t, col] - close[t-1, col]),
                                abs(low[t, col] - close[t-1, col])))
                
                if t < period:
                    tr_sum += tr
                elif t == period:
                    tr_sum += tr
                    result[t, col] = tr_sum / period
                else:
                    result[t, col] = (result[t-1, col] * (period - 1) + tr) / period
        
        return result
    
    @njit(cache=True, parallel=True)
    def _calc_boll_numba(close: np.ndarray, period: int, std_dev: float, factor_name: str) -> np.ndarray:
        T, N = close.shape
        mid = np.full((T, N), np.nan, dtype=np.float32)
        upper = np.full((T, N), np.nan, dtype=np.float32)
        lower = np.full((T, N), np.nan, dtype=np.float32)
        
        for col in prange(N):
            for t in range(period - 1, T):
                s = 0.0
                ss = 0.0
                count = 0
                
                for i in range(period):
                    val = close[t - i, col]
                    if not np.isnan(val):
                        s += val
                        count += 1
                
                if count > 0:
                    mean = s / count
                    mid[t, col] = mean
                    
                    for i in range(period):
                        val = close[t - i, col]
                        if not np.isnan(val):
                            ss += (val - mean) ** 2
                    
                    std = np.sqrt(ss / count)
                    upper[t, col] = mean + std_dev * std
                    lower[t, col] = mean - std_dev * std
        
        if 'upper' in factor_name.lower():
            return upper
        elif 'lower' in factor_name.lower():
            return lower
        else:
            return mid


# 单例获取函数
def get_smart_factor_loader() -> SmartFactorLoader:
    """获取智能因子加载器单例"""
    return SmartFactorLoader()
