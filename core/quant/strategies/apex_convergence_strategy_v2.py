"""
收敛三角形策略 V2 - 基于因子层的简化实现

核心变化：
- 不写任何形态识别算法
- 只配置需要的因子
- 只写交易逻辑（买入/卖出条件）

策略开发者只需要：
1. 声明需要的因子
2. 实现 generate_signals 方法，使用预计算因子做决策
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np

from core.quant.strategies.strategy_framework import StrategyBase, simple_strategy


@dataclass(frozen=True)
class ApexConvergenceConfig:
    """策略配置 - 只包含交易参数，不包含算法参数"""
    # 买入阈值
    min_signal_strength: float = 0.3  # 最小信号强度
    
    # 仓位控制
    position_ratio: float = 0.2
    
    # 止盈止损
    take_profit_pct: float = 0.05  # 5%止盈
    stop_loss_pct: float = -0.05   # 5%止损
    
    # 持仓时间限制
    max_hold_days: int = 20  # 最长持仓天数


class ApexConvergenceStrategyV2(StrategyBase):
    """
    收敛三角形策略 V2
    
    使用预计算的形态因子，不写任何算法代码
    """
    
    strategy_id = "apex_convergence_v2"
    strategy_name = "收敛三角形策略V2(因子化)"
    
    # ==========================================
    # 策略开发者只需要配置这些
    # ==========================================
    
    # 声明需要的因子
    REQUIRED_FACTORS = [
        'apex_convergence',  # 收敛三角形信号强度 (0-1)
        'MA20',              # 20日均线
        'VOLATILITY_20',     # 20日波动率
    ]
    
    # 声明需要的基础数据字段
    REQUIRED_FIELDS = ['close', 'volume']
    
    def __init__(self, config: Optional[ApexConvergenceConfig] = None):
        super().__init__(name=self.strategy_name)
        self.config = config or ApexConvergenceConfig()
        self.position_ratio = self.config.position_ratio
    
    # ==========================================
    # 策略核心逻辑 - 只写交易条件
    # ==========================================
    
    @simple_strategy(required_days=60)
    def generate_signals(
        self,
        stock_code: str,
        current_stock_data: pd.Series,
        stock_history: pd.DataFrame,
    ) -> Dict[str, Any] | str:
        """
        交易决策 - 使用预计算因子
        
        参数说明：
        - current_stock_data: 当日数据，包含所有因子
          {
            'close': 100.0,
            'volume': 10000,
            'apex_convergence': 0.8,    # 预计算因子
            'MA20': 98.5,               # 预计算因子
            'VOLATILITY_20': 15.2,      # 预计算因子
            ...
          }
        - stock_history: 历史数据（可选，用于需要历史序列的策略）
        """
        # 获取预计算因子
        apex_signal = current_stock_data.get('apex_convergence', 0.0)
        ma20 = current_stock_data.get('MA20', 0.0)
        volatility = current_stock_data.get('VOLATILITY_20', 0.0)
        current_price = current_stock_data.get('close', 0.0)
        
        # 检查是否有持仓
        portfolio = getattr(self, "current_portfolio", {}) or {}
        has_position = stock_code in portfolio
        
        # ================= 持仓阶段：卖出逻辑 =================
        if has_position:
            pos = portfolio.get(stock_code, {}) or {}
            entry_price = float(pos.get("entry_price") or current_price)
            hold_days = int(pos.get("hold_days", 0))
            
            profit_pct = (current_price / entry_price - 1.0) if entry_price > 0 else 0.0
            
            # 止盈
            if profit_pct >= self.config.take_profit_pct:
                return {
                    "action": "sell",
                    "reason": "take_profit",
                    "profit_pct": profit_pct
                }
            
            # 止损
            if profit_pct <= self.config.stop_loss_pct:
                return {
                    "action": "sell", 
                    "reason": "stop_loss",
                    "profit_pct": profit_pct
                }
            
            # 时间止损
            if hold_days >= self.config.max_hold_days:
                return {
                    "action": "sell",
                    "reason": "time_stop",
                    "hold_days": hold_days
                }
            
            return "hold"
        
        # ================= 空仓阶段：买入逻辑 =================
        
        # 基本条件检查
        if apex_signal < self.config.min_signal_strength:
            return "hold"
        
        if current_price <= 0 or ma20 <= 0:
            return "hold"
        
        # 趋势确认：价格在MA20上方（上升趋势）
        trend_ok = current_price > ma20 * 0.98  # 允许2%的偏离
        
        # 波动率检查：排除过于平静的股票
        volatility_ok = volatility > 5.0  # 波动率大于5%
        
        if trend_ok and volatility_ok:
            # 买入信号
            return {
                "action": "buy",
                "weight": min(apex_signal * self.position_ratio, self.position_ratio),
                "score": apex_signal * 100,  # 信号强度作为评分
                "metadata": {
                    "apex_signal": apex_signal,
                    "ma20": ma20,
                    "volatility": volatility
                }
            }
        
        return "hold"


# ==========================================
# 策略配置示例
# ==========================================

DEFAULT_CONFIG = ApexConvergenceConfig(
    min_signal_strength=0.5,  # 只选强信号
    position_ratio=0.15,      # 单票15%仓位
    take_profit_pct=0.08,     # 8%止盈
    stop_loss_pct=-0.04,      # 4%止损
    max_hold_days=15
)


# ==========================================
# 使用示例
# ==========================================

def example_usage():
    """
    策略使用示例
    """
    from core.quant.strategies.utils.factor_precompute import FactorPrecomputeEngine
    from data_svc.database.polars_data_loader_v6 import get_polars_loader_v6
    
    # 1. 初始化因子引擎
    factor_engine = FactorPrecomputeEngine()
    
    # 2. 加载数据
    loader = get_polars_loader_v6()
    data = loader.load_with_factors(
        start_date="2024-01-01",
        end_date="2024-06-30",
        base_fields=['close', 'volume'],
        factor_names=['apex_convergence', 'MA20', 'VOLATILITY_20']
    )
    
    # 3. 创建策略实例
    strategy = ApexConvergenceStrategyV2(config=DEFAULT_CONFIG)
    
    # 4. 运行回测
    # ... (回测引擎会自动传入因子数据)
    
    print("策略示例完成")


if __name__ == "__main__":
    example_usage()