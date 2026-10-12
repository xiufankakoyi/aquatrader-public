"""
策略配置 Schema - 使用 Pydantic 定义配置化策略的数据结构

设计目标：
1. 类型安全：所有配置项都有明确的类型定义
2. 验证机制：自动验证配置合法性
3. 文档生成：可以从 Schema 生成前端表单配置
4. 序列化：支持 YAML/JSON 的导入导出
"""

from typing import Dict, List, Optional, Any, Union
from enum import Enum
from dataclasses import dataclass, field


class ParameterType(Enum):
    """参数类型枚举"""
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    SELECT = "select"


class IndicatorType(Enum):
    """指标类型枚举"""
    MA = "ma"                    # 移动平均线
    EMA = "ema"                  # 指数移动平均线
    RSI = "rsi"                  # 相对强弱指标
    MACD = "macd"                # MACD指标
    BOLLINGER = "bollinger"      # 布林带
    ATR = "atr"                  # 平均真实波幅
    VOLUME_MA = "volume_ma"      # 成交量均线
    CROSSOVER = "crossover"      # 金叉死叉检测
    ABOVE = "above"              # 价格上穿
    BELOW = "below"              # 价格下穿


class ActionType(Enum):
    """交易动作枚举"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class ParameterConfig:
    """
    策略参数配置
    
    用于前端表单生成和参数优化
    
    示例：
        ParameterConfig(
            name="fast_window",
            type=ParameterType.INT,
            default=5,
            min=2,
            max=30,
            label="短期均线周期",
            description="短期移动平均线的计算周期"
        )
    """
    name: str
    type: Union[ParameterType, str]
    default: Any
    min: Optional[Union[int, float]] = None
    max: Optional[Union[int, float]] = None
    step: Optional[Union[int, float]] = None
    options: Optional[List[Dict[str, Any]]] = None  # 用于 SELECT 类型
    label: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None  # 单位，如 "天", "%", "元"
    
    def __post_init__(self):
        """类型转换和验证"""
        if isinstance(self.type, str):
            self.type = ParameterType(self.type)


@dataclass
class IndicatorConfig:
    """
    指标配置
    
    声明策略需要的指标，引擎会预计算这些指标
    
    示例：
        IndicatorConfig(
            name="ma5",
            type=IndicatorType.MA,
            params={"column": "close", "window": "${fast_window}"}
        )
    """
    name: str
    type: Union[IndicatorType, str]
    params: Dict[str, Any] = field(default_factory=dict)
    description: Optional[str] = None
    
    def __post_init__(self):
        """类型转换"""
        if isinstance(self.type, str):
            self.type = IndicatorType(self.type)
    
    def resolve_params(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析参数中的变量引用
        
        支持 ${param_name} 语法引用策略参数
        
        示例：
            params = {"window": "${fast_window}"}
            parameters = {"fast_window": 5}
            result = {"window": 5}
        """
        resolved = {}
        for key, value in self.params.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                param_name = value[2:-1]
                resolved[key] = parameters.get(param_name, value)
            else:
                resolved[key] = value
        return resolved


@dataclass
class ConditionConfig:
    """
    条件配置
    
    支持的条件表达式：
    - 比较: >, <, >=, <=, ==, !=
    - 逻辑: and, or, not
    - 函数: crossover(ma5, ma20), above(close, ma20), etc.
    
    示例：
        # 简单条件
        ConditionConfig(type="comparison", expr="close > ma20")
        
        # 复合条件
        ConditionConfig(type="compound", expr="close > ma5 and ma5 > ma20")
        
        # 函数条件
        ConditionConfig(type="function", expr="crossover(ma5, ma20)")
    """
    type: str  # "comparison", "compound", "function"
    expr: str  # 条件表达式字符串
    description: Optional[str] = None


@dataclass
class RuleConfig:
    """
    交易规则配置
    
    定义买入/卖出条件和对应的仓位管理
    
    示例：
        RuleConfig(
            action=ActionType.BUY,
            condition=ConditionConfig(type="compound", expr="close > ma5 and ma5 > ma20"),
            position_ratio=0.2,
            max_stocks_per_day=5
        )
    """
    action: Union[ActionType, str]
    condition: Union[ConditionConfig, str, Dict[str, Any]]
    position_ratio: Optional[float] = None  # 仓位比例，None 表示使用策略默认值
    max_stocks_per_day: Optional[int] = None  # 每日最大选股数
    priority: int = 0  # 规则优先级，数字越大优先级越高
    
    def __post_init__(self):
        """类型转换"""
        if isinstance(self.action, str):
            self.action = ActionType(self.action)
        
        if isinstance(self.condition, str):
            self.condition = ConditionConfig(type="compound", expr=self.condition)
        elif isinstance(self.condition, dict):
            self.condition = ConditionConfig(**self.condition)


@dataclass
class RiskManagementConfig:
    """
    风险管理配置
    
    示例：
        RiskManagementConfig(
            max_positions=10,
            position_ratio=0.1,
            stop_loss=0.08,
            max_holding_days=20
        )
    """
    max_positions: Optional[int] = None  # 最大持仓数
    position_ratio: Optional[float] = None  # 单票仓位比例
    stop_loss: Optional[float] = None  # 止损比例 (如 0.08 表示 8%)
    take_profit: Optional[float] = None  # 止盈比例
    max_holding_days: Optional[int] = None  # 最大持有天数
    max_drawdown: Optional[float] = None  # 最大回撤限制


@dataclass
class FilterConfig:
    """
    股票过滤配置
    
    在生成信号前对股票池进行预筛选
    
    示例：
        FilterConfig(
            market_cap_min=200000,  # 20亿
            market_cap_max=1000000,  # 100亿
            exclude_st=True,
            exclude_kc=True,
            min_price=2.0
        )
    """
    market_cap_min: Optional[float] = None  # 最小市值 (万元)
    market_cap_max: Optional[float] = None  # 最大市值 (万元)
    price_min: Optional[float] = None  # 最小价格
    price_max: Optional[float] = None  # 最大价格
    volume_min: Optional[float] = None  # 最小成交量
    exclude_st: bool = True  # 排除ST股票
    exclude_kc: bool = False  # 排除科创板
    exclude_cy: bool = False  # 排除创业板
    min_list_days: Optional[int] = None  # 最小上市天数


@dataclass
class StrategyConfig:
    """
    策略配置根对象
    
    完整的策略配置包含所有必要信息
    
    示例：
        StrategyConfig(
            strategy_id="dual_ma_v1",
            name="双均线策略",
            version="1.0",
            description="基于双均线交叉的趋势跟踪策略",
            parameters=[
                ParameterConfig(name="fast_window", type=ParameterType.INT, default=5, min=2, max=30),
                ParameterConfig(name="slow_window", type=ParameterType.INT, default=20, min=5, max=60),
            ],
            indicators=[
                IndicatorConfig(name="ma_fast", type=IndicatorType.MA, params={"column": "close", "window": "${fast_window}"}),
                IndicatorConfig(name="ma_slow", type=IndicatorType.MA, params={"column": "close", "window": "${slow_window}"}),
            ],
            rules=[
                RuleConfig(action=ActionType.BUY, condition="close > ma_fast and ma_fast > ma_slow"),
                RuleConfig(action=ActionType.SELL, condition="close < ma_slow"),
            ],
            risk_management=RiskManagementConfig(max_positions=10, position_ratio=0.1),
            filters=FilterConfig(market_cap_min=200000, exclude_st=True)
        )
    """
    strategy_id: str
    name: str
    version: str = "1.0"
    description: Optional[str] = None
    author: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    
    # 核心配置
    parameters: List[ParameterConfig] = field(default_factory=list)
    indicators: List[IndicatorConfig] = field(default_factory=list)
    rules: List[RuleConfig] = field(default_factory=list)
    
    # 辅助配置
    risk_management: Optional[RiskManagementConfig] = None
    filters: Optional[FilterConfig] = None
    
    # 简化格式的参数存储 (用于兼容旧配置格式)
    _simplified_params: Dict[str, Any] = field(default_factory=dict, repr=False)
    
    # 元数据
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    def __post_init__(self):
        """类型转换"""
        self.parameters = [
            p if isinstance(p, ParameterConfig) else ParameterConfig(**p)
            for p in self.parameters
        ]
        self.indicators = [
            i if isinstance(i, IndicatorConfig) else IndicatorConfig(**i)
            for i in self.indicators
        ]
        self.rules = [
            r if isinstance(r, RuleConfig) else RuleConfig(**r)
            for r in self.rules
        ]
        
        if self.risk_management and isinstance(self.risk_management, dict):
            self.risk_management = RiskManagementConfig(**self.risk_management)
        
        if self.filters and isinstance(self.filters, dict):
            self.filters = FilterConfig(**self.filters)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于序列化）"""
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "tags": self.tags,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type.value if isinstance(p.type, Enum) else p.type,
                    "default": p.default,
                    "min": p.min,
                    "max": p.max,
                    "step": p.step,
                    "options": p.options,
                    "label": p.label,
                    "description": p.description,
                    "unit": p.unit,
                }
                for p in self.parameters
            ],
            "indicators": [
                {
                    "name": i.name,
                    "type": i.type.value if isinstance(i.type, Enum) else i.type,
                    "params": i.params,
                    "description": i.description,
                }
                for i in self.indicators
            ],
            "rules": [
                {
                    "action": r.action.value if isinstance(r.action, Enum) else r.action,
                    "condition": {
                        "type": r.condition.type,
                        "expr": r.condition.expr,
                        "description": r.condition.description,
                    } if isinstance(r.condition, ConditionConfig) else r.condition,
                    "position_ratio": r.position_ratio,
                    "max_stocks_per_day": r.max_stocks_per_day,
                    "priority": r.priority,
                }
                for r in self.rules
            ],
            "risk_management": {
                "max_positions": self.risk_management.max_positions,
                "position_ratio": self.risk_management.position_ratio,
                "stop_loss": self.risk_management.stop_loss,
                "take_profit": self.risk_management.take_profit,
                "max_holding_days": self.risk_management.max_holding_days,
                "max_drawdown": self.risk_management.max_drawdown,
            } if self.risk_management else None,
            "filters": {
                "market_cap_min": self.filters.market_cap_min,
                "market_cap_max": self.filters.market_cap_max,
                "price_min": self.filters.price_min,
                "price_max": self.filters.price_max,
                "volume_min": self.filters.volume_min,
                "exclude_st": self.filters.exclude_st,
                "exclude_kc": self.filters.exclude_kc,
                "exclude_cy": self.filters.exclude_cy,
                "min_list_days": self.filters.min_list_days,
            } if self.filters else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
    
    def get_default_params(self) -> Dict[str, Any]:
        """获取默认参数值"""
        return {p.name: p.default for p in self.parameters}
    
    def validate_params(self, params: Dict[str, Any]) -> List[str]:
        """
        验证参数值是否合法
        
        返回：错误信息列表，空列表表示验证通过
        """
        errors = []
        
        for param in self.parameters:
            value = params.get(param.name)
            
            if value is None:
                errors.append(f"参数 {param.name} 未提供")
                continue
            
            # 类型检查
            if param.type == ParameterType.INT and not isinstance(value, int):
                errors.append(f"参数 {param.name} 应为整数")
            elif param.type == ParameterType.FLOAT and not isinstance(value, (int, float)):
                errors.append(f"参数 {param.name} 应为数字")
            elif param.type == ParameterType.BOOL and not isinstance(value, bool):
                errors.append(f"参数 {param.name} 应为布尔值")
            
            # 范围检查
            if param.min is not None and value < param.min:
                errors.append(f"参数 {param.name} 不能小于 {param.min}")
            if param.max is not None and value > param.max:
                errors.append(f"参数 {param.name} 不能大于 {param.max}")
        
        return errors
