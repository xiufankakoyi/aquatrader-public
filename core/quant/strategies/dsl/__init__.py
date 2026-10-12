"""
策略配置协议 (Strategy DSL) - 向量化的 JSON 策略描述语言

设计目标：
1. 声明式：描述"做什么"而非"怎么做"
2. 向量化：天然适合 Polars/NumPy 批量计算
3. AI 友好：结构清晰，便于 LLM 生成和解析
4. 安全隔离：不执行用户代码，防止注入攻击

核心概念：
- Signal: 信号表达式，生成买卖信号
- Filter: 过滤器，筛选股票池
- Risk: 风控规则，管理仓位和止损
- Action: 交易动作，执行买卖

使用示例：
    from core.quant.strategies.dsl import StrategyDSL, SignalExpr
    
    # AI 生成的策略配置
    config = {
        "signal": {
            "type": "crossover",
            "fast": {"type": "ma", "window": 5},
            "slow": {"type": "ma", "window": 20}
        },
        "filter": {
            "market_cap": {">": 200000, "<": 1000000}
        }
    }
    
    # 编译为 Polars 表达式
    dsl = StrategyDSL()
    expr = dsl.compile_signal(config["signal"])
"""

from .schema import (
    StrategySchema,
    SignalSchema,
    FilterSchema,
    RiskSchema,
    ActionSchema,
)
from .compiler import (
    DSLCompiler,
    OptimizingCompiler,
)
from .translator import (
    PolarsTranslator,
    PandasTranslator,
    CompiledExpression,
)

__all__ = [
    # Schema
    'StrategySchema',
    'SignalSchema',
    'FilterSchema',
    'RiskSchema',
    'ActionSchema',
    # Compiler
    'DSLCompiler',
    'OptimizingCompiler',
    # Translators
    'PolarsTranslator',
    'PandasTranslator',
    'CompiledExpression',
]
