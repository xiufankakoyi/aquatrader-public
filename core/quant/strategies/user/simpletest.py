# core/strategies/user/my_strategy.py
from core.quant.strategies.strategy_framework import StrategyBase

class MyStrategy(StrategyBase):
    """
    示例策略：使用数据库预计算指标
    
    买入条件：
    - 收盘价 > MA20（趋势向上）
    - 量比 > 2（放量）
    - 非ST、非涨停
    
    卖出条件：
    - 收盘价 < MA5（短期走弱）
    """
    strategy_name = "我的策略"
    
    def __init__(self, name=None):
        super().__init__(name)
    
    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        策略入口：每天调用一次
        
        参数:
            current_date: 当前日期 '2024-01-15'
            stock_pool_today: DataFrame，包含当日所有股票数据
            data_query: 数据查询对象（用于获取历史数据）
        
        返回:
            Dict[str, str]: {股票代码: 'buy'/'sell'/'hold'}
        """
        signals = {}
        
        # stock_pool_today 已经包含所有预计算指标！
        # 可用字段：open, high, low, close, ma5, ma10, ma20, volume_ratio, 
        #          is_st, is_limit_up, total_mv 等
        
        for _, row in stock_pool_today.iterrows():
            code = row['stock_code']
            
            # 买入条件（直接使用数据库字段）
            if (row['close'] > row['ma20'] and      # 收盘价 > MA20
                row['volume_ratio'] > 2.0 and       # 量比 > 2
                not row['is_st'] and                # 非ST
                not row['is_limit_up']):            # 非涨停
                signals[code] = 'buy'
            
            # 卖出条件
            elif row['close'] < row['ma5']:         # 收盘价跌破MA5
                signals[code] = 'sell'
            
            else:
                signals[code] = 'hold'
        
        return signals