"""
Strategy DSL Schema - JSON Schema 定义

定义策略配置的完整结构，用于验证和文档生成。

Schema 结构：
{
    "version": "1.0",
    "metadata": {...},
    "data": {...},
    "signals": {...},
    "filters": {...},
    "risk": {...},
    "actions": {...}
}
"""

from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field
from enum import Enum


class SignalType(Enum):
    """信号类型"""
    CROSSOVER = "crossover"      # 金叉
    CROSSUNDER = "crossunder"    # 死叉
    ABOVE = "above"              # 上穿
    BELOW = "below"              # 下穿
    BETWEEN = "between"          # 区间
    TREND_UP = "trend_up"        # 上升趋势
    TREND_DOWN = "trend_down"    # 下降趋势
    DIVERGENCE = "divergence"    # 背离
    CONVERGENCE = "convergence"  # 收敛
    CUSTOM = "custom"            # 自定义


class IndicatorType(Enum):
    """指标类型"""
    # 价格指标
    PRICE = "price"              # 价格
    OPEN = "open"                # 开盘价
    HIGH = "high"                # 最高价
    LOW = "low"                  # 最低价
    CLOSE = "close"              # 收盘价
    VWAP = "vwap"                # 成交量加权平均价
    
    # 移动平均线
    MA = "ma"                    # 简单移动平均
    EMA = "ema"                  # 指数移动平均
    WMA = "wma"                  # 加权移动平均
    SMA = "sma"                  # 平滑移动平均
    
    # 动量指标
    RSI = "rsi"                  # 相对强弱指标
    MACD = "macd"                # MACD
    KDJ = "kdj"                  # 随机指标
    CCI = "cci"                  # 商品通道指数
    
    # 波动率指标
    ATR = "atr"                  # 平均真实波幅
    BOLLINGER = "bollinger"      # 布林带
    
    # 成交量指标
    VOLUME = "volume"            # 成交量
    VOL_MA = "vol_ma"            # 成交量均线
    OBV = "obv"                  # 能量潮
    
    # 自定义指标
    CUSTOM = "custom"            # 自定义


class FilterType(Enum):
    """过滤器类型"""
    RANGE = "range"              # 范围过滤
    COMPARE = "compare"          # 比较过滤
    RANK = "rank"                # 排名过滤
    PERCENTILE = "percentile"    # 分位数过滤
    TOP_N = "top_n"              # 前N名
    BOTTOM_N = "bottom_n"        # 后N名


class ActionType(Enum):
    """动作类型"""
    BUY = "buy"                  # 买入
    SELL = "sell"                # 卖出
    HOLD = "hold"                # 持有
    CLOSE = "close"              # 平仓
    SCALE_IN = "scale_in"        # 加仓
    SCALE_OUT = "scale_out"      # 减仓


class RiskType(Enum):
    """风控类型"""
    STOP_LOSS = "stop_loss"      # 止损
    TAKE_PROFIT = "take_profit"  # 止盈
    TRAILING_STOP = "trailing_stop"  # 跟踪止损
    MAX_POSITIONS = "max_positions"  # 最大持仓
    MAX_DRAWDOWN = "max_drawdown"    # 最大回撤
    POSITION_SIZE = "position_size"  # 仓位大小


# ==================== Schema 定义 ====================

@dataclass
class IndicatorSchema:
    """
    指标 Schema
    
    示例：
        {"type": "ma", "window": 20, "column": "close"}
        {"type": "rsi", "window": 14}
        {"type": "bollinger", "window": 20, "std": 2}
    """
    type: Union[IndicatorType, str]
    window: Optional[int] = None
    column: Optional[str] = "close"
    params: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if isinstance(self.type, str):
            self.type = IndicatorType(self.type)


@dataclass
class SignalSchema:
    """
    信号 Schema
    
    示例：
        # 金叉信号
        {
            "type": "crossover",
            "fast": {"type": "ma", "window": 5},
            "slow": {"type": "ma", "window": 20}
        }
        
        # RSI 超卖
        {
            "type": "below",
            "indicator": {"type": "rsi", "window": 14},
            "threshold": 30
        }
    """
    type: Union[SignalType, str]
    # 交叉信号参数
    fast: Optional[Union[IndicatorSchema, Dict[str, Any]]] = None
    slow: Optional[Union[IndicatorSchema, Dict[str, Any]]] = None
    # 阈值信号参数
    indicator: Optional[Union[IndicatorSchema, Dict[str, Any]]] = None
    threshold: Optional[float] = None
    # 区间信号参数
    lower: Optional[float] = None
    upper: Optional[float] = None
    # 自定义参数
    params: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if isinstance(self.type, str):
            self.type = SignalType(self.type)
        
        # 转换字典为对象
        if self.fast and isinstance(self.fast, dict):
            self.fast = IndicatorSchema(**self.fast)
        if self.slow and isinstance(self.slow, dict):
            self.slow = IndicatorSchema(**self.slow)
        if self.indicator and isinstance(self.indicator, dict):
            self.indicator = IndicatorSchema(**self.indicator)


@dataclass
class FilterSchema:
    """
    过滤器 Schema
    
    示例：
        # 市值范围
        {
            "type": "range",
            "column": "market_cap",
            "min": 200000,
            "max": 1000000
        }
        
        # 排名前20%
        {
            "type": "percentile",
            "column": "volume",
            "threshold": 0.8,
            "direction": "top"
        }
    """
    type: Union[FilterType, str]
    column: Optional[str] = None
    # 范围参数
    min: Optional[float] = None
    max: Optional[float] = None
    # 比较参数
    operator: Optional[str] = None  # >, <, >=, <=, ==, !=
    value: Optional[float] = None
    # 排名参数
    n: Optional[int] = None
    threshold: Optional[float] = None
    direction: Optional[str] = "top"  # top, bottom
    # 自定义参数
    params: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if isinstance(self.type, str):
            self.type = FilterType(self.type)


@dataclass
class RiskSchema:
    """
    风控 Schema
    
    示例：
        # 止损
        {"type": "stop_loss", "percentage": 0.08}
        
        # 最大持仓
        {"type": "max_positions", "value": 10}
        
        # 仓位大小
        {"type": "position_size", "percentage": 0.1}
    """
    type: Union[RiskType, str]
    percentage: Optional[float] = None
    value: Optional[float] = None
    params: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if isinstance(self.type, str):
            self.type = RiskType(self.type)


@dataclass
class ActionSchema:
    """
    动作 Schema
    
    示例：
        {
            "type": "buy",
            "signal": {"type": "crossover", ...},
            "position_ratio": 0.1,
            "max_stocks": 5
        }
    """
    type: Union[ActionType, str]
    signal: Optional[Union[SignalSchema, Dict[str, Any]]] = None
    filter: Optional[Union[FilterSchema, Dict[str, Any]]] = None
    position_ratio: Optional[float] = 0.1
    max_stocks: Optional[int] = None
    priority: Optional[int] = 0
    
    def __post_init__(self):
        if isinstance(self.type, str):
            self.type = ActionType(self.type)
        
        if self.signal and isinstance(self.signal, dict):
            self.signal = SignalSchema(**self.signal)
        if self.filter and isinstance(self.filter, dict):
            self.filter = FilterSchema(**self.filter)


@dataclass
class DataSourceSchema:
    """
    数据源 Schema
    
    示例：
        {
            "type": "daily",
            "start_date": "2020-01-01",
            "end_date": "2024-12-31",
            "universe": "all_a_share"
        }
    """
    type: str = "daily"  # daily, minute, tick
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    universe: Optional[str] = "all_a_share"  # all_a_share, hs300, zz500, etc.
    adjust: bool = True  # 是否复权


@dataclass
class MetadataSchema:
    """
    元数据 Schema
    
    示例：
        {
            "id": "dual_ma_v1",
            "name": "双均线策略",
            "version": "1.0",
            "description": "基于双均线交叉的趋势跟踪策略",
            "author": "AI",
            "tags": ["趋势", "均线"],
            "created_at": "2025-02-14T00:00:00",
            "updated_at": "2025-02-14T00:00:00"
        }
    """
    id: str
    name: str
    version: str = "1.0"
    description: Optional[str] = None
    author: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class StrategySchema:
    """
    完整策略 Schema
    
    这是策略 DSL 的根对象，包含策略的所有配置。
    
    示例：
        {
            "version": "1.0",
            "metadata": {
                "id": "dual_ma_v1",
                "name": "双均线策略",
                "version": "1.0"
            },
            "data": {
                "type": "daily",
                "universe": "all_a_share"
            },
            "signals": {
                "buy": {"type": "crossover", "fast": {...}, "slow": {...}},
                "sell": {"type": "crossunder", "fast": {...}, "slow": {...}}
            },
            "filters": [
                {"type": "range", "column": "market_cap", "min": 200000, "max": 1000000}
            ],
            "risk": [
                {"type": "stop_loss", "percentage": 0.08},
                {"type": "max_positions", "value": 10}
            ],
            "actions": [
                {"type": "buy", "signal": "buy", "position_ratio": 0.1}
            ]
        }
    """
    version: str = "1.0"
    metadata: Optional[Union[MetadataSchema, Dict[str, Any]]] = None
    data: Optional[Union[DataSourceSchema, Dict[str, Any]]] = None
    signals: Dict[str, Union[SignalSchema, Dict[str, Any]]] = field(default_factory=dict)
    filters: List[Union[FilterSchema, Dict[str, Any]]] = field(default_factory=list)
    risk: List[Union[RiskSchema, Dict[str, Any]]] = field(default_factory=list)
    actions: List[Union[ActionSchema, Dict[str, Any]]] = field(default_factory=list)
    
    def __post_init__(self):
        if self.metadata and isinstance(self.metadata, dict):
            self.metadata = MetadataSchema(**self.metadata)
        if self.data and isinstance(self.data, dict):
            self.data = DataSourceSchema(**self.data)
        
        self.signals = {
            k: SignalSchema(**v) if isinstance(v, dict) else v
            for k, v in self.signals.items()
        }
        self.filters = [
            FilterSchema(**f) if isinstance(f, dict) else f
            for f in self.filters
        ]
        self.risk = [
            RiskSchema(**r) if isinstance(r, dict) else r
            for r in self.risk
        ]
        self.actions = [
            ActionSchema(**a) if isinstance(a, dict) else a
            for a in self.actions
        ]
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "version": self.version,
            "signals": {},
            "filters": [],
            "risk": [],
            "actions": [],
        }
        
        if self.metadata:
            result["metadata"] = self._dataclass_to_dict(self.metadata)
        if self.data:
            result["data"] = self._dataclass_to_dict(self.data)
        
        for k, v in self.signals.items():
            result["signals"][k] = self._dataclass_to_dict(v)
        for f in self.filters:
            result["filters"].append(self._dataclass_to_dict(f))
        for r in self.risk:
            result["risk"].append(self._dataclass_to_dict(r))
        for a in self.actions:
            result["actions"].append(self._dataclass_to_dict(a))
        
        return result
    
    def _dataclass_to_dict(self, obj) -> Dict[str, Any]:
        """将 dataclass 转换为字典"""
        if hasattr(obj, '__dataclass_fields__'):
            result = {}
            for field_name in obj.__dataclass_fields__:
                value = getattr(obj, field_name)
                if value is not None:
                    if hasattr(value, '__dataclass_fields__'):
                        result[field_name] = self._dataclass_to_dict(value)
                    elif isinstance(value, Enum):
                        result[field_name] = value.value
                    elif isinstance(value, list):
                        result[field_name] = [
                            self._dataclass_to_dict(item) if hasattr(item, '__dataclass_fields__') else item
                            for item in value
                        ]
                    elif isinstance(value, dict):
                        result[field_name] = {
                            k: self._dataclass_to_dict(v) if hasattr(v, '__dataclass_fields__') else v
                            for k, v in value.items()
                        }
                    else:
                        result[field_name] = value
            return result
        return obj


# ==================== JSON Schema 验证 ====================

STRATEGY_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Strategy DSL Schema",
    "type": "object",
    "required": ["version", "metadata"],
    "properties": {
        "version": {
            "type": "string",
            "description": "DSL 版本号"
        },
        "metadata": {
            "type": "object",
            "required": ["id", "name"],
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "version": {"type": "string"},
                "description": {"type": "string"},
                "author": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        },
        "data": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["daily", "minute", "tick"]},
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "universe": {"type": "string"},
                "adjust": {"type": "boolean"}
            }
        },
        "signals": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string"},
                    "fast": {"$ref": "#/definitions/indicator"},
                    "slow": {"$ref": "#/definitions/indicator"},
                    "indicator": {"$ref": "#/definitions/indicator"},
                    "threshold": {"type": "number"},
                    "lower": {"type": "number"},
                    "upper": {"type": "number"}
                }
            }
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string"},
                    "column": {"type": "string"},
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                    "operator": {"type": "string"},
                    "value": {"type": "number"},
                    "n": {"type": "integer"},
                    "threshold": {"type": "number"},
                    "direction": {"type": "string"}
                }
            }
        },
        "risk": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string"},
                    "percentage": {"type": "number"},
                    "value": {"type": "number"}
                }
            }
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["buy", "sell", "hold", "close"]},
                    "signal": {"type": "string"},
                    "filter": {"$ref": "#/definitions/filter"},
                    "position_ratio": {"type": "number"},
                    "max_stocks": {"type": "integer"},
                    "priority": {"type": "integer"}
                }
            }
        }
    },
    "definitions": {
        "indicator": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"type": "string"},
                "window": {"type": "integer"},
                "column": {"type": "string"},
                "params": {"type": "object"}
            }
        }
    }
}


def validate_strategy_schema(data: Dict[str, Any]) -> List[str]:
    """
    验证策略配置是否符合 Schema
    
    返回：错误信息列表，空列表表示验证通过
    """
    errors = []
    
    # 基本字段检查
    if "version" not in data:
        errors.append("缺少必需字段: version")
    if "metadata" not in data:
        errors.append("缺少必需字段: metadata")
    elif "id" not in data.get("metadata", {}):
        errors.append("metadata 缺少必需字段: id")
    elif "name" not in data.get("metadata", {}):
        errors.append("metadata 缺少必需字段: name")
    
    # 信号检查
    signals = data.get("signals", {})
    for name, signal in signals.items():
        if "type" not in signal:
            errors.append(f"信号 '{name}' 缺少必需字段: type")
    
    # 过滤器检查
    filters = data.get("filters", [])
    for i, f in enumerate(filters):
        if "type" not in f:
            errors.append(f"过滤器 [{i}] 缺少必需字段: type")
    
    # 风控检查
    risk_rules = data.get("risk", [])
    for i, r in enumerate(risk_rules):
        if "type" not in r:
            errors.append(f"风控规则 [{i}] 缺少必需字段: type")
    
    # 动作检查
    actions = data.get("actions", [])
    for i, a in enumerate(actions):
        if "type" not in a:
            errors.append(f"动作 [{i}] 缺少必需字段: type")
    
    return errors
