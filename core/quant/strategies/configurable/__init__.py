"""
配置化策略系统 - 无需编写代码，通过配置即可创建策略

核心组件：
1. strategy_config.py - 策略配置 Schema (Pydantic)
2. builtin_indicators.py - 内置指标函数注册表
3. rule_engine.py - 规则引擎 (条件解析与执行)
4. config_loader.py - 配置加载器 (YAML/JSON)
5. configurable_strategy.py - 配置化策略类

使用示例：
    # 加载配置化策略
    from core.quant.strategies.configurable import ConfigurableStrategy, StrategyConfigLoader
    
    loader = StrategyConfigLoader()
    config = loader.load("strategies/configs/dual_ma_strategy.yaml")
    strategy = ConfigurableStrategy(config)
    
    # 运行回测
    engine = UnifiedBacktestEngine(data_query)
    for event in engine.run_backtest(start_date, end_date, strategy):
        print(event)
"""

from .strategy_config import (
    StrategyConfig,
    ParameterConfig,
    IndicatorConfig,
    RuleConfig,
    RiskManagementConfig,
)
from .builtin_indicators import (
    IndicatorRegistry,
    register_indicator,
    get_indicator,
    list_indicators,
)
from .config_loader import StrategyConfigLoader
from .configurable_strategy import ConfigurableStrategy

__all__ = [
    # 配置类
    'StrategyConfig',
    'ParameterConfig',
    'IndicatorConfig',
    'RuleConfig',
    'RiskManagementConfig',
    # 指标注册表
    'IndicatorRegistry',
    'register_indicator',
    'get_indicator',
    'list_indicators',
    # 加载器和策略
    'StrategyConfigLoader',
    'ConfigurableStrategy',
]
