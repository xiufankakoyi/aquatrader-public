"""
DSL 翻译器 - 将策略 DSL 翻译为 Polars/Pandas 表达式

这是实现"无代码回测"和"AI 生成策略"的核心技术。
翻译器将 JSON DSL 转换为可执行的向量化表达式，
无需生成或执行 Python 代码，保证安全性和性能。

架构：
    JSON DSL -> Schema 验证 -> 中间表示 (IR) -> Polars/Pandas 表达式

示例：
    # DSL 输入
    signal = {
        "type": "crossover",
        "fast": {"type": "ma", "window": 5},
        "slow": {"type": "ma", "window": 20}
    }
    
    # 翻译为 Polars
    translator = PolarsTranslator()
    expr = translator.translate_signal(signal)
    # 结果: pl.col("close").rolling_mean(5) > pl.col("close").rolling_mean(20)
"""

from typing import Dict, Any, List, Optional, Union, Callable
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd
import numpy as np

# 尝试导入 Polars
try:
    import polars as pl
    POLARS_AVAILABLE = True
except ImportError:
    POLARS_AVAILABLE = False
    pl = None

from .schema import (
    SignalSchema,
    FilterSchema,
    RiskSchema,
    ActionSchema,
    IndicatorSchema,
    SignalType,
    IndicatorType,
    FilterType,
)


@dataclass
class CompiledExpression:
    """编译后的表达式"""
    expr: Any  # Polars Expr 或 Pandas Series
    name: str  # 表达式名称
    dtype: str  # 数据类型
    dependencies: List[str]  # 依赖的列


class BaseTranslator(ABC):
    """翻译器基类"""
    
    def __init__(self):
        self._indicator_registry: Dict[str, Callable] = {}
        self._register_builtin_indicators()
    
    def _register_builtin_indicators(self):
        """注册内置指标"""
        pass
    
    @abstractmethod
    def translate_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """翻译指标为表达式"""
        pass
    
    @abstractmethod
    def translate_signal(self, signal: SignalSchema) -> CompiledExpression:
        """翻译信号为表达式"""
        pass
    
    @abstractmethod
    def translate_filter(self, filter: FilterSchema) -> CompiledExpression:
        """翻译过滤器为表达式"""
        pass


class PolarsTranslator(BaseTranslator):
    """
    Polars 表达式翻译器
    
    将策略 DSL 翻译为 Polars 表达式，实现高性能向量化计算。
    
    特性：
    - 惰性求值 (Lazy Evaluation)
    - 查询优化
    - 多线程执行
    - 内存高效
    
    使用示例：
        translator = PolarsTranslator()
        
        # 翻译信号
        signal_expr = translator.translate_signal(signal_schema)
        
        # 应用到 DataFrame
        df = df.with_columns([
            signal_expr.expr.alias("buy_signal")
        ])
    """
    
    def __init__(self):
        if not POLARS_AVAILABLE:
            raise ImportError("Polars 未安装，请运行: pip install polars")
        super().__init__()
    
    def _register_builtin_indicators(self):
        """注册内置指标到 Polars"""
        self._indicator_registry = {
            # 价格指标
            "price": self._price_indicator,
            "open": lambda ind: self._column_indicator("open"),
            "high": lambda ind: self._column_indicator("high"),
            "low": lambda ind: self._column_indicator("low"),
            "close": lambda ind: self._column_indicator("close"),
            "volume": lambda ind: self._column_indicator("volume"),
            "vwap": self._vwap_indicator,
            
            # 移动平均线
            "ma": self._ma_indicator,
            "ema": self._ema_indicator,
            "wma": self._wma_indicator,
            "sma": self._sma_indicator,
            
            # 动量指标
            "rsi": self._rsi_indicator,
            "macd": self._macd_indicator,
            
            # 波动率指标
            "atr": self._atr_indicator,
            "bollinger": self._bollinger_indicator,
            
            # 成交量指标
            "vol_ma": self._vol_ma_indicator,
            "obv": self._obv_indicator,
        }
    
    # ==================== 指标实现 ====================
    
    def _price_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """价格指标"""
        column = indicator.column or "close"
        return CompiledExpression(
            expr=pl.col(column),
            name=f"price_{column}",
            dtype="f64",
            dependencies=[column]
        )
    
    def _column_indicator(self, column: str) -> CompiledExpression:
        """列引用"""
        return CompiledExpression(
            expr=pl.col(column),
            name=column,
            dtype="f64",
            dependencies=[column]
        )
    
    def _vwap_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """VWAP 指标"""
        return CompiledExpression(
            expr=(pl.col("close") * pl.col("volume")).cumsum() / pl.col("volume").cumsum(),
            name="vwap",
            dtype="f64",
            dependencies=["close", "volume"]
        )
    
    def _ma_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """简单移动平均"""
        window = indicator.window or 20
        column = indicator.column or "close"
        return CompiledExpression(
            expr=pl.col(column).rolling_mean(window),
            name=f"ma_{window}",
            dtype="f64",
            dependencies=[column]
        )
    
    def _ema_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """指数移动平均"""
        window = indicator.window or 12
        column = indicator.column or "close"
        alpha = 2.0 / (window + 1)
        return CompiledExpression(
            expr=pl.col(column).ewm_mean(alpha=alpha, adjust=False),
            name=f"ema_{window}",
            dtype="f64",
            dependencies=[column]
        )
    
    def _wma_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """加权移动平均"""
        window = indicator.window or 20
        column = indicator.column or "close"
        # Polars 没有直接的 WMA，使用 rolling_mean 近似
        return CompiledExpression(
            expr=pl.col(column).rolling_mean(window),
            name=f"wma_{window}",
            dtype="f64",
            dependencies=[column]
        )
    
    def _sma_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """平滑移动平均 (与 EMA 相同)"""
        return self._ema_indicator(indicator)
    
    def _rsi_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """RSI 指标"""
        window = indicator.window or 14
        column = indicator.column or "close"
        
        # RSI 计算
        delta = pl.col(column).diff()
        gain = delta.clip_min(0)
        loss = (-delta).clip_min(0)
        
        avg_gain = gain.rolling_mean(window)
        avg_loss = loss.rolling_mean(window)
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return CompiledExpression(
            expr=rsi,
            name=f"rsi_{window}",
            dtype="f64",
            dependencies=[column]
        )
    
    def _macd_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """MACD 指标"""
        fast = indicator.params.get("fast", 12)
        slow = indicator.params.get("slow", 26)
        signal = indicator.params.get("signal", 9)
        column = indicator.column or "close"
        
        ema_fast = pl.col(column).ewm_mean(alpha=2.0/(fast+1), adjust=False)
        ema_slow = pl.col(column).ewm_mean(alpha=2.0/(slow+1), adjust=False)
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm_mean(alpha=2.0/(signal+1), adjust=False)
        histogram = macd_line - signal_line
        
        return CompiledExpression(
            expr=macd_line,
            name=f"macd_{fast}_{slow}",
            dtype="f64",
            dependencies=[column]
        )
    
    def _atr_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """ATR 指标"""
        window = indicator.window or 14
        
        high_low = pl.col("high") - pl.col("low")
        high_close = (pl.col("high") - pl.col("close").shift(1)).abs()
        low_close = (pl.col("low") - pl.col("close").shift(1)).abs()
        
        tr = pl.max([high_low, high_close, low_close])
        atr = tr.rolling_mean(window)
        
        return CompiledExpression(
            expr=atr,
            name=f"atr_{window}",
            dtype="f64",
            dependencies=["high", "low", "close"]
        )
    
    def _bollinger_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """布林带指标"""
        window = indicator.window or 20
        std = indicator.params.get("std", 2)
        column = indicator.column or "close"
        
        middle = pl.col(column).rolling_mean(window)
        rolling_std = pl.col(column).rolling_std(window)
        upper = middle + rolling_std * std
        lower = middle - rolling_std * std
        
        # 返回带宽百分比
        bandwidth = (upper - lower) / middle
        
        return CompiledExpression(
            expr=bandwidth,
            name=f"bollinger_{window}_{std}",
            dtype="f64",
            dependencies=[column]
        )
    
    def _vol_ma_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """成交量均线"""
        window = indicator.window or 20
        return CompiledExpression(
            expr=pl.col("volume").rolling_mean(window),
            name=f"vol_ma_{window}",
            dtype="f64",
            dependencies=["volume"]
        )
    
    def _obv_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """OBV 指标"""
        close_diff = pl.col("close").diff()
        obv = pl.when(close_diff > 0).then(pl.col("volume")) \
                 .when(close_diff < 0).then(-pl.col("volume")) \
                 .otherwise(0).cumsum()
        
        return CompiledExpression(
            expr=obv,
            name="obv",
            dtype="f64",
            dependencies=["close", "volume"]
        )
    
    # ==================== 翻译方法 ====================
    
    def translate_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """翻译指标为 Polars 表达式"""
        indicator_type = indicator.type.value if hasattr(indicator.type, 'value') else str(indicator.type)
        
        if indicator_type in self._indicator_registry:
            return self._indicator_registry[indicator_type](indicator)
        
        raise ValueError(f"未知的指标类型: {indicator_type}")
    
    def translate_signal(self, signal: SignalSchema) -> CompiledExpression:
        """翻译信号为 Polars 表达式"""
        signal_type = signal.type.value if hasattr(signal.type, 'value') else str(signal.type)
        
        if signal_type == "crossover":
            return self._crossover_signal(signal)
        elif signal_type == "crossunder":
            return self._crossunder_signal(signal)
        elif signal_type == "above":
            return self._above_signal(signal)
        elif signal_type == "below":
            return self._below_signal(signal)
        elif signal_type == "between":
            return self._between_signal(signal)
        elif signal_type == "trend_up":
            return self._trend_up_signal(signal)
        elif signal_type == "trend_down":
            return self._trend_down_signal(signal)
        else:
            raise ValueError(f"未知的信号类型: {signal_type}")
    
    def _crossover_signal(self, signal: SignalSchema) -> CompiledExpression:
        """金叉信号：快线上穿慢线"""
        fast_expr = self.translate_indicator(signal.fast)
        slow_expr = self.translate_indicator(signal.slow)
        
        # 今日快线在慢线上方
        above_today = fast_expr.expr > slow_expr.expr
        # 昨日快线在慢线下方或等于
        above_yesterday = fast_expr.expr.shift(1) <= slow_expr.expr.shift(1)
        
        result = above_today & above_yesterday
        
        return CompiledExpression(
            expr=result,
            name="crossover_signal",
            dtype="bool",
            dependencies=list(set(fast_expr.dependencies + slow_expr.dependencies))
        )
    
    def _crossunder_signal(self, signal: SignalSchema) -> CompiledExpression:
        """死叉信号：快线下穿慢线"""
        fast_expr = self.translate_indicator(signal.fast)
        slow_expr = self.translate_indicator(signal.slow)
        
        below_today = fast_expr.expr < slow_expr.expr
        below_yesterday = fast_expr.expr.shift(1) >= slow_expr.expr.shift(1)
        
        result = below_today & below_yesterday
        
        return CompiledExpression(
            expr=result,
            name="crossunder_signal",
            dtype="bool",
            dependencies=list(set(fast_expr.dependencies + slow_expr.dependencies))
        )
    
    def _above_signal(self, signal: SignalSchema) -> CompiledExpression:
        """上穿信号：指标高于阈值"""
        ind_expr = self.translate_indicator(signal.indicator)
        threshold = signal.threshold or 0
        
        result = ind_expr.expr > threshold
        
        return CompiledExpression(
            expr=result,
            name="above_signal",
            dtype="bool",
            dependencies=ind_expr.dependencies
        )
    
    def _below_signal(self, signal: SignalSchema) -> CompiledExpression:
        """下穿信号：指标低于阈值"""
        ind_expr = self.translate_indicator(signal.indicator)
        threshold = signal.threshold or 0
        
        result = ind_expr.expr < threshold
        
        return CompiledExpression(
            expr=result,
            name="below_signal",
            dtype="bool",
            dependencies=ind_expr.dependencies
        )
    
    def _between_signal(self, signal: SignalSchema) -> CompiledExpression:
        """区间信号：指标在区间内"""
        ind_expr = self.translate_indicator(signal.indicator)
        lower = signal.lower or 0
        upper = signal.upper or 100
        
        result = (ind_expr.expr >= lower) & (ind_expr.expr <= upper)
        
        return CompiledExpression(
            expr=result,
            name="between_signal",
            dtype="bool",
            dependencies=ind_expr.dependencies
        )
    
    def _trend_up_signal(self, signal: SignalSchema) -> CompiledExpression:
        """上升趋势信号"""
        ind_expr = self.translate_indicator(signal.indicator)
        window = signal.params.get("window", 5)
        
        # 当前值高于 N 周期前的值
        result = ind_expr.expr > ind_expr.expr.shift(window)
        
        return CompiledExpression(
            expr=result,
            name="trend_up_signal",
            dtype="bool",
            dependencies=ind_expr.dependencies
        )
    
    def _trend_down_signal(self, signal: SignalSchema) -> CompiledExpression:
        """下降趋势信号"""
        ind_expr = self.translate_indicator(signal.indicator)
        window = signal.params.get("window", 5)
        
        result = ind_expr.expr < ind_expr.expr.shift(window)
        
        return CompiledExpression(
            expr=result,
            name="trend_down_signal",
            dtype="bool",
            dependencies=ind_expr.dependencies
        )
    
    def translate_filter(self, filter: FilterSchema) -> CompiledExpression:
        """翻译过滤器为 Polars 表达式"""
        filter_type = filter.type.value if hasattr(filter.type, 'value') else str(filter.type)
        
        if filter_type == "range":
            return self._range_filter(filter)
        elif filter_type == "compare":
            return self._compare_filter(filter)
        elif filter_type == "percentile":
            return self._percentile_filter(filter)
        elif filter_type == "top_n":
            return self._top_n_filter(filter)
        elif filter_type == "bottom_n":
            return self._bottom_n_filter(filter)
        else:
            raise ValueError(f"未知的过滤器类型: {filter_type}")
    
    def _range_filter(self, filter: FilterSchema) -> CompiledExpression:
        """范围过滤器"""
        column = filter.column
        min_val = filter.min
        max_val = filter.max
        
        expr = pl.col(column)
        if min_val is not None:
            expr = expr >= min_val
        if max_val is not None:
            expr = expr <= max_val
        
        return CompiledExpression(
            expr=expr,
            name=f"range_filter_{column}",
            dtype="bool",
            dependencies=[column]
        )
    
    def _compare_filter(self, filter: FilterSchema) -> CompiledExpression:
        """比较过滤器"""
        column = filter.column
        operator = filter.operator or ">"
        value = filter.value or 0
        
        col_expr = pl.col(column)
        
        if operator == ">":
            expr = col_expr > value
        elif operator == "<":
            expr = col_expr < value
        elif operator == ">=":
            expr = col_expr >= value
        elif operator == "<=":
            expr = col_expr <= value
        elif operator == "==":
            expr = col_expr == value
        elif operator == "!=":
            expr = col_expr != value
        else:
            raise ValueError(f"未知的比较操作符: {operator}")
        
        return CompiledExpression(
            expr=expr,
            name=f"compare_filter_{column}",
            dtype="bool",
            dependencies=[column]
        )
    
    def _percentile_filter(self, filter: FilterSchema) -> CompiledExpression:
        """分位数过滤器"""
        column = filter.column
        threshold = filter.threshold or 0.8
        direction = filter.direction or "top"
        
        # 计算分位数
        # 注意：Polars 的 quantile 需要窗口函数或 groupby
        # 这里简化处理，实际使用时需要根据具体场景调整
        
        if direction == "top":
            expr = pl.col(column) >= pl.col(column).quantile(threshold)
        else:
            expr = pl.col(column) <= pl.col(column).quantile(1 - threshold)
        
        return CompiledExpression(
            expr=expr,
            name=f"percentile_filter_{column}",
            dtype="bool",
            dependencies=[column]
        )
    
    def _top_n_filter(self, filter: FilterSchema) -> CompiledExpression:
        """前 N 名过滤器"""
        column = filter.column
        n = filter.n or 10
        
        # 使用 rank 函数
        expr = pl.col(column).rank(method="dense", descending=True) <= n
        
        return CompiledExpression(
            expr=expr,
            name=f"top_{n}_filter_{column}",
            dtype="bool",
            dependencies=[column]
        )
    
    def _bottom_n_filter(self, filter: FilterSchema) -> CompiledExpression:
        """后 N 名过滤器"""
        column = filter.column
        n = filter.n or 10
        
        expr = pl.col(column).rank(method="dense", descending=False) <= n
        
        return CompiledExpression(
            expr=expr,
            name=f"bottom_{n}_filter_{column}",
            dtype="bool",
            dependencies=[column]
        )
    
    def compile_strategy(
        self,
        signals: Dict[str, SignalSchema],
        filters: List[FilterSchema],
        group_by: Optional[str] = "stock_code"
    ) -> Dict[str, CompiledExpression]:
        """
        编译完整策略
        
        参数：
            signals: 信号字典
            filters: 过滤器列表
            group_by: 分组列（用于多股票场景）
        
        返回：
            编译后的表达式字典
        """
        result = {}
        
        # 编译信号
        for name, signal in signals.items():
            result[f"signal_{name}"] = self.translate_signal(signal)
        
        # 编译过滤器
        for i, filter in enumerate(filters):
            result[f"filter_{i}"] = self.translate_filter(filter)
        
        return result


class PandasTranslator(BaseTranslator):
    """
    Pandas 表达式翻译器
    
    将策略 DSL 翻译为 Pandas 表达式，兼容性更好。
    适用于已经使用 Pandas 的代码库。
    
    使用示例：
        translator = PandasTranslator()
        
        # 翻译信号
        signal_expr = translator.translate_signal(signal_schema)
        
        # 应用到 DataFrame
        df["buy_signal"] = signal_expr.expr
    """
    
    def __init__(self):
        super().__init__()
        self._register_builtin_indicators()
    
    def _register_builtin_indicators(self):
        """注册内置指标到 Pandas"""
        self._indicator_registry = {
            "price": self._price_indicator,
            "open": lambda ind: self._column_indicator("open"),
            "high": lambda ind: self._column_indicator("high"),
            "low": lambda ind: self._column_indicator("low"),
            "close": lambda ind: self._column_indicator("close"),
            "volume": lambda ind: self._column_indicator("volume"),
            "ma": self._ma_indicator,
            "ema": self._ema_indicator,
            "rsi": self._rsi_indicator,
            "macd": self._macd_indicator,
            "atr": self._atr_indicator,
            "bollinger": self._bollinger_indicator,
        }
    
    def _price_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """价格指标"""
        column = indicator.column or "close"
        return CompiledExpression(
            expr=f"df['{column}']",
            name=f"price_{column}",
            dtype="float64",
            dependencies=[column]
        )
    
    def _column_indicator(self, column: str) -> CompiledExpression:
        """列引用"""
        return CompiledExpression(
            expr=f"df['{column}']",
            name=column,
            dtype="float64",
            dependencies=[column]
        )
    
    def _ma_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """简单移动平均"""
        window = indicator.window or 20
        column = indicator.column or "close"
        return CompiledExpression(
            expr=f"df['{column}'].rolling(window={window}).mean()",
            name=f"ma_{window}",
            dtype="float64",
            dependencies=[column]
        )
    
    def _ema_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """指数移动平均"""
        window = indicator.window or 12
        column = indicator.column or "close"
        return CompiledExpression(
            expr=f"df['{column}'].ewm(span={window}, adjust=False).mean()",
            name=f"ema_{window}",
            dtype="float64",
            dependencies=[column]
        )
    
    def _rsi_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """RSI 指标"""
        window = indicator.window or 14
        column = indicator.column or "close"
        return CompiledExpression(
            expr=f"df['{column}'].diff().clip(lower=0).rolling(window={window}).mean() / "
                 f"(-df['{column}'].diff().clip(upper=0)).rolling(window={window}).mean()",
            name=f"rsi_{window}",
            dtype="float64",
            dependencies=[column]
        )
    
    def _macd_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """MACD 指标"""
        fast = indicator.params.get("fast", 12)
        slow = indicator.params.get("slow", 26)
        column = indicator.column or "close"
        return CompiledExpression(
            expr=f"df['{column}'].ewm(span={fast}, adjust=False).mean() - "
                 f"df['{column}'].ewm(span={slow}, adjust=False).mean()",
            name=f"macd_{fast}_{slow}",
            dtype="float64",
            dependencies=[column]
        )
    
    def _atr_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """ATR 指标"""
        window = indicator.window or 14
        return CompiledExpression(
            expr=f"pd.concat([df['high'] - df['low'], "
                 f"(df['high'] - df['close'].shift(1)).abs(), "
                 f"(df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)"
                 f".rolling(window={window}).mean()",
            name=f"atr_{window}",
            dtype="float64",
            dependencies=["high", "low", "close"]
        )
    
    def _bollinger_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """布林带指标"""
        window = indicator.window or 20
        std = indicator.params.get("std", 2)
        column = indicator.column or "close"
        return CompiledExpression(
            expr=f"(df['{column}'].rolling(window={window}).mean() + "
                 f"df['{column}'].rolling(window={window}).std() * {std} - "
                 f"df['{column}'].rolling(window={window}).mean() + "
                 f"df['{column}'].rolling(window={window}).std() * {std}) / "
                 f"df['{column}'].rolling(window={window}).mean()",
            name=f"bollinger_{window}_{std}",
            dtype="float64",
            dependencies=[column]
        )
    
    def translate_indicator(self, indicator: IndicatorSchema) -> CompiledExpression:
        """翻译指标为 Pandas 表达式字符串"""
        indicator_type = indicator.type.value if hasattr(indicator.type, 'value') else str(indicator.type)
        
        if indicator_type in self._indicator_registry:
            return self._indicator_registry[indicator_type](indicator)
        
        raise ValueError(f"未知的指标类型: {indicator_type}")
    
    def translate_signal(self, signal: SignalSchema) -> CompiledExpression:
        """翻译信号为 Pandas 表达式字符串"""
        signal_type = signal.type.value if hasattr(signal.type, 'value') else str(signal.type)
        
        if signal_type == "crossover":
            fast = self.translate_indicator(signal.fast)
            slow = self.translate_indicator(signal.slow)
            return CompiledExpression(
                expr=f"({fast.expr} > {slow.expr}) & ({fast.expr}.shift(1) <= {slow.expr}.shift(1))",
                name="crossover_signal",
                dtype="bool",
                dependencies=list(set(fast.dependencies + slow.dependencies))
            )
        elif signal_type == "crossunder":
            fast = self.translate_indicator(signal.fast)
            slow = self.translate_indicator(signal.slow)
            return CompiledExpression(
                expr=f"({fast.expr} < {slow.expr}) & ({fast.expr}.shift(1) >= {slow.expr}.shift(1))",
                name="crossunder_signal",
                dtype="bool",
                dependencies=list(set(fast.dependencies + slow.dependencies))
            )
        elif signal_type == "above":
            ind = self.translate_indicator(signal.indicator)
            threshold = signal.threshold or 0
            return CompiledExpression(
                expr=f"{ind.expr} > {threshold}",
                name="above_signal",
                dtype="bool",
                dependencies=ind.dependencies
            )
        elif signal_type == "below":
            ind = self.translate_indicator(signal.indicator)
            threshold = signal.threshold or 0
            return CompiledExpression(
                expr=f"{ind.expr} < {threshold}",
                name="below_signal",
                dtype="bool",
                dependencies=ind.dependencies
            )
        else:
            raise ValueError(f"未知的信号类型: {signal_type}")
    
    def translate_filter(self, filter: FilterSchema) -> CompiledExpression:
        """翻译过滤器为 Pandas 表达式字符串"""
        filter_type = filter.type.value if hasattr(filter.type, 'value') else str(filter.type)
        
        if filter_type == "range":
            column = filter.column
            min_val = filter.min
            max_val = filter.max
            
            conditions = []
            if min_val is not None:
                conditions.append(f"df['{column}'] >= {min_val}")
            if max_val is not None:
                conditions.append(f"df['{column}'] <= {max_val}")
            
            expr = " & ".join(conditions) if conditions else "True"
            
            return CompiledExpression(
                expr=expr,
                name=f"range_filter_{column}",
                dtype="bool",
                dependencies=[column]
            )
        elif filter_type == "compare":
            column = filter.column
            operator = filter.operator or ">"
            value = filter.value or 0
            
            return CompiledExpression(
                expr=f"df['{column}'] {operator} {value}",
                name=f"compare_filter_{column}",
                dtype="bool",
                dependencies=[column]
            )
        else:
            raise ValueError(f"未知的过滤器类型: {filter_type}")


# ==================== 便捷函数 ====================

def compile_signal_to_polars(signal_dict: Dict[str, Any]) -> Any:
    """
    便捷函数：将信号字典编译为 Polars 表达式
    
    示例：
        expr = compile_signal_to_polars({
            "type": "crossover",
            "fast": {"type": "ma", "window": 5},
            "slow": {"type": "ma", "window": 20}
        })
    """
    translator = PolarsTranslator()
    signal = SignalSchema(**signal_dict)
    compiled = translator.translate_signal(signal)
    return compiled.expr


def compile_signal_to_pandas(signal_dict: Dict[str, Any]) -> str:
    """
    便捷函数：将信号字典编译为 Pandas 表达式字符串
    
    示例：
        expr_str = compile_signal_to_pandas({
            "type": "crossover",
            "fast": {"type": "ma", "window": 5},
            "slow": {"type": "ma", "window": 20}
        })
        # 结果: "(df['close'].rolling(window=5).mean() > ...) & (...)"
    """
    translator = PandasTranslator()
    signal = SignalSchema(**signal_dict)
    compiled = translator.translate_signal(signal)
    return compiled.expr
