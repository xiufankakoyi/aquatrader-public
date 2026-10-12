"""
因子计算模块 - 自定义因子的向量化实现

仅包含数据库中没有的因子，使用 Numba JIT 加速
"""

import numpy as np
from typing import Optional
try:
    from numba import njit
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    # 降级为普通装饰器
    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if not args else decorator(args[0])


class FactorCompute:
    """
    自定义因子计算（数据库没有的）
    
    所有方法都是静态方法，接收策略实例作为第一个参数
    """
    
    @staticmethod
    def calc_gain_3d(strategy_instance, window: int = 3) -> np.ndarray:
        """
        计算 N 日涨幅
        
        参数：
            strategy_instance: 策略实例（需要有 close 属性）
            window: 回溯天数，默认 3
        
        返回：
            (T, N) 涨幅矩阵，单位：%
        """
        if not hasattr(strategy_instance, 'close') or strategy_instance.close is None:
            raise ValueError("策略实例缺少 'close' 属性，请先调用 prepare_data()")
        
        close_matrix = strategy_instance.close
        return _calc_gain_njit(close_matrix, window)
    
    @staticmethod
    def calc_gain_5d(strategy_instance, window: int = 5) -> np.ndarray:
        """5日涨幅（快捷方式）"""
        return FactorCompute.calc_gain_3d(strategy_instance, window=window)
    
    @staticmethod
    def calc_gain_10d(strategy_instance, window: int = 10) -> np.ndarray:
        """10日涨幅（快捷方式）"""
        return FactorCompute.calc_gain_3d(strategy_instance, window=window)
    
    @staticmethod
    def calc_volatility(strategy_instance, window: int = 20) -> np.ndarray:
        """
        滚动波动率（收益率标准差）
        
        参数：
            window: 滚动窗口，默认 20 天
        
        返回：
            (T, N) 波动率矩阵
        """
        if not hasattr(strategy_instance, 'close') or strategy_instance.close is None:
            raise ValueError("策略实例缺少 'close' 属性")
        
        close_matrix = strategy_instance.close
        return _calc_volatility_njit(close_matrix, window)
    
    @staticmethod
    def calc_sharpe(strategy_instance, window: int = 20) -> np.ndarray:
        """
        滚动夏普率（简化版：收益率均值 / 收益率标准差）
        
        参数：
            window: 滚动窗口，默认 20 天
        
        返回：
            (T, N) 夏普率矩阵
        """
        if not hasattr(strategy_instance, 'close') or strategy_instance.close is None:
            raise ValueError("策略实例缺少 'close' 属性")
        
        close_matrix = strategy_instance.close
        return _calc_sharpe_njit(close_matrix, window)
    
    @staticmethod
    def calc_turnover_ma(strategy_instance, window: int = 5) -> np.ndarray:
        """
        换手率移动平均
        
        参数：
            window: 滚动窗口，默认 5 天
        
        返回：
            (T, N) 换手率均值矩阵
        """
        if not hasattr(strategy_instance, 'turnover_rate') or strategy_instance.turnover_rate is None:
            raise ValueError("策略实例缺少 'turnover_rate' 属性")
        
        turnover_matrix = strategy_instance.turnover_rate
        return _calc_ma_njit(turnover_matrix, window)
    
    @staticmethod
    def calc_amount_ratio(strategy_instance, window: int = 5) -> np.ndarray:
        """
        成交额比率：当前成交额 / N日平均成交额
        
        参数：
            window: 均值窗口，默认 5 天
        
        返回：
            (T, N) 成交额比率矩阵
        """
        if not hasattr(strategy_instance, 'amount') or strategy_instance.amount is None:
            raise ValueError("策略实例缺少 'amount' 属性")
        
        amount_matrix = strategy_instance.amount
        return _calc_ratio_to_ma_njit(amount_matrix, window)
    
    @staticmethod
    def calc_rank_pct(strategy_instance, factor_matrix: np.ndarray, axis: int = 1) -> np.ndarray:
        """
        计算因子的横截面排名百分位
        
        参数：
            factor_matrix: 输入因子矩阵 (T, N)
            axis: 排名方向，1=每日横截面，0=每只股票时间序列
        
        返回：
            (T, N) 排名百分位矩阵，范围 [0, 100]
        """
        return _calc_rank_pct_njit(factor_matrix, axis)
    
    @staticmethod
    def calc_bias(close_array: np.ndarray, ma_period: int = 20) -> np.ndarray:
        """
        计算乖离率：(收盘价 / N日均线 - 1) * 100
        
        参数：
            close_array: 收盘价数组 (T,) 或 (T, N)
            ma_period: 均线周期，默认 20
        
        返回：
            乖离率数组，单位：%
        """
        return _calc_bias_njit(np.asarray(close_array, dtype=np.float32), ma_period)
    
    @staticmethod
    def calc_macd(close_array: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
        """
        计算 MACD 指标
        
        参数：
            close_array: 收盘价数组 (T,) 或 (T, N)
            fast: 快线周期，默认 12
            slow: 慢线周期，默认 26
            signal: 信号线周期，默认 9
        
        返回：
            dict: {
                'dif': DIF 线,
                'dea': DEA 信号线,
                'macd': MACD 柱状图,
                'golden_cross': 金叉信号 (布尔),
                'death_cross': 死叉信号 (布尔)
            }
        """
        dif, dea, macd_hist, golden_cross, death_cross = _calc_macd_njit(
            np.asarray(close_array, dtype=np.float32), fast, slow, signal
        )
        return {
            "dif": dif,
            "dea": dea,
            "macd": macd_hist,
            "golden_cross": golden_cross,
            "death_cross": death_cross,
        }
    
    @staticmethod
    def calc_rsi(close_array: np.ndarray, period: int = 14) -> np.ndarray:
        """
        计算 RSI 相对强弱指标
        
        参数：
            close_array: 收盘价数组 (T,) 或 (T, N)
            period: RSI 周期，默认 14
        
        返回：
            RSI 值数组，范围 [0, 100]
        """
        return _calc_rsi_njit(np.asarray(close_array, dtype=np.float32), period)
    
    @staticmethod
    def calc_ma_breakout(close_array: np.ndarray, ma_period: int = 20) -> np.ndarray:
        """
        判断是否站上均线
        
        参数：
            close_array: 收盘价数组 (T,) 或 (T, N)
            ma_period: 均线周期，默认 20
        
        返回：
            布尔数组：True 表示站上均线
        """
        return _calc_ma_breakout_njit(np.asarray(close_array, dtype=np.float32), ma_period)
    
    @staticmethod
    def calc_valuation_percentile(value_array: np.ndarray, window: int = 252) -> np.ndarray:
        """
        计算估值分位（历史百分位）
        
        参数：
            value_array: 估值数据数组 (T,) 或 (T, N)，如 PE、PB
            window: 回溯窗口，默认 252（约一年）
        
        返回：
            分位值数组，范围 [0, 100]
        """
        return _calc_valuation_percentile_njit(np.asarray(value_array, dtype=np.float32), window)


# =============================================================================
# Numba 加速的核心计算函数
# =============================================================================

@njit
def _calc_gain_njit(close_matrix: np.ndarray, window: int) -> np.ndarray:
    """N日涨幅计算（Numba加速）"""
    T, N = close_matrix.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    for t in range(window, T):
        for n in range(N):
            close_now = close_matrix[t, n]
            close_prev = close_matrix[t - window, n]
            
            if not np.isnan(close_now) and not np.isnan(close_prev) and close_prev > 0:
                result[t, n] = (close_now / close_prev - 1.0) * 100.0
    
    return result


@njit
def _calc_volatility_njit(close_matrix: np.ndarray, window: int) -> np.ndarray:
    """滚动波动率计算（Numba加速）"""
    T, N = close_matrix.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    for t in range(window, T):
        for n in range(N):
            # 计算过去 window 天的收益率
            returns = np.zeros(window - 1, dtype=np.float32)
            valid_count = 0
            
            for i in range(window - 1):
                close_curr = close_matrix[t - i, n]
                close_prev = close_matrix[t - i - 1, n]
                
                if not np.isnan(close_curr) and not np.isnan(close_prev) and close_prev > 0:
                    returns[valid_count] = (close_curr / close_prev - 1.0)
                    valid_count += 1
            
            if valid_count >= window // 2:  # 至少有一半有效数据
                result[t, n] = np.std(returns[:valid_count])
    
    return result


@njit
def _calc_sharpe_njit(close_matrix: np.ndarray, window: int) -> np.ndarray:
    """滚动夏普率计算（Numba加速）"""
    T, N = close_matrix.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    for t in range(window, T):
        for n in range(N):
            # 计算过去 window 天的收益率
            returns = np.zeros(window - 1, dtype=np.float32)
            valid_count = 0
            
            for i in range(window - 1):
                close_curr = close_matrix[t - i, n]
                close_prev = close_matrix[t - i - 1, n]
                
                if not np.isnan(close_curr) and not np.isnan(close_prev) and close_prev > 0:
                    returns[valid_count] = (close_curr / close_prev - 1.0)
                    valid_count += 1
            
            if valid_count >= window // 2:
                mean_return = np.mean(returns[:valid_count])
                std_return = np.std(returns[:valid_count])
                
                if std_return > 1e-8:
                    # 年化夏普 = 日均收益 / 日波动率 * sqrt(252)
                    result[t, n] = mean_return / std_return * np.sqrt(252.0)
    
    return result


@njit
def _calc_ma_njit(data_matrix: np.ndarray, window: int) -> np.ndarray:
    """移动平均计算（Numba加速）"""
    T, N = data_matrix.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    for t in range(window - 1, T):
        for n in range(N):
            sum_val = 0.0
            count = 0
            
            for i in range(window):
                val = data_matrix[t - i, n]
                if not np.isnan(val):
                    sum_val += val
                    count += 1
            
            if count > 0:
                result[t, n] = sum_val / count
    
    return result


@njit
def _calc_ratio_to_ma_njit(data_matrix: np.ndarray, window: int) -> np.ndarray:
    """数值 / 均值 比率（Numba加速）"""
    T, N = data_matrix.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    ma = _calc_ma_njit(data_matrix, window)
    
    for t in range(T):
        for n in range(N):
            current = data_matrix[t, n]
            average = ma[t, n]
            
            if not np.isnan(current) and not np.isnan(average) and average > 0:
                result[t, n] = current / average
    
    return result


@njit
def _calc_rank_pct_njit(data_matrix: np.ndarray, axis: int) -> np.ndarray:
    """排名百分位计算（Numba加速）"""
    T, N = data_matrix.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    if axis == 1:  # 横截面排名（每日）
        for t in range(T):
            # 提取当日所有股票的因子值
            values = data_matrix[t, :]
            valid_mask = ~np.isnan(values)
            valid_values = values[valid_mask]
            
            if len(valid_values) > 0:
                # 计算排名百分位
                for n in range(N):
                    if valid_mask[n]:
                        val = values[n]
                        rank = np.sum(valid_values < val)
                        result[t, n] = (rank / len(valid_values)) * 100.0
    
    else:  # 时间序列排名（每只股票）
        for n in range(N):
            values = data_matrix[:, n]
            valid_mask = ~np.isnan(values)
            valid_values = values[valid_mask]
            
            if len(valid_values) > 0:
                for t in range(T):
                    if valid_mask[t]:
                        val = values[t]
                        rank = np.sum(valid_values < val)
                        result[t, n] = (rank / len(valid_values)) * 100.0
    
    return result


@njit
def _calc_bias_njit(close_array: np.ndarray, ma_period: int) -> np.ndarray:
    """乖离率计算（Numba加速）"""
    if close_array.ndim == 1:
        T = len(close_array)
        result = np.full(T, np.nan, dtype=np.float32)
        
        for t in range(ma_period - 1, T):
            sum_val = 0.0
            count = 0
            for i in range(ma_period):
                val = close_array[t - i]
                if not np.isnan(val):
                    sum_val += val
                    count += 1
            
            if count > 0 and close_array[t] > 0:
                ma = sum_val / count
                result[t] = (close_array[t] / ma - 1.0) * 100.0
    else:
        T, N = close_array.shape
        result = np.full((T, N), np.nan, dtype=np.float32)
        
        for t in range(ma_period - 1, T):
            for n in range(N):
                sum_val = 0.0
                count = 0
                for i in range(ma_period):
                    val = close_array[t - i, n]
                    if not np.isnan(val):
                        sum_val += val
                        count += 1
                
                if count > 0 and close_array[t, n] > 0:
                    ma = sum_val / count
                    result[t, n] = (close_array[t, n] / ma - 1.0) * 100.0
    
    return result


@njit
def _calc_macd_njit(
    close_array: np.ndarray, fast: int, slow: int, signal: int
) -> tuple:
    """MACD 计算（Numba加速）"""
    if close_array.ndim == 1:
        T = len(close_array)
        close_2d = close_array.reshape(-1, 1)
    else:
        T, N = close_array.shape
        close_2d = close_array
    
    _, N = close_2d.shape
    
    dif = np.full((T, N), np.nan, dtype=np.float32)
    dea = np.full((T, N), np.nan, dtype=np.float32)
    macd_hist = np.full((T, N), np.nan, dtype=np.float32)
    golden_cross = np.zeros((T, N), dtype=np.bool_)
    death_cross = np.zeros((T, N), dtype=np.bool_)
    
    alpha_fast = 2.0 / (fast + 1)
    alpha_slow = 2.0 / (slow + 1)
    alpha_signal = 2.0 / (signal + 1)
    
    for n in range(N):
        ema_fast = close_2d[0, n] if not np.isnan(close_2d[0, n]) else 0.0
        ema_slow = ema_fast
        ema_signal = 0.0
        has_signal = False
        
        for t in range(1, T):
            if np.isnan(close_2d[t, n]):
                continue
            
            ema_fast = alpha_fast * close_2d[t, n] + (1 - alpha_fast) * ema_fast
            ema_slow = alpha_slow * close_2d[t, n] + (1 - alpha_slow) * ema_slow
            dif[t, n] = ema_fast - ema_slow
            
            if has_signal:
                prev_dea = dea[t - 1, n] if not np.isnan(dea[t - 1, n]) else 0.0
                dea[t, n] = alpha_signal * dif[t, n] + (1 - alpha_signal) * prev_dea
                macd_hist[t, n] = 2 * (dif[t, n] - dea[t, n])
                
                if t > 1 and not np.isnan(dif[t - 1, n]) and not np.isnan(dea[t - 1, n]):
                    if dif[t - 1, n] <= dea[t - 1, n] and dif[t, n] > dea[t, n]:
                        golden_cross[t, n] = True
                    elif dif[t - 1, n] >= dea[t - 1, n] and dif[t, n] < dea[t, n]:
                        death_cross[t, n] = True
            else:
                if t >= slow:
                    dea[t, n] = dif[t, n]
                    has_signal = True
    
    return dif, dea, macd_hist, golden_cross, death_cross


@njit
def _calc_rsi_njit(close_array: np.ndarray, period: int) -> np.ndarray:
    """RSI 计算（Numba加速）"""
    if close_array.ndim == 1:
        T = len(close_array)
        close_2d = close_array.reshape(-1, 1)
    else:
        T, N = close_array.shape
        close_2d = close_array
    
    _, N = close_2d.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    for n in range(N):
        gains = np.zeros(period, dtype=np.float32)
        losses = np.zeros(period, dtype=np.float32)
        avg_gain = 0.0
        avg_loss = 0.0
        valid_count = 0
        
        for t in range(1, T):
            if np.isnan(close_2d[t, n]) or np.isnan(close_2d[t - 1, n]):
                continue
            
            change = close_2d[t, n] - close_2d[t - 1, n]
            
            if valid_count < period:
                idx = valid_count % period
                if change > 0:
                    gains[idx] = change
                    losses[idx] = 0.0
                else:
                    gains[idx] = 0.0
                    losses[idx] = -change
                
                valid_count += 1
                
                if valid_count == period:
                    avg_gain = np.mean(gains)
                    avg_loss = np.mean(losses)
            else:
                idx = t % period
                if change > 0:
                    avg_gain = (avg_gain * (period - 1) + change) / period
                    avg_loss = (avg_loss * (period - 1)) / period
                else:
                    avg_gain = (avg_gain * (period - 1)) / period
                    avg_loss = (avg_loss * (period - 1) + (-change)) / period
            
            if valid_count >= period:
                if avg_loss > 1e-8:
                    rs = avg_gain / avg_loss
                    result[t, n] = 100.0 - (100.0 / (1.0 + rs))
                else:
                    result[t, n] = 100.0
    
    return result


@njit
def _calc_ma_breakout_njit(close_array: np.ndarray, ma_period: int) -> np.ndarray:
    """均线突破判断（Numba加速）"""
    if close_array.ndim == 1:
        T = len(close_array)
        close_2d = close_array.reshape(-1, 1)
    else:
        T, N = close_array.shape
        close_2d = close_array
    
    _, N = close_2d.shape
    result = np.zeros((T, N), dtype=np.bool_)
    
    for n in range(N):
        for t in range(ma_period - 1, T):
            sum_val = 0.0
            count = 0
            for i in range(ma_period):
                val = close_2d[t - i, n]
                if not np.isnan(val):
                    sum_val += val
                    count += 1
            
            if count > 0 and not np.isnan(close_2d[t, n]):
                ma = sum_val / count
                result[t, n] = close_2d[t, n] > ma
    
    return result


@njit
def _calc_valuation_percentile_njit(value_array: np.ndarray, window: int) -> np.ndarray:
    """估值分位计算（Numba加速）"""
    if value_array.ndim == 1:
        T = len(value_array)
        value_2d = value_array.reshape(-1, 1)
    else:
        T, N = value_array.shape
        value_2d = value_array
    
    _, N = value_2d.shape
    result = np.full((T, N), np.nan, dtype=np.float32)
    
    for n in range(N):
        for t in range(window - 1, T):
            values = np.zeros(window, dtype=np.float32)
            count = 0
            
            for i in range(window):
                val = value_2d[t - i, n]
                if not np.isnan(val) and val > 0:
                    values[count] = val
                    count += 1
            
            if count > 0 and not np.isnan(value_2d[t, n]) and value_2d[t, n] > 0:
                current = value_2d[t, n]
                below = 0
                for i in range(count):
                    if values[i] < current:
                        below += 1
                result[t, n] = (below / count) * 100.0
    
    return result
