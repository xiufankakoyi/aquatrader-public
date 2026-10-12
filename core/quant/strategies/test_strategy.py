from core.quant.strategies.strategy_framework import StrategyBase

class TestStrategy(StrategyBase):
    """测试策略类"""
    strategy_id = "test_strategy"
    strategy_name = "TestStrategy"
    
    def __init__(self):
        super().__init__()
        self.description = "这是一个测试策略"
    
    def run(self, stock_code, date_range=None):
        # 简单的测试策略实现
        return {"result": "success"}

# 确保在模块被导入时就能被发现
__all__ = ["TestStrategy"]