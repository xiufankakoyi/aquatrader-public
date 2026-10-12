"""
DSL 编译器 - 将策略 DSL 编译为可执行代码

编译流程：
    1. 解析：JSON -> Schema 对象
    2. 验证：检查配置合法性
    3. 优化：简化表达式，消除冗余
    4. 代码生成：生成 Polars/Pandas 代码
    5. 缓存：缓存编译结果

使用示例：
    from core.quant.strategies.dsl import DSLCompiler
    
    compiler = DSLCompiler()
    
    # 编译策略
    compiled = compiler.compile(strategy_dict)
    
    # 执行策略
    signals = compiled.execute(df)
"""

from typing import Dict, Any, List, Optional, Callable, Union
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json

from .schema import (
    StrategySchema,
    SignalSchema,
    FilterSchema,
    RiskSchema,
    ActionSchema,
    validate_strategy_schema,
)
from .translator import (
    PolarsTranslator,
    PandasTranslator,
    CompiledExpression,
)


@dataclass
class CompiledStrategy:
    """编译后的策略"""
    strategy_id: str
    strategy_name: str
    
    # 编译后的表达式
    signal_exprs: Dict[str, CompiledExpression] = field(default_factory=dict)
    filter_exprs: List[CompiledExpression] = field(default_factory=list)
    
    # 依赖信息
    required_columns: List[str] = field(default_factory=list)
    required_indicators: List[str] = field(default_factory=list)
    
    # 风控配置
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    max_positions: Optional[int] = None
    position_ratio: float = 0.1
    
    # 原始配置
    raw_config: Dict[str, Any] = field(default_factory=dict)
    
    def execute(self, df, engine: str = "polars") -> Dict[str, Any]:
        """
        执行编译后的策略
        
        参数：
            df: 数据 DataFrame
            engine: 执行引擎 (polars/pandas)
        
        返回：
            信号结果字典
        """
        if engine == "polars":
            return self._execute_polars(df)
        elif engine == "pandas":
            return self._execute_pandas(df)
        else:
            raise ValueError(f"未知的执行引擎: {engine}")
    
    def _execute_polars(self, df) -> Dict[str, Any]:
        """使用 Polars 执行"""
        import polars as pl
        
        # 确保是 Polars DataFrame
        if hasattr(df, 'to_polars'):
            df = df.to_polars()
        
        # 应用过滤器
        filter_mask = None
        for filter_expr in self.filter_exprs:
            if filter_mask is None:
                filter_mask = filter_expr.expr
            else:
                filter_mask = filter_mask & filter_expr.expr
        
        if filter_mask is not None:
            df = df.filter(filter_mask)
        
        # 计算信号
        results = {}
        for name, signal_expr in self.signal_exprs.items():
            df = df.with_columns([
                signal_expr.expr.alias(f"signal_{name}")
            ])
            results[name] = df[f"signal_{name}"].to_list()
        
        return results
    
    def _execute_pandas(self, df) -> Dict[str, Any]:
        """使用 Pandas 执行"""
        # 应用过滤器
        filter_mask = None
        for filter_expr in self.filter_exprs:
            if filter_mask is None:
                filter_mask = eval(filter_expr.expr)
            else:
                filter_mask = filter_mask & eval(filter_expr.expr)
        
        if filter_mask is not None:
            df = df[filter_mask]
        
        # 计算信号
        results = {}
        for name, signal_expr in self.signal_exprs.items():
            df[f"signal_{name}"] = eval(signal_expr.expr)
            results[name] = df[f"signal_{name}"].tolist()
        
        return results


class DSLCompiler:
    """
    DSL 编译器
    
    负责将策略 DSL 编译为可执行代码。
    """
    
    def __init__(self, engine: str = "polars"):
        """
        初始化编译器
        
        参数：
            engine: 目标执行引擎 (polars/pandas)
        """
        self.engine = engine
        
        if engine == "polars":
            self.translator = PolarsTranslator()
        elif engine == "pandas":
            self.translator = PandasTranslator()
        else:
            raise ValueError(f"未知的执行引擎: {engine}")
        
        # 编译缓存
        self._cache: Dict[str, CompiledStrategy] = {}
    
    def compile(self, strategy_dict: Dict[str, Any]) -> CompiledStrategy:
        """
        编译策略
        
        参数：
            strategy_dict: 策略配置字典
        
        返回：
            编译后的策略对象
        """
        # 计算缓存键
        cache_key = self._compute_cache_key(strategy_dict)
        
        # 检查缓存
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # 1. 解析为 Schema
        strategy = StrategySchema(**strategy_dict)
        
        # 2. 验证
        errors = validate_strategy_schema(strategy_dict)
        if errors:
            raise ValueError(f"策略验证失败: {', '.join(errors)}")
        
        # 3. 编译
        compiled = self._compile_strategy(strategy)
        
        # 4. 缓存
        self._cache[cache_key] = compiled
        
        return compiled
    
    def _compute_cache_key(self, strategy_dict: Dict[str, Any]) -> str:
        """计算缓存键"""
        # 使用策略 ID + 配置哈希
        strategy_id = strategy_dict.get("metadata", {}).get("id", "unknown")
        config_hash = hashlib.md5(
            json.dumps(strategy_dict, sort_keys=True).encode()
        ).hexdigest()[:8]
        return f"{strategy_id}_{config_hash}"
    
    def _compile_strategy(self, strategy: StrategySchema) -> CompiledStrategy:
        """编译策略为可执行代码"""
        metadata = strategy.metadata
        
        compiled = CompiledStrategy(
            strategy_id=metadata.id if metadata else "unknown",
            strategy_name=metadata.name if metadata else "Unknown",
            raw_config=strategy.to_dict(),
        )
        
        # 编译信号
        for name, signal in strategy.signals.items():
            compiled.signal_exprs[name] = self.translator.translate_signal(signal)
        
        # 编译过滤器
        for filter in strategy.filters:
            compiled.filter_exprs.append(self.translator.translate_filter(filter))
        
        # 提取依赖
        all_deps = set()
        for expr in compiled.signal_exprs.values():
            all_deps.update(expr.dependencies)
        for expr in compiled.filter_exprs:
            all_deps.update(expr.dependencies)
        compiled.required_columns = list(all_deps)
        
        # 提取风控配置
        for risk in strategy.risk:
            risk_type = risk.type.value if hasattr(risk.type, 'value') else str(risk.type)
            if risk_type == "stop_loss":
                compiled.stop_loss = risk.percentage
            elif risk_type == "take_profit":
                compiled.take_profit = risk.percentage
            elif risk_type == "max_positions":
                compiled.max_positions = int(risk.value) if risk.value else None
            elif risk_type == "position_size":
                compiled.position_ratio = risk.percentage or 0.1
        
        return compiled
    
    def compile_signal(self, signal_dict: Dict[str, Any]) -> CompiledExpression:
        """
        编译单个信号
        
        参数：
            signal_dict: 信号配置字典
        
        返回：
            编译后的表达式
        """
        signal = SignalSchema(**signal_dict)
        return self.translator.translate_signal(signal)
    
    def compile_filter(self, filter_dict: Dict[str, Any]) -> CompiledExpression:
        """
        编译单个过滤器
        
        参数：
            filter_dict: 过滤器配置字典
        
        返回：
            编译后的表达式
        """
        filter = FilterSchema(**filter_dict)
        return self.translator.translate_filter(filter)
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        return {
            "cache_size": len(self._cache),
            "cached_ids": list(self._cache.keys()),
        }
    
    def clear_cache(self):
        """清空编译缓存"""
        self._cache.clear()


class OptimizingCompiler(DSLCompiler):
    """
    优化编译器
    
    在编译过程中进行表达式优化。
    """
    
    def _compile_strategy(self, strategy: StrategySchema) -> CompiledStrategy:
        """编译并优化策略"""
        # 先进行标准编译
        compiled = super()._compile_strategy(strategy)
        
        # 优化：合并相同的过滤器
        compiled.filter_exprs = self._merge_filters(compiled.filter_exprs)
        
        # 优化：提取公共子表达式
        compiled.signal_exprs = self._optimize_signals(compiled.signal_exprs)
        
        return compiled
    
    def _merge_filters(self, filters: List[CompiledExpression]) -> List[CompiledExpression]:
        """合并相同的过滤器"""
        # 简化实现：去重
        seen = set()
        unique_filters = []
        for f in filters:
            key = str(f.expr)
            if key not in seen:
                seen.add(key)
                unique_filters.append(f)
        return unique_filters
    
    def _optimize_signals(self, signals: Dict[str, CompiledExpression]) -> Dict[str, CompiledExpression]:
        """优化信号表达式"""
        # 可以在这里添加更多优化逻辑
        # 例如：常量折叠、死代码消除等
        return signals


# ==================== 便捷函数 ====================

def compile_strategy(strategy_dict: Dict[str, Any], engine: str = "polars") -> CompiledStrategy:
    """
    便捷函数：编译策略
    
    示例：
        compiled = compile_strategy({
            "version": "1.0",
            "metadata": {"id": "test", "name": "Test"},
            "signals": {...}
        })
    """
    compiler = DSLCompiler(engine=engine)
    return compiler.compile(strategy_dict)


def compile_signal(signal_dict: Dict[str, Any], engine: str = "polars") -> CompiledExpression:
    """
    便捷函数：编译信号
    
    示例：
        expr = compile_signal({
            "type": "crossover",
            "fast": {"type": "ma", "window": 5},
            "slow": {"type": "ma", "window": 20}
        })
    """
    compiler = DSLCompiler(engine=engine)
    return compiler.compile_signal(signal_dict)
