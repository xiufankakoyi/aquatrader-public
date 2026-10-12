"""
内置指标函数注册表

设计目标：
1. 注册机制：通过装饰器轻松注册新指标
2. 统一接口：所有指标函数具有相同的签名
3. 向量化计算：利用 Pandas/NumPy 进行高效计算
4. 自动文档：从函数签名生成指标文档

使用示例：
    # 注册自定义指标
    @register_indicator("my_indicator")
    def calculate_my_indicator(data: pd.DataFrame, column: str, window: int) -> pd.Series:
        return data.groupby('stock_code')[column].transform(lambda x: x.rolling(window).mean())
    
    # 使用指标
    from core.quant.strategies.configurable import get_indicator
    ma_func = get_indicator("ma")
    result = ma_func(data, column="close", window=20)
"""

import pandas as pd
import numpy as np
from typing import Dict, Callable, List, Any, Optional
from functools import wraps


class IndicatorRegistry:
    """
    指标注册表 - 单例模式
    
    管理所有可用的指标函数，提供注册、查询、执行功能
    """
    
    _instance = None
    _indicators: Dict[str, Callable] = {}
    _metadata: Dict[str, Dict[str, Any]] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def register(
        self,
        name: str,
        func: Callable,
        description: Optional[str] = None,
        params_schema: Optional[Dict[str, Any]] = None
    ) -> Callable:
        """
        注册指标函数
        
        参数：
            name: 指标名称（唯一标识）
            func: 指标计算函数
            description: 指标描述
            params_schema: 参数schema，用于前端表单生成
        """
        if name in self._indicators:
            import warnings
            warnings.warn(f"指标 '{name}' 已存在，将被覆盖", UserWarning)
        
        self._indicators[name] = func
        self._metadata[name] = {
            "description": description or func.__doc__ or "",
            "params_schema": params_schema or {},
            "signature": str(func.__code__.co_varnames[:func.__code__.co_argcount]),
        }
        
        return func
    
    def get(self, name: str) -> Callable:
        """获取指标函数"""
        if name not in self._indicators:
            raise KeyError(f"未找到指标 '{name}'，可用指标: {list(self._indicators.keys())}")
        return self._indicators[name]
    
    def list_indicators(self) -> List[str]:
        """列出所有可用指标"""
        return list(self._indicators.keys())
    
    def get_metadata(self, name: str) -> Dict[str, Any]:
        """获取指标元数据"""
        if name not in self._metadata:
            raise KeyError(f"未找到指标 '{name}' 的元数据")
        return self._metadata[name]
    
    def execute(
        self,
        name: str,
        data: pd.DataFrame,
        **kwargs
    ) -> pd.Series:
        """
        执行指标计算
        
        参数：
            name: 指标名称
            data: 数据DataFrame
            **kwargs: 指标参数
        
        返回：
            pd.Series: 计算结果
        """
        func = self.get(name)
        return func(data, **kwargs)
    
    def clear(self):
        """清空所有注册指标（主要用于测试）"""
        self._indicators.clear()
        self._metadata.clear()


# 全局注册表实例
_registry = IndicatorRegistry()


# 便捷函数
def register_indicator(
    name: str,
    description: Optional[str] = None,
    params_schema: Optional[Dict[str, Any]] = None
) -> Callable:
    """
    指标注册装饰器
    
    使用示例：
        @register_indicator("ma", description="移动平均线")
        def calculate_ma(data: pd.DataFrame, column: str, window: int) -> pd.Series:
            ...
    """
    def decorator(func: Callable) -> Callable:
        _registry.register(name, func, description, params_schema)
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        
        # 附加元数据到函数
        wrapper._indicator_name = name
        wrapper._indicator_description = description
        
        return wrapper
    return decorator


def get_indicator(name: str) -> Callable:
    """获取指标函数"""
    return _registry.get(name)


def list_indicators() -> List[str]:
    """列出所有可用指标"""
    return _registry.list_indicators()


def get_indicator_metadata(name: str) -> Dict[str, Any]:
    """获取指标元数据"""
    return _registry.get_metadata(name)


def execute_indicator(name: str, data: pd.DataFrame, **kwargs) -> pd.Series:
    """执行指标计算"""
    return _registry.execute(name, data, **kwargs)


# ==================== 内置指标实现 ====================

@register_indicator(
    "ma",
    description="移动平均线 (Moving Average)",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "window": {"type": "integer", "default": 20, "min": 1, "max": 252, "description": "窗口大小"},
    }
)
def calculate_ma(data: pd.DataFrame, column: str = "close", window: int = 20) -> pd.Series:
    """
    计算移动平均线
    
    参数：
        data: DataFrame，必须包含 'stock_code' 和指定的 column 列
        column: 要计算的列名
        window: 移动平均窗口大小
    
    返回：
        pd.Series: 移动平均线值，与 data 的 index 对齐
    """
    return data.groupby('stock_code')[column].transform(
        lambda x: x.rolling(window=window, min_periods=1).mean()
    )


@register_indicator(
    "ema",
    description="指数移动平均线 (Exponential Moving Average)",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "window": {"type": "integer", "default": 12, "min": 1, "max": 252, "description": "窗口大小"},
    }
)
def calculate_ema(data: pd.DataFrame, column: str = "close", window: int = 12) -> pd.Series:
    """计算指数移动平均线"""
    return data.groupby('stock_code')[column].transform(
        lambda x: x.ewm(span=window, adjust=False).mean()
    )


@register_indicator(
    "rsi",
    description="相对强弱指标 (Relative Strength Index)",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "window": {"type": "integer", "default": 14, "min": 2, "max": 60, "description": "RSI周期"},
    }
)
def calculate_rsi(data: pd.DataFrame, column: str = "close", window: int = 14) -> pd.Series:
    """
    计算RSI指标
    
    RSI = 100 - (100 / (1 + RS))
    RS = 平均上涨幅度 / 平均下跌幅度
    """
    def _rsi(series: pd.Series) -> pd.Series:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)  # 初始值设为50（中性）
    
    return data.groupby('stock_code')[column].transform(_rsi)


@register_indicator(
    "macd",
    description="MACD指标",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "fast": {"type": "integer", "default": 12, "min": 2, "max": 60, "description": "快线周期"},
        "slow": {"type": "integer", "default": 26, "min": 5, "max": 120, "description": "慢线周期"},
        "signal": {"type": "integer", "default": 9, "min": 2, "max": 30, "description": "信号线周期"},
    }
)
def calculate_macd(
    data: pd.DataFrame,
    column: str = "close",
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> pd.DataFrame:
    """
    计算MACD指标
    
    返回DataFrame包含三列：macd, signal, histogram
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
    
    results = []
    for name, group in data.groupby('stock_code'):
        result = _macd(group[column])
        result.index = group.index
        results.append(result)
    
    return pd.concat(results).sort_index()


@register_indicator(
    "bollinger",
    description="布林带 (Bollinger Bands)",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "window": {"type": "integer", "default": 20, "min": 5, "max": 60, "description": "窗口大小"},
        "num_std": {"type": "float", "default": 2.0, "min": 0.5, "max": 5.0, "description": "标准差倍数"},
    }
)
def calculate_bollinger(
    data: pd.DataFrame,
    column: str = "close",
    window: int = 20,
    num_std: float = 2.0
) -> pd.DataFrame:
    """
    计算布林带
    
    返回DataFrame包含三列：upper, middle, lower
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
    
    results = []
    for name, group in data.groupby('stock_code'):
        result = _bollinger(group[column])
        result.index = group.index
        results.append(result)
    
    return pd.concat(results).sort_index()


@register_indicator(
    "atr",
    description="平均真实波幅 (Average True Range)",
    params_schema={
        "window": {"type": "integer", "default": 14, "min": 2, "max": 60, "description": "ATR周期"},
    }
)
def calculate_atr(data: pd.DataFrame, window: int = 14) -> pd.Series:
    """
    计算ATR指标
    
    需要数据包含 'high', 'low', 'close' 列
    """
    def _atr(group: pd.DataFrame) -> pd.Series:
        high_low = group['high'] - group['low']
        high_close = np.abs(group['high'] - group['close'].shift())
        low_close = np.abs(group['low'] - group['close'].shift())
        
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=window).mean()
        
        return atr
    
    return data.groupby('stock_code').apply(_atr).reset_index(level=0, drop=True)


@register_indicator(
    "volume_ma",
    description="成交量移动平均线",
    params_schema={
        "window": {"type": "integer", "default": 20, "min": 1, "max": 252, "description": "窗口大小"},
    }
)
def calculate_volume_ma(data: pd.DataFrame, window: int = 20) -> pd.Series:
    """计算成交量移动平均线"""
    return data.groupby('stock_code')['volume'].transform(
        lambda x: x.rolling(window=window, min_periods=1).mean()
    )


@register_indicator(
    "crossover",
    description="金叉检测 (快线从下向上穿越慢线)",
    params_schema={
        "fast": {"type": "string", "description": "快线列名"},
        "slow": {"type": "string", "description": "慢线列名"},
    }
)
def calculate_crossover(data: pd.DataFrame, fast: str, slow: str) -> pd.Series:
    """
    检测金叉信号
    
    返回布尔Series，True表示当日发生金叉
    """
    def _crossover(group: pd.DataFrame) -> pd.Series:
        fast_line = group[fast]
        slow_line = group[slow]
        
        # 当日快线在慢线上方
        above_today = fast_line > slow_line
        # 前一日快线在慢线下方
        above_yesterday = fast_line.shift(1) <= slow_line.shift(1)
        
        return above_today & above_yesterday
    
    return data.groupby('stock_code').apply(_crossover).reset_index(level=0, drop=True)


@register_indicator(
    "crossunder",
    description="死叉检测 (快线从上向下穿越慢线)",
    params_schema={
        "fast": {"type": "string", "description": "快线列名"},
        "slow": {"type": "string", "description": "慢线列名"},
    }
)
def calculate_crossunder(data: pd.DataFrame, fast: str, slow: str) -> pd.Series:
    """检测死叉信号"""
    def _crossunder(group: pd.DataFrame) -> pd.Series:
        fast_line = group[fast]
        slow_line = group[slow]
        
        below_today = fast_line < slow_line
        below_yesterday = fast_line.shift(1) >= slow_line.shift(1)
        
        return below_today & below_yesterday
    
    return data.groupby('stock_code').apply(_crossunder).reset_index(level=0, drop=True)


@register_indicator(
    "above",
    description="价格上穿检测",
    params_schema={
        "column": {"type": "string", "description": "价格列名"},
        "reference": {"type": "string", "description": "参考线列名"},
    }
)
def calculate_above(data: pd.DataFrame, column: str, reference: str) -> pd.Series:
    """检测价格是否在参考线上方"""
    return data[column] > data[reference]


@register_indicator(
    "below",
    description="价格下穿检测",
    params_schema={
        "column": {"type": "string", "description": "价格列名"},
        "reference": {"type": "string", "description": "参考线列名"},
    }
)
def calculate_below(data: pd.DataFrame, column: str, reference: str) -> pd.Series:
    """检测价格是否在参考线下方"""
    return data[column] < data[reference]


@register_indicator(
    "slope",
    description="斜率/趋势检测",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "window": {"type": "integer", "default": 5, "min": 2, "max": 60, "description": "窗口大小"},
    }
)
def calculate_slope(data: pd.DataFrame, column: str = "close", window: int = 5) -> pd.Series:
    """
    计算价格斜率（线性回归斜率）
    
    正值表示上升趋势，负值表示下降趋势
    """
    def _slope(series: pd.Series) -> pd.Series:
        x = np.arange(len(series))
        
        def _linear_regression(y):
            if len(y) < 2:
                return 0
            A = np.vstack([x[-len(y):], np.ones(len(y))]).T
            m, _ = np.linalg.lstsq(A, y, rcond=None)[0]
            return m
        
        return series.rolling(window=window).apply(_linear_regression, raw=True)
    
    return data.groupby('stock_code')[column].transform(_slope)


@register_indicator(
    "percentile",
    description="历史分位数",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "window": {"type": "integer", "default": 60, "min": 5, "max": 252, "description": "窗口大小"},
    }
)
def calculate_percentile(data: pd.DataFrame, column: str = "close", window: int = 60) -> pd.Series:
    """
    计算当前价格在历史窗口中的分位数位置
    
    返回0-1之间的值，表示当前价格在历史分布中的位置
    """
    def _percentile(series: pd.Series) -> pd.Series:
        def _rank(x):
            if len(x) < 2:
                return 0.5
            return (x[-1] - x.min()) / (x.max() - x.min()) if x.max() != x.min() else 0.5
        
        return series.rolling(window=window).apply(_rank, raw=True)
    
    return data.groupby('stock_code')[column].transform(_percentile)


@register_indicator(
    "zscore",
    description="Z-Score标准化",
    params_schema={
        "column": {"type": "string", "default": "close", "description": "计算列名"},
        "window": {"type": "integer", "default": 20, "min": 5, "max": 252, "description": "窗口大小"},
    }
)
def calculate_zscore(data: pd.DataFrame, column: str = "close", window: int = 20) -> pd.Series:
    """
    计算Z-Score
    
    Z = (X - μ) / σ
    """
    def _zscore(series: pd.Series) -> pd.Series:
        mean = series.rolling(window=window).mean()
        std = series.rolling(window=window).std()
        return (series - mean) / std
    
    return data.groupby('stock_code')[column].transform(_zscore)


# ==================== 批量计算工具 ====================

def calculate_indicators(
    data: pd.DataFrame,
    indicator_configs: List[Dict[str, Any]]
) -> pd.DataFrame:
    """
    批量计算多个指标
    
    参数：
        data: 原始数据DataFrame
        indicator_configs: 指标配置列表
            [
                {"name": "ma5", "type": "ma", "params": {"column": "close", "window": 5}},
                {"name": "ma20", "type": "ma", "params": {"column": "close", "window": 20}},
            ]
    
    返回：
        pd.DataFrame: 添加了指标列的DataFrame
    """
    result = data.copy()
    
    for config in indicator_configs:
        indicator_name = config.get("name")
        indicator_type = config.get("type")
        params = config.get("params", {})
        
        try:
            indicator_func = get_indicator(indicator_type)
            indicator_result = indicator_func(result, **params)
            
            # 处理返回DataFrame的情况（如MACD、布林带）
            if isinstance(indicator_result, pd.DataFrame):
                for col in indicator_result.columns:
                    result[f"{indicator_name}_{col}"] = indicator_result[col]
            else:
                result[indicator_name] = indicator_result
                
        except Exception as e:
            import warnings
            warnings.warn(f"计算指标 '{indicator_name}' 失败: {e}")
    
    return result


# ==================== 初始化时注册所有内置指标 ====================

def _register_all_builtin_indicators():
    """注册所有内置指标（在模块导入时自动调用）"""
    # 指标已在定义时通过装饰器注册
    pass


# 自动注册
_register_all_builtin_indicators()
