# utils/strategy_runner.py
"""
策略运行器 - 实现回测引擎的每日循环逻辑（on_bar）

核心功能：
1. 自动更新持仓状态（持仓天数等）
2. 准备当日数据（包含已计算的指标）
3. 执行策略逻辑
4. 执行交易并更新持仓列表

设计理念：
- 将状态管理从策略中分离出来，让 AI 策略专注于信号生成
- 自动处理容易出错的状态逻辑（如持仓天数计算）
- 提供清晰的接口，便于策略访问持仓信息
"""

import pandas as pd
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from config.logger import get_logger


class StrategyRunner:
    """
    策略运行器 - 封装回测引擎的每日循环逻辑
    
    职责：
    1. 自动更新持仓状态（持仓天数、持仓成本等）
    2. 准备当日数据快照（包含已计算的指标）
    3. 执行策略逻辑
    4. 执行交易并更新持仓状态
    """
    
    def __init__(self, strategy, data_engine, logger=None):
        """
        初始化策略运行器
        
        参数:
            strategy: 策略实例
            data_engine: 数据引擎（如 OptimizedStockDataQuery）
            logger: 日志记录器（可选）
        """
        self.strategy = strategy
        self.data_engine = data_engine
        self.logger = logger or get_logger(__name__)
        
        # 确保策略有持仓状态字典
        if not hasattr(strategy, 'holding_state'):
            strategy.holding_state = {}
    
    def on_bar(
        self,
        current_date: str,
        portfolio: Dict[str, Any],
        cash: float
    ) -> Dict[str, str]:
        """
        每日循环的主入口（on_bar）
        
        流程：
        1. 自动更新持仓状态（持仓天数等）
        2. 准备当日数据（切片，包含已计算的指标）
        3. 执行策略逻辑
        4. 返回交易信号（交易执行由引擎负责）
        
        参数:
            current_date: 当前日期（'YYYY-MM-DD'）
            portfolio: 当前持仓 {stock_code: {'shares': ..., 'entry_price': ...}}
            cash: 当前现金
        
        返回:
            signals: 交易信号字典 {stock_code: 'buy'/'sell'/'hold'}
        """
        # 1. 自动更新持仓状态（帮 AI 做的脏活累活）
        self._update_holding_state(current_date, portfolio)
        
        # 2. 准备当日数据（切片）
        # 此时数据里已经有了 AI 之前点的"菜"（RSI_14 等指标）
        daily_data = self._get_daily_snapshot(current_date)
        
        # 3. 设置策略运行时上下文（包含持仓状态）
        self.strategy.set_runtime_context(
            current_date=current_date,
            portfolio=portfolio,
            cash=cash
        )
        
        # 4. 执行 AI 逻辑
        try:
            signals = self.strategy.generate_signals(
                current_date=current_date,
                stock_pool_today=daily_data,
                data_query=self.data_engine
            )
        except Exception as e:
            self.logger.error(f"策略信号生成失败 ({current_date}): {e}", exc_info=True)
            signals = {}
        
        return signals
    
    def _update_holding_state(self, current_date: str, portfolio: Dict[str, Any]) -> None:
        """
        自动更新持仓状态（持仓天数等）
        
        这是 AI 容易写错的状态逻辑，由引擎自动处理：
        - 持仓天数自动递增
        - 新买入的股票自动添加到持仓状态
        - 已卖出的股票自动从持仓状态移除
        """
        current_dt = pd.to_datetime(current_date)
        
        # 更新现有持仓的持仓天数
        for stock_code in list(self.strategy.holding_state.keys()):
            state = self.strategy.holding_state[stock_code]
            
            # 检查是否还在持仓中
            if stock_code in portfolio and portfolio[stock_code].get('shares', 0) > 0:
                # 还在持仓中，增加持仓天数
                if 'days_held' not in state:
                    state['days_held'] = 0
                state['days_held'] += 1
                
                # 更新最后更新日期
                state['last_update_date'] = current_date
            else:
                # 已卖出，从持仓状态中移除
                # 但保留历史记录（可选，用于分析）
                # 这里直接删除，如果需要历史记录，可以移到另一个字典
                del self.strategy.holding_state[stock_code]
        
        # 添加新买入的股票到持仓状态
        for stock_code, position in portfolio.items():
            if position.get('shares', 0) > 0:
                if stock_code not in self.strategy.holding_state:
                    # 新买入的股票，初始化持仓状态
                    entry_date = position.get('entry_date', current_date)
                    entry_price = position.get('entry_price', 0.0)
                    
                    self.strategy.holding_state[stock_code] = {
                        'entry_date': entry_date,
                        'entry_price': entry_price,
                        'days_held': 0,  # 当天买入，持仓天数为 0
                        'last_update_date': current_date,
                        'shares': position.get('shares', 0)
                    }
                else:
                    # 已存在的持仓，更新股数（可能是加仓）
                    self.strategy.holding_state[stock_code]['shares'] = position.get('shares', 0)
    
    def _get_daily_snapshot(self, current_date: str) -> Optional[pd.DataFrame]:
        """
        获取当日数据快照（切片）
        
        此时数据里已经有了之前计算的指标（通过 prepare_strategy_execution 注入）
        
        参数:
            current_date: 当前日期
        
        返回:
            daily_data: 当日股票池 DataFrame（包含所有指标列）
        """
        try:
            # 检查策略是否需要当日股票池
            needs_today_pool = getattr(self.strategy, 'needs_today_pool', False)
            
            if not needs_today_pool:
                # 策略不需要当日股票池（如 JQVolumeStrategypro），返回 None
                # 策略内部会自己获取昨日数据，避免无用的数据获取开销
                return None
            
            # 尝试从预加载数据获取（性能最优）
            if hasattr(self.data_engine, 'get_stock_pool_from_preloaded'):
                daily_data = self.data_engine.get_stock_pool_from_preloaded(current_date)
                if daily_data is not None and not daily_data.empty:
                    return daily_data
            
            # 回退到常规查询
            daily_data = self.data_engine.get_stock_pool(current_date)
            return daily_data
            
        except Exception as e:
            self.logger.warning(f"获取当日数据快照失败 ({current_date}): {e}")
            return None
    
    def get_holding_state(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取指定股票的持仓状态
        
        参数:
            stock_code: 股票代码
        
        返回:
            state: 持仓状态字典，包含：
                - entry_date: 买入日期
                - entry_price: 买入价格
                - days_held: 持仓天数
                - last_update_date: 最后更新日期
                - shares: 持仓股数
        """
        return self.strategy.holding_state.get(stock_code)
    
    def get_all_holding_states(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有持仓状态
        
        返回:
            holding_states: {stock_code: state_dict}
        """
        return self.strategy.holding_state.copy()
    
    def execute_orders(
        self,
        signals: Dict[str, str],
        current_date: str,
        portfolio: Dict[str, Any],
        cash: float,
        price_matrix: Optional[Any] = None,
        code_to_idx: Optional[Dict[str, int]] = None
    ) -> Dict[str, Any]:
        """
        执行交易订单（由引擎调用）
        
        这个方法由回测引擎调用，用于执行交易并更新持仓。
        策略运行器主要负责准备数据和状态管理，交易执行由引擎负责。
        
        参数:
            signals: 交易信号 {stock_code: 'buy'/'sell'/'hold'}
            current_date: 当前日期
            portfolio: 当前持仓
            cash: 当前现金
            price_matrix: 价格矩阵（可选，用于向量化执行）
            code_to_idx: 代码到索引的映射（可选）
        
        返回:
            execution_result: 执行结果字典（由引擎填充）
        """
        # 这个方法主要是占位，实际交易执行由引擎负责
        # 但可以在这里做一些预处理或验证
        return {
            'signals': signals,
            'date': current_date,
            'portfolio_before': portfolio.copy(),
            'cash_before': cash
        }
    
    def update_holding_state_after_trade(
        self,
        stock_code: str,
        action: str,
        shares: int,
        price: float,
        current_date: str
    ) -> None:
        """
        交易后更新持仓状态（由引擎调用）
        
        参数:
            stock_code: 股票代码
            action: 'buy' 或 'sell'
            shares: 交易股数
            price: 交易价格
            current_date: 交易日期
        """
        if action == 'buy':
            # 买入：添加到持仓状态或更新
            if stock_code not in self.strategy.holding_state:
                self.strategy.holding_state[stock_code] = {
                    'entry_date': current_date,
                    'entry_price': price,
                    'days_held': 0,
                    'last_update_date': current_date,
                    'shares': shares
                }
            else:
                # 加仓：更新持仓（可能需要计算平均成本）
                state = self.strategy.holding_state[stock_code]
                old_shares = state.get('shares', 0)
                old_price = state.get('entry_price', 0)
                
                # 计算新的平均成本
                total_cost = old_shares * old_price + shares * price
                new_shares = old_shares + shares
                new_avg_price = total_cost / new_shares if new_shares > 0 else price
                
                state['entry_price'] = new_avg_price
                state['shares'] = new_shares
                state['last_update_date'] = current_date
                # 注意：加仓时，持仓天数不变（从首次买入开始计算）
        
        elif action == 'sell':
            # 卖出：减少持仓或移除
            if stock_code in self.strategy.holding_state:
                state = self.strategy.holding_state[stock_code]
                old_shares = state.get('shares', 0)
                new_shares = old_shares - shares
                
                if new_shares <= 0:
                    # 全部卖出，移除持仓状态
                    del self.strategy.holding_state[stock_code]
                else:
                    # 部分卖出，更新股数
                    state['shares'] = new_shares
                    state['last_update_date'] = current_date

