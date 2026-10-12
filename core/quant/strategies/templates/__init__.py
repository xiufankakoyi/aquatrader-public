"""
AI 策略模板模块
提供 AI 策略的基础框架和规范

包含：
- AIStrategyBase: AI 策略基类
- AIStrategyConfig: 策略配置类
- AIStrategyGenerator: 代码生成器
- Prompt 模板: 用于将自然语言转换为代码
"""

from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig
from core.quant.strategies.templates.ai_generator import (
    AIStrategyGenerator,
    generate_strategy_code
)
from core.quant.strategies.templates.prompt_template import (
    build_prompt,
    SYSTEM_PROMPT,
    HARD_CONSTRAINTS,
    CODE_TEMPLATE,
    INDICATOR_MAPPING,
    STRATEGY_PATTERNS
)

__all__ = [
    # 基类和配置
    "AIStrategyBase",
    "AIStrategyConfig",
    # 代码生成器
    "AIStrategyGenerator",
    "generate_strategy_code",
    # Prompt 模板
    "build_prompt",
    "SYSTEM_PROMPT",
    "HARD_CONSTRAINTS",
    "CODE_TEMPLATE",
    "INDICATOR_MAPPING",
    "STRATEGY_PATTERNS",
]

