"""
AI 策略基类 - 定义 AI 生成代码的"宪法"

设计目标：
1. 强制 AI 遵守状态管理和参数分离的规则
2. 强制 AI 通过 get_required_indicators() 声明需要的指标，而不是自己计算
3. 强制 AI 在 generate_signals() 中只做 if/else 逻辑判断，不计算指标

使用示例：
    from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig
    
    # 1. 创建配置
    config = AIStrategyConfig(params={
        "rsi_period": 14,
        "rsi_threshold": 30,
        "ma_period": 20,
    })
    
    # 2. 实现策略
    class MyAIStrategy(AIStrategyBase):
        def __init__(self, config: AIStrategyConfig):
            super().__init__(config)
        
        def get_required_indicators(self):
            # 声明需要的指标，参数从 config.params 获取
            return [
                {"name": "RSI", "period": self.config.params.get("rsi_period", 14)},
                {"name": "MA", "period": self.config.params.get("ma_period", 20)},
            ]
        
        def _generate_signals_impl(self, current_date, stock_pool):
            # stock_pool 已经包含了 RSI、MA 等列（由后端计算）
            # 只做逻辑判断，不计算指标
            signals = {}
            rsi_threshold = self.config.params.get("rsi_threshold", 30)
            
            for _, row in stock_pool.iterrows():
                code = row["stock_code"]
                # 使用已有的指标列，不计算
                if row["RSI"] < rsi_threshold and row["close"] > row["MA"]:
                    signals[code] = "buy"
                else:
                    signals[code] = "hold"
            
            return signals
"""

from typing import Dict, List, Optional, Any
import pandas as pd
from dataclasses import dataclass, field
from core.quant.strategies.strategy_framework import StrategyBase


@dataclass
class AIStrategyConfig:
    """
    AI 策略配置规范
    
    强制要求：
    - 所有的参数（如均线周期、RSI阈值）必须是变量，不能是硬编码
    - 参数存储在 self.params 字典中
    - 支持参数优化和前端配置
    """
    params: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """确保 params 是字典类型"""
        if not isinstance(self.params, dict):
            self.params = {}


class AIStrategyBase(StrategyBase):
    """
    AI 策略基类
    
    核心规则：
    1. 参数必须从 config.params 中获取，不能硬编码
    2. 指标必须通过 get_required_indicators() 声明，不能自己计算
    3. 状态管理必须使用 self.holding_state，记录持仓天数等信息
    4. _generate_signals_impl() 只负责逻辑判断，不计算指标
    """
    
    strategy_name = "AI策略基类"
    strategy_template = True
    
    def __init__(self, config: AIStrategyConfig, name: str | None = None):
        """
        初始化 AI 策略
        
        参数：
            config: AIStrategyConfig 实例，包含所有策略参数
            name: 策略名称（可选）
        """
        super().__init__(name)
        self.config = config
        
        # 强制内置状态管理（记录持仓天数、买入价格等）
        # 格式: {stock_code: {"holding_days": int, "buy_price": float, ...}}
        self.holding_state: Dict[str, Dict[str, Any]] = {}
        
        # 记录策略所需的指标列表（由 get_required_indicators() 返回）
        self._required_indicators: List[Dict[str, Any]] = []
    
    def get_required_indicators(self) -> List[Dict[str, Any]]:
        """
        [关键接口 1] 告诉后端：我需要什么数据？
        
        AI 不自己算指标，而是"点菜"
        
        返回格式示例：
            [
                {"name": "RSI", "period": 14, "column": "close"},
                {"name": "MA", "period": 20, "column": "close"},
                {"name": "EMA", "period": 12, "column": "close"},
                {"name": "MACD", "fast": 12, "slow": 26, "signal": 9},
            ]
        
        支持的指标类型：
            - MA: 移动平均线，需要 period 和 column
            - EMA: 指数移动平均线，需要 period 和 column
            - RSI: 相对强弱指标，需要 period 和 column
            - MACD: MACD 指标，需要 fast, slow, signal 和 column
            - BOLL: 布林带，需要 period 和 column
        
        注意：
            - 子类必须重写此方法
            - 参数应该从 self.config.params 中获取，不能硬编码
            - 返回的指标会被后端计算并添加到 stock_pool 中
        
        返回：
            List[Dict[str, Any]]: 指标配置列表
        """
        return []
    
    def _generate_signals_impl(
        self, 
        current_date: str, 
        stock_pool: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        [关键接口 2] 核心逻辑实现（子类必须重写）
        
        输入的 stock_pool 必须已经包含了上面请求的 RSI, MA 等列
        AI 只负责 if/else 逻辑，不计算指标
        
        参数：
            current_date: 当前日期（字符串格式，如 "2024-01-01"）
            stock_pool: DataFrame，包含：
                - 基础列：stock_code, close, open, high, low, volume, total_mv 等
                - 指标列：由 get_required_indicators() 声明的指标（如 RSI, MA 等）
        
        返回：
            Dict[str, str]: {stock_code: "buy"/"sell"/"hold"}
            或 Dict[str, tuple]: {stock_code: ("buy", weight)}
            或 Dict[str, dict]: {stock_code: {"action": "buy", "weight": 0.2, "score": 3.5}}
        
        注意：
            - 子类必须重写此方法
            - 不能在这里计算指标，只能使用 stock_pool 中已有的列
            - 参数应该从 self.config.params 中获取
            - 可以使用 self.holding_state 记录持仓状态
        """
        raise NotImplementedError(
            "子类必须实现 _generate_signals_impl() 方法"
        )
    
    def _update_holding_state(self, date: str, signals: Dict[str, str]):
        """
        更新持仓状态
        
        在生成信号后，更新 self.holding_state，记录：
        - 持仓天数
        - 买入价格
        - 其他自定义状态
        
        参数：
            date: 当前日期
            signals: 生成的信号字典
        """
        # 更新持仓天数
        for code in self.current_portfolio:
            if code not in self.holding_state:
                self.holding_state[code] = {
                    "holding_days": 0,
                    "buy_price": self.current_portfolio[code].get("buy_price", 0.0),
                }
            self.holding_state[code]["holding_days"] += 1
        
        # 清理已卖出的股票状态
        for code in list(self.holding_state.keys()):
            if code not in self.current_portfolio:
                del self.holding_state[code]
    
    def _validate_indicators(self, stock_pool: pd.DataFrame) -> bool:
        """
        验证 stock_pool 是否包含所需的指标
        
        参数：
            stock_pool: 待验证的 DataFrame
        
        返回：
            bool: 是否包含所有必需的指标
        """
        if self._required_indicators is None or len(self._required_indicators) == 0:
            # 首次调用时，获取所需指标
            self._required_indicators = self.get_required_indicators()
        
        if not self._required_indicators:
            return True  # 如果没有声明指标，则不需要验证
        
        # 检查每个指标是否存在于 stock_pool 中
        for indicator in self._required_indicators:
            indicator_name = indicator.get("name", "").upper()
            # 检查指标列是否存在（可能以不同格式命名，如 "RSI_14", "MA_20" 等）
            found = False
            for col in stock_pool.columns:
                if indicator_name in col.upper():
                    found = True
                    break
            if not found:
                print(
                    f"[AIStrategyBase] 警告: stock_pool 中缺少指标 {indicator_name}，"
                    f"请确保后端已计算并添加该指标"
                )
                return False
        
        return True
    
    # ====== 重写基类的 generate_signals 方法 ======
    
    def generate_signals(
        self, 
        current_date: str, 
        stock_pool: pd.DataFrame,
        data_query: Any = None
    ) -> Dict[str, str]:
        """
        重写基类方法，在调用子类 _generate_signals_impl() 前验证指标
        
        注意：AI 策略直接重写此方法，不需要使用 simple_strategy 装饰器
        """
        if stock_pool is None or stock_pool.empty:
            return {}
        
        # 1. 预筛选
        candidate_df = self._pre_screen_stocks(stock_pool)
        if candidate_df.empty:
            return {}
        
        # 2. 验证指标是否已计算
        if not self._validate_indicators(candidate_df):
            print(
                f"[AIStrategyBase] 警告: 指标验证失败，"
                f"请确保后端已根据 get_required_indicators() 计算指标"
            )
            # 继续执行，但可能缺少某些指标
        
        # 3. 调用子类的 _generate_signals_impl() 方法（子类必须重写）
        try:
            signals = self._generate_signals_impl(current_date, candidate_df)
            
            # 4. 更新持仓状态
            self._update_holding_state(current_date, signals)
            
            # 5. 规范化信号格式（转换为基类期望的格式）
            normalized_signals = {}
            for code, signal in signals.items():
                if isinstance(signal, str):
                    normalized_signals[code] = signal
                elif isinstance(signal, (tuple, list)) and len(signal) >= 1:
                    normalized_signals[code] = signal[0]
                elif isinstance(signal, dict):
                    normalized_signals[code] = signal.get("action", "hold")
                else:
                    normalized_signals[code] = "hold"
            
            # 6. 缓存富信号（用于后续分析）
            self.last_rich_signals = {}
            for code, signal in signals.items():
                if isinstance(signal, dict):
                    self.last_rich_signals[code] = signal
                else:
                    self.last_rich_signals[code] = {
                        "action": normalized_signals.get(code, "hold"),
                        "weight": 0.0,
                        "score": None,
                        "params": {},
                    }
            
            return normalized_signals
            
        except NotImplementedError:
            # 如果子类没有实现 _generate_signals_impl，抛出更清晰的错误
            raise NotImplementedError(
                "AI 策略子类必须实现 _generate_signals_impl() 方法，"
                "该方法接收 (current_date, stock_pool) 参数，"
                "返回 {stock_code: signal} 字典"
            )
        except Exception as e:
            print(f"[AIStrategyBase] 生成信号失败: {e}")
            import traceback
            traceback.print_exc()
            return {}
