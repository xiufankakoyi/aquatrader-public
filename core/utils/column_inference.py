"""
列推断工具类

通过 AST 分析策略代码，自动推断所需列，无需手动声明 required_factors
"""
import ast
import inspect
import logging
import textwrap
from typing import List, Set, Optional, Type

logger = logging.getLogger(__name__)

BASE_REQUIRED_COLUMNS = {'stock_code', 'trade_date'}

KNOWN_FACTOR_COLUMNS = {
    'open', 'high', 'low', 'close', 'volume', 'amount',
    'adj_factor', 'prev_close',
    'total_mv', 'float_mv', 'turnover_rate', 'volume_ratio',
    'ma5', 'ma10', 'ma20', 'ma30', 'ma60',
    'volume_ma5', 'volume_ma10',
    'pe', 'pb', 'ps', 'pcf',
    'is_st', 'is_kc', 'is_cy', 'is_bj',
    'is_limit_up', 'is_limit_down', 'is_suspended',
    'limit_up', 'limit_down',
    'days_listed',
    'open_adj', 'high_adj', 'low_adj', 'close_adj',
    'macd', 'macd_signal', 'macd_hist', 'macd_dif',
    'rsi_6', 'rsi_12', 'rsi_14', 'rsi_24',
    'kdj_k', 'kdj_d', 'kdj_j',
    'boll_upper', 'boll_middle', 'boll_lower',
    'atr_14', 'atr_20',
    'volatility_strength', 'vs',
}


class AttributeVisitor(ast.NodeVisitor):
    """
    AST 访问器，提取 self.xxx 属性访问
    """
    
    def __init__(self):
        self.attributes: Set[str] = set()
        self._in_self_context = False
    
    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == 'self':
            self.attributes.add(node.attr)
        elif isinstance(node.value, ast.Attribute):
            if isinstance(node.value.value, ast.Name) and node.value.value.id == 'self':
                self.attributes.add(node.value.attr)
                self.attributes.add(node.attr)
        self.generic_visit(node)
    
    def visit_Subscript(self, node: ast.Subscript):
        if isinstance(node.value, ast.Attribute):
            if isinstance(node.value.value, ast.Name) and node.value.value.id == 'self':
                self.attributes.add(node.value.attr)
        self.generic_visit(node)


class ColumnInference:
    """
    列推断工具类
    
    通过 AST 分析策略代码，自动推断所需列
    
    使用方式:
        columns = ColumnInference.infer(strategy_instance)
        # {'close', 'open', 'ma5', 'ma10', ...}
    """
    
    _cache: dict = {}
    
    @classmethod
    def infer(cls, strategy) -> Set[str]:
        """
        推断策略所需列
        
        Args:
            strategy: 策略实例或策略类
            
        Returns:
            所需列名的集合
        """
        strategy_class = strategy if isinstance(strategy, type) else strategy.__class__
        cache_key = f"{strategy_class.__module__}.{strategy_class.__name__}"
        
        if cache_key in cls._cache:
            return cls._cache[cache_key].copy()
        
        columns: Set[str] = set(BASE_REQUIRED_COLUMNS)
        
        required_factors = getattr(strategy_class, 'required_factors', None)
        if required_factors:
            columns.update(required_factors)
            logger.debug(f"[ColumnInference] 从 required_factors 获取: {required_factors}")
        
        columns.update(cls._infer_from_method(strategy_class, 'generate_signals_vectorized'))
        columns.update(cls._infer_from_method(strategy_class, 'generate_signals'))
        columns.update(cls._infer_from_method(strategy_class, 'prepare_data'))
        
        columns.update(cls._infer_from_indicators(strategy_class))
        
        inferred_columns = columns & KNOWN_FACTOR_COLUMNS
        inferred_columns.update(BASE_REQUIRED_COLUMNS)
        
        cls._cache[cache_key] = inferred_columns
        
        logger.info(f"[ColumnInference] 推断 {strategy_class.__name__} 所需列: {sorted(inferred_columns)}")
        
        return inferred_columns
    
    @classmethod
    def _infer_from_method(cls, strategy_class: Type, method_name: str) -> Set[str]:
        """
        从指定方法中推断所需列
        
        Args:
            strategy_class: 策略类
            method_name: 方法名
            
        Returns:
            推断的列名集合
        """
        columns: Set[str] = set()
        
        method = getattr(strategy_class, method_name, None)
        if method is None:
            return columns
        
        try:
            source = inspect.getsource(method)
            if not source:
                return columns
            
            source = textwrap.dedent(source)
            
            tree = ast.parse(source)
            visitor = AttributeVisitor()
            visitor.visit(tree)
            
            columns.update(visitor.attributes)
            
        except (OSError, TypeError, IndentationError, SyntaxError) as e:
            logger.debug(f"[ColumnInference] 解析 {method_name} 失败: {e}")
        
        return columns
    
    @classmethod
    def _infer_from_indicators(cls, strategy_class: Type) -> Set[str]:
        """
        从策略的指标配置中推断所需列
        
        Args:
            strategy_class: 策略类
            
        Returns:
            推断的列名集合
        """
        columns: Set[str] = set()
        
        indicator_configs = getattr(strategy_class, 'indicator_configs', None)
        if indicator_configs:
            for config in indicator_configs:
                if isinstance(config, dict):
                    indicator_type = config.get('type', '')
                    
                    if indicator_type in ['ma', 'ema', 'sma']:
                        columns.add('close')
                    elif indicator_type in ['volume_ma']:
                        columns.add('volume')
                    elif indicator_type in ['macd']:
                        columns.add('close')
                    elif indicator_type in ['rsi']:
                        columns.add('close')
                    elif indicator_type in ['kdj']:
                        columns.add('close')
                        columns.add('high')
                        columns.add('low')
                    elif indicator_type in ['boll']:
                        columns.add('close')
                    elif indicator_type in ['atr']:
                        columns.add('high')
                        columns.add('low')
                        columns.add('close')
        
        return columns
    
    @classmethod
    def get_required_columns(
        cls,
        strategy,
        additional_columns: Optional[List[str]] = None
    ) -> List[str]:
        """
        获取策略所需列列表（有序）
        
        Args:
            strategy: 策略实例或策略类
            additional_columns: 额外需要的列
            
        Returns:
            所需列名列表
        """
        columns = cls.infer(strategy)
        
        if additional_columns:
            columns.update(additional_columns)
        
        ordered_columns = ['stock_code', 'trade_date']
        
        ohlcv = ['open', 'high', 'low', 'close', 'volume', 'amount']
        for col in ohlcv:
            if col in columns and col not in ordered_columns:
                ordered_columns.append(col)
        
        adj_cols = ['adj_factor', 'prev_close']
        for col in adj_cols:
            if col in columns and col not in ordered_columns:
                ordered_columns.append(col)
        
        limit_cols = ['limit_up', 'limit_down', 'is_limit_up', 'is_limit_down']
        for col in limit_cols:
            if col in columns and col not in ordered_columns:
                ordered_columns.append(col)
        
        other_cols = sorted([c for c in columns if c not in ordered_columns])
        ordered_columns.extend(other_cols)
        
        return ordered_columns
    
    @classmethod
    def clear_cache(cls):
        """清空缓存"""
        cls._cache.clear()
