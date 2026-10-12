"""
AI 友好的策略生成接口

为 LLM (大语言模型) 提供结构化的策略生成和验证接口。
这是实现 "AI 生成策略" 的关键组件。

设计理念：
1. 结构化 Prompt：提供清晰的 Schema 和示例
2. 验证反馈：自动验证生成的策略并给出反馈
3. 迭代优化：支持策略的迭代改进
4. 安全隔离：生成的策略不执行代码，只生成 DSL

使用示例：
    from core.quant.strategies.dsl.ai_interface import AIStrategyGenerator
    
    generator = AIStrategyGenerator()
    
    # 从自然语言描述生成策略
    strategy_json = generator.generate_from_description(
        "创建一个双均线策略，当5日均线上穿20日均线时买入，下穿时卖出"
    )
    
    # 验证策略
    validation = generator.validate_strategy(strategy_json)
    if validation.is_valid:
        print("策略有效！")
    else:
        print(f"错误: {validation.errors}")
"""

import json
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from .schema import (
    StrategySchema,
    SignalSchema,
    FilterSchema,
    RiskSchema,
    ActionSchema,
    IndicatorSchema,
    SignalType,
    IndicatorType,
    FilterType,
    RiskType,
    ActionType,
    validate_strategy_schema,
    STRATEGY_JSON_SCHEMA,
)
from .translator import PolarsTranslator, PandasTranslator


@dataclass
class ValidationResult:
    """策略验证结果"""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
        }


@dataclass
class StrategyTemplate:
    """策略模板"""
    name: str
    description: str
    category: str  # trend_following, mean_reversion, breakout, etc.
    template: Dict[str, Any]
    parameters: Dict[str, Any] = field(default_factory=dict)


class AIStrategyGenerator:
    """
    AI 策略生成器
    
    为 LLM 提供结构化的策略生成支持。
    """
    
    def __init__(self):
        self.templates = self._load_templates()
        self.translator = PolarsTranslator()
    
    def _load_templates(self) -> Dict[str, StrategyTemplate]:
        """加载策略模板"""
        return {
            "dual_ma": StrategyTemplate(
                name="双均线策略",
                description="基于两条移动平均线交叉的趋势跟踪策略",
                category="trend_following",
                template={
                    "version": "1.0",
                    "metadata": {
                        "id": "dual_ma_{timestamp}",
                        "name": "双均线策略",
                        "description": "当短期均线上穿长期均线时买入，下穿时卖出",
                        "tags": ["趋势跟踪", "均线"]
                    },
                    "signals": {
                        "buy": {
                            "type": "crossover",
                            "fast": {"type": "ma", "window": 5, "column": "close"},
                            "slow": {"type": "ma", "window": 20, "column": "close"}
                        },
                        "sell": {
                            "type": "crossunder",
                            "fast": {"type": "ma", "window": 5, "column": "close"},
                            "slow": {"type": "ma", "window": 20, "column": "close"}
                        }
                    },
                    "filters": [
                        {"type": "range", "column": "market_cap", "min": 200000, "max": 1000000}
                    ],
                    "risk": [
                        {"type": "stop_loss", "percentage": 0.08},
                        {"type": "max_positions", "value": 10}
                    ],
                    "actions": [
                        {"type": "buy", "signal": "buy", "position_ratio": 0.1},
                        {"type": "sell", "signal": "sell"}
                    ]
                },
                parameters={
                    "fast_window": {"type": "int", "default": 5, "min": 2, "max": 30},
                    "slow_window": {"type": "int", "default": 20, "min": 5, "max": 60},
                    "stop_loss": {"type": "float", "default": 0.08, "min": 0.01, "max": 0.2},
                }
            ),
            "rsi_mean_reversion": StrategyTemplate(
                name="RSI 均值回归策略",
                description="基于 RSI 超买超卖的均值回归策略",
                category="mean_reversion",
                template={
                    "version": "1.0",
                    "metadata": {
                        "id": "rsi_mean_reversion_{timestamp}",
                        "name": "RSI 均值回归策略",
                        "description": "RSI低于30时买入，高于70时卖出",
                        "tags": ["均值回归", "RSI"]
                    },
                    "signals": {
                        "buy": {
                            "type": "below",
                            "indicator": {"type": "rsi", "window": 14, "column": "close"},
                            "threshold": 30
                        },
                        "sell": {
                            "type": "above",
                            "indicator": {"type": "rsi", "window": 14, "column": "close"},
                            "threshold": 70
                        }
                    },
                    "filters": [
                        {"type": "range", "column": "market_cap", "min": 100000}
                    ],
                    "risk": [
                        {"type": "stop_loss", "percentage": 0.05},
                        {"type": "max_positions", "value": 5}
                    ],
                    "actions": [
                        {"type": "buy", "signal": "buy", "position_ratio": 0.1},
                        {"type": "sell", "signal": "sell"}
                    ]
                },
                parameters={
                    "rsi_window": {"type": "int", "default": 14, "min": 5, "max": 30},
                    "oversold": {"type": "int", "default": 30, "min": 10, "max": 40},
                    "overbought": {"type": "int", "default": 70, "min": 60, "max": 90},
                }
            ),
            "macd_momentum": StrategyTemplate(
                name="MACD 动量策略",
                description="基于 MACD 金叉死叉的动量策略",
                category="momentum",
                template={
                    "version": "1.0",
                    "metadata": {
                        "id": "macd_momentum_{timestamp}",
                        "name": "MACD 动量策略",
                        "description": "MACD线上穿信号线时买入，下穿时卖出",
                        "tags": ["动量", "MACD"]
                    },
                    "signals": {
                        "buy": {
                            "type": "crossover",
                            "fast": {"type": "macd", "params": {"fast": 12, "slow": 26}, "column": "close"},
                            "slow": {"type": "ma", "window": 9, "column": "close"}
                        },
                        "sell": {
                            "type": "crossunder",
                            "fast": {"type": "macd", "params": {"fast": 12, "slow": 26}, "column": "close"},
                            "slow": {"type": "ma", "window": 9, "column": "close"}
                        }
                    },
                    "filters": [],
                    "risk": [
                        {"type": "stop_loss", "percentage": 0.1},
                        {"type": "max_positions", "value": 8}
                    ],
                    "actions": [
                        {"type": "buy", "signal": "buy", "position_ratio": 0.1},
                        {"type": "sell", "signal": "sell"}
                    ]
                },
                parameters={
                    "macd_fast": {"type": "int", "default": 12, "min": 5, "max": 20},
                    "macd_slow": {"type": "int", "default": 26, "min": 15, "max": 40},
                    "macd_signal": {"type": "int", "default": 9, "min": 5, "max": 15},
                }
            ),
            "bollinger_breakout": StrategyTemplate(
                name="布林带突破策略",
                description="基于布林带上下轨的突破策略",
                category="breakout",
                template={
                    "version": "1.0",
                    "metadata": {
                        "id": "bollinger_breakout_{timestamp}",
                        "name": "布林带突破策略",
                        "description": "价格突破布林带上轨时买入，跌破下轨时卖出",
                        "tags": ["突破", "布林带"]
                    },
                    "signals": {
                        "buy": {
                            "type": "above",
                            "indicator": {"type": "close"},
                            "threshold": 0  # 需要配合布林带计算
                        },
                        "sell": {
                            "type": "below",
                            "indicator": {"type": "close"},
                            "threshold": 0
                        }
                    },
                    "filters": [],
                    "risk": [
                        {"type": "stop_loss", "percentage": 0.05}
                    ],
                    "actions": [
                        {"type": "buy", "signal": "buy", "position_ratio": 0.1},
                        {"type": "sell", "signal": "sell"}
                    ]
                },
                parameters={
                    "bollinger_window": {"type": "int", "default": 20, "min": 10, "max": 50},
                    "bollinger_std": {"type": "float", "default": 2.0, "min": 1.0, "max": 3.0},
                }
            ),
        }
    
    def get_system_prompt(self) -> str:
        """
        获取系统 Prompt，用于指导 LLM 生成策略
        
        返回：
            系统 Prompt 字符串
        """
        prompt = """你是一个专业的量化交易策略生成助手。你的任务是将用户的自然语言描述转换为结构化的策略配置（JSON 格式）。

## 策略 DSL Schema

策略配置必须遵循以下 JSON Schema：

```json
{schema}
```

## 核心概念

1. **信号 (signals)**: 定义买卖条件
   - `crossover`: 金叉（快线上穿慢线）
   - `crossunder`: 死叉（快线下穿慢线）
   - `above`: 指标高于阈值
   - `below`: 指标低于阈值
   - `between`: 指标在区间内

2. **指标 (indicators)**: 技术指标
   - `ma`: 简单移动平均
   - `ema`: 指数移动平均
   - `rsi`: 相对强弱指标
   - `macd`: MACD 指标
   - `bollinger`: 布林带
   - `atr`: 平均真实波幅

3. **过滤器 (filters)**: 股票筛选条件
   - `range`: 范围过滤（如市值范围）
   - `compare`: 比较过滤（如价格大于某值）
   - `percentile`: 分位数过滤（如前20%）
   - `top_n`: 前 N 名

4. **风控 (risk)**: 风险管理规则
   - `stop_loss`: 止损比例
   - `take_profit`: 止盈比例
   - `max_positions`: 最大持仓数
   - `position_size`: 仓位大小

5. **动作 (actions)**: 交易执行
   - `buy`: 买入
   - `sell`: 卖出
   - `position_ratio`: 仓位比例
   - `max_stocks`: 最大选股数

## 生成规则

1. **必须包含的字段**:
   - `version`: DSL 版本（固定为 "1.0"）
   - `metadata.id`: 策略唯一标识
   - `metadata.name`: 策略名称
   - 至少一个信号定义

2. **命名规范**:
   - 策略 ID 使用小写字母和下划线，如 `dual_ma_v1`
   - 信号名称使用 `buy` 和 `sell`（推荐）

3. **参数范围**:
   - 均线周期: 2-252
   - RSI 周期: 5-30
   - 止损比例: 0.01-0.30 (1%-30%)
   - 仓位比例: 0.01-1.0 (1%-100%)

4. **安全限制**:
   - 不要生成执行任意代码的策略
   - 只使用预定义的指标类型
   - 参数必须在合理范围内

## 响应格式

请直接返回 JSON 格式的策略配置，不要包含其他解释文字。

示例响应：
```json
{{
    "version": "1.0",
    "metadata": {{
        "id": "example_strategy",
        "name": "示例策略",
        "description": "这是一个示例策略"
    }},
    "signals": {{
        "buy": {{
            "type": "crossover",
            "fast": {{"type": "ma", "window": 5}},
            "slow": {{"type": "ma", "window": 20}}
        }}
    }}
}}
```
"""
        return prompt.format(schema=json.dumps(STRATEGY_JSON_SCHEMA, indent=2, ensure_ascii=False))
    
    def get_available_templates(self) -> List[Dict[str, Any]]:
        """获取可用的策略模板列表"""
        return [
            {
                "id": key,
                "name": template.name,
                "description": template.description,
                "category": template.category,
                "parameters": template.parameters,
            }
            for key, template in self.templates.items()
        ]
    
    def get_template(self, template_id: str) -> Optional[StrategyTemplate]:
        """获取指定模板"""
        return self.templates.get(template_id)
    
    def generate_from_template(
        self,
        template_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        strategy_id: Optional[str] = None,
        strategy_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        从模板生成策略
        
        参数：
            template_id: 模板 ID
            parameters: 模板参数
            strategy_id: 策略 ID（可选，默认自动生成）
            strategy_name: 策略名称（可选）
        
        返回：
            策略配置字典
        """
        template = self.templates.get(template_id)
        if template is None:
            raise ValueError(f"模板不存在: {template_id}")
        
        # 深拷贝模板
        import copy
        strategy = copy.deepcopy(template.template)
        
        # 应用参数
        if parameters:
            strategy = self._apply_parameters(strategy, parameters)
        
        # 设置策略 ID 和名称
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if strategy_id:
            strategy["metadata"]["id"] = strategy_id
        else:
            strategy["metadata"]["id"] = strategy["metadata"]["id"].format(timestamp=timestamp)
        
        if strategy_name:
            strategy["metadata"]["name"] = strategy_name
        
        return strategy
    
    def _apply_parameters(self, strategy: Dict[str, Any], parameters: Dict[str, Any]) -> Dict[str, Any]:
        """应用参数到策略"""
        # 将参数应用到策略的各个部分
        # 这里简化处理，实际应用中可能需要更复杂的参数替换逻辑
        strategy_str = json.dumps(strategy)
        
        for key, value in parameters.items():
            placeholder = f"${{{key}}}"
            strategy_str = strategy_str.replace(placeholder, str(value))
        
        return json.loads(strategy_str)
    
    def validate_strategy(self, strategy_dict: Dict[str, Any]) -> ValidationResult:
        """
        验证策略配置
        
        参数：
            strategy_dict: 策略配置字典
        
        返回：
            验证结果
        """
        errors = validate_strategy_schema(strategy_dict)
        warnings = []
        suggestions = []
        
        if not errors:
            # 额外检查
            signals = strategy_dict.get("signals", {})
            
            # 检查是否有买卖信号
            if "buy" not in signals and "sell" not in signals:
                warnings.append("策略没有定义 buy 或 sell 信号")
            
            # 检查风控
            risk = strategy_dict.get("risk", [])
            has_stop_loss = any(r.get("type") == "stop_loss" for r in risk)
            if not has_stop_loss:
                suggestions.append("建议添加止损规则 (stop_loss) 以控制风险")
            
            # 检查过滤器
            filters = strategy_dict.get("filters", [])
            if not filters:
                suggestions.append("建议添加过滤器以缩小选股范围，提高策略效率")
            
            # 检查参数范围
            for signal_name, signal in signals.items():
                if "fast" in signal and "slow" in signal:
                    fast_window = signal["fast"].get("window", 5)
                    slow_window = signal["slow"].get("window", 20)
                    if fast_window >= slow_window:
                        errors.append(f"信号 '{signal_name}': 快线周期必须小于慢线周期")
        
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            suggestions=suggestions,
        )
    
    def fix_strategy(self, strategy_dict: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        """
        自动修复策略中的常见问题
        
        参数：
            strategy_dict: 策略配置字典
        
        返回：
            (修复后的策略, 修复记录)
        """
        fixed = strategy_dict.copy()
        fixes = []
        
        # 修复 1: 添加缺失的必需字段
        if "version" not in fixed:
            fixed["version"] = "1.0"
            fixes.append("添加缺失的 version 字段")
        
        if "metadata" not in fixed:
            fixed["metadata"] = {}
            fixes.append("添加缺失的 metadata 字段")
        
        if "id" not in fixed.get("metadata", {}):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            fixed["metadata"]["id"] = f"strategy_{timestamp}"
            fixes.append("添加缺失的 metadata.id 字段")
        
        if "name" not in fixed.get("metadata", {}):
            fixed["metadata"]["name"] = "未命名策略"
            fixes.append("添加缺失的 metadata.name 字段")
        
        # 修复 2: 确保 signals 存在
        if "signals" not in fixed:
            fixed["signals"] = {}
            fixes.append("添加空的 signals 字段")
        
        # 修复 3: 确保 filters 是列表
        if "filters" in fixed and not isinstance(fixed["filters"], list):
            fixed["filters"] = [fixed["filters"]]
            fixes.append("将 filters 转换为列表")
        
        # 修复 4: 确保 risk 是列表
        if "risk" in fixed and not isinstance(fixed["risk"], list):
            fixed["risk"] = [fixed["risk"]]
            fixes.append("将 risk 转换为列表")
        
        # 修复 5: 确保 actions 是列表
        if "actions" in fixed and not isinstance(fixed["actions"], list):
            fixed["actions"] = [fixed["actions"]]
            fixes.append("将 actions 转换为列表")
        
        return fixed, fixes
    
    def get_strategy_summary(self, strategy_dict: Dict[str, Any]) -> str:
        """
        获取策略的文本摘要（用于展示给人类）
        
        参数：
            strategy_dict: 策略配置字典
        
        返回：
            策略摘要文本
        """
        metadata = strategy_dict.get("metadata", {})
        signals = strategy_dict.get("signals", {})
        filters = strategy_dict.get("filters", [])
        risk = strategy_dict.get("risk", [])
        
        lines = [
            f"策略名称: {metadata.get('name', '未命名')}",
            f"策略 ID: {metadata.get('id', '未知')}",
            f"描述: {metadata.get('description', '无')}",
            "",
            "信号:",
        ]
        
        for signal_name, signal in signals.items():
            signal_type = signal.get("type", "unknown")
            if signal_type == "crossover":
                fast = signal.get("fast", {}).get("window", "?")
                slow = signal.get("slow", {}).get("window", "?")
                lines.append(f"  - {signal_name}: {fast}日均线上穿{slow}日均线")
            elif signal_type == "crossunder":
                fast = signal.get("fast", {}).get("window", "?")
                slow = signal.get("slow", {}).get("window", "?")
                lines.append(f"  - {signal_name}: {fast}日均线下穿{slow}日均线")
            elif signal_type == "above":
                threshold = signal.get("threshold", "?")
                lines.append(f"  - {signal_name}: 指标高于 {threshold}")
            elif signal_type == "below":
                threshold = signal.get("threshold", "?")
                lines.append(f"  - {signal_name}: 指标低于 {threshold}")
            else:
                lines.append(f"  - {signal_name}: {signal_type}")
        
        if filters:
            lines.append("")
            lines.append("过滤器:")
            for f in filters:
                filter_type = f.get("type", "unknown")
                column = f.get("column", "?")
                if filter_type == "range":
                    min_val = f.get("min", "-")
                    max_val = f.get("max", "-")
                    lines.append(f"  - {column}: {min_val} ~ {max_val}")
                elif filter_type == "compare":
                    operator = f.get("operator", "?")
                    value = f.get("value", "?")
                    lines.append(f"  - {column} {operator} {value}")
        
        if risk:
            lines.append("")
            lines.append("风控:")
            for r in risk:
                risk_type = r.get("type", "unknown")
                if risk_type == "stop_loss":
                    percentage = r.get("percentage", "?")
                    lines.append(f"  - 止损: {percentage * 100}%")
                elif risk_type == "max_positions":
                    value = r.get("value", "?")
                    lines.append(f"  - 最大持仓: {value} 只")
                elif risk_type == "position_size":
                    percentage = r.get("percentage", "?")
                    lines.append(f"  - 仓位大小: {percentage * 100}%")
        
        return "\n".join(lines)
    
    def compile_to_polars(self, strategy_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        将策略编译为 Polars 表达式
        
        参数：
            strategy_dict: 策略配置字典
        
        返回：
            编译结果，包含表达式和依赖信息
        """
        translator = PolarsTranslator()
        
        result = {
            "signals": {},
            "filters": [],
            "dependencies": set(),
        }
        
        # 编译信号
        signals = strategy_dict.get("signals", {})
        for name, signal_dict in signals.items():
            signal = SignalSchema(**signal_dict)
            compiled = translator.translate_signal(signal)
            result["signals"][name] = {
                "expr": str(compiled.expr),
                "dependencies": compiled.dependencies,
            }
            result["dependencies"].update(compiled.dependencies)
        
        # 编译过滤器
        filters = strategy_dict.get("filters", [])
        for filter_dict in filters:
            filter = FilterSchema(**filter_dict)
            compiled = translator.translate_filter(filter)
            result["filters"].append({
                "expr": str(compiled.expr),
                "dependencies": compiled.dependencies,
            })
            result["dependencies"].update(compiled.dependencies)
        
        result["dependencies"] = list(result["dependencies"])
        
        return result


# ==================== 便捷函数 ====================

def generate_strategy_prompt(description: str) -> str:
    """
    生成策略的完整 Prompt（用于发送给 LLM）
    
    参数：
        description: 策略描述
    
    返回：
        完整的 Prompt 字符串
    """
    generator = AIStrategyGenerator()
    
    prompt = f"""{generator.get_system_prompt()}

## 用户需求

请根据以下描述生成策略配置：

"{description}"

## 要求

1. 返回有效的 JSON 格式
2. 确保所有必需字段都已填写
3. 参数值在合理范围内
4. 添加适当的过滤器以提高策略质量
5. 添加风控规则以控制风险

请直接返回 JSON，不要包含其他文字。
"""
    
    return prompt


def validate_and_fix_strategy(strategy_dict: Dict[str, Any]) -> Tuple[Dict[str, Any], ValidationResult]:
    """
    验证并自动修复策略
    
    参数：
        strategy_dict: 策略配置字典
    
    返回：
        (修复后的策略, 验证结果)
    """
    generator = AIStrategyGenerator()
    
    # 先尝试修复
    fixed, fixes = generator.fix_strategy(strategy_dict)
    
    # 再验证
    validation = generator.validate_strategy(fixed)
    
    # 添加修复记录到验证结果
    if fixes:
        validation.warnings.insert(0, f"自动修复了 {len(fixes)} 个问题: {', '.join(fixes)}")
    
    return fixed, validation
