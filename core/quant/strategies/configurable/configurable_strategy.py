"""
配置化策略类 - 从配置生成策略实例

此类将配置转换为与现有策略框架兼容的接口，
使得配置化策略可以无缝集成到现有回测系统中。

使用示例：
    from core.quant.strategies.configurable import StrategyConfigLoader, ConfigurableStrategy
    
    # 加载配置
    loader = StrategyConfigLoader()
    config = loader.load("dual_ma_strategy.yaml")
    
    # 创建策略实例
    strategy = ConfigurableStrategy(config)
    
    # 运行回测（与现有策略使用方式相同）
    engine = UnifiedBacktestEngine(data_query)
    for event in engine.run_backtest(start_date, end_date, strategy):
        print(event)
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, List
from datetime import datetime

from .strategy_config import StrategyConfig, ActionType, FilterConfig
from .builtin_indicators import calculate_indicators, get_indicator
from .rule_engine import RuleEngine


class ConfigurableStrategy:
    """
    配置化策略
    
    将 YAML/JSON 配置转换为可执行的策略逻辑
    
    特性：
    1. 兼容现有策略框架接口 (generate_signals, set_runtime_context)
    2. 支持向量化信号生成
    3. 自动计算所需指标
    4. 灵活的风控配置
    """
    
    def __init__(
        self,
        config: StrategyConfig,
        parameters: Optional[Dict[str, Any]] = None
    ):
        """
        初始化配置化策略
        
        参数：
            config: 策略配置对象
            parameters: 运行时参数（覆盖默认参数）
        """
        self.config = config
        self.strategy_id = config.strategy_id
        self.strategy_name = config.name
        
        # 合并参数（运行时参数覆盖默认参数）
        self.parameters = config.get_default_params()
        if parameters:
            self.parameters.update(parameters)
        
        # 验证参数
        errors = config.validate_params(self.parameters)
        if errors:
            raise ValueError(f"参数验证失败: {', '.join(errors)}")
        
        # 运行时上下文
        self.current_date: Optional[str] = None
        self.current_portfolio: Dict[str, int] = {}
        self.current_cash: float = 0.0
        
        # 持仓状态管理
        self.holding_state: Dict[str, Dict[str, Any]] = {}
        
        # 缓存
        self._indicator_cache: Dict[str, pd.DataFrame] = {}
        self._rule_engine: Optional[RuleEngine] = None
        
        # 风控配置
        self.risk_config = config.risk_management
        if self.risk_config:
            self.max_positions = self.risk_config.max_positions
            self.position_ratio = self.risk_config.position_ratio or 0.1
            self.stop_loss = self.risk_config.stop_loss
            self.max_holding_days = self.risk_config.max_holding_days
        else:
            self.max_positions = None
            self.position_ratio = 0.1
            self.stop_loss = None
            self.max_holding_days = None
    
    def set_runtime_context(
        self,
        current_date: str,
        portfolio: Dict[str, int],
        cash: float
    ):
        """
        设置运行时上下文（由回测引擎调用）
        
        参数：
            current_date: 当前日期
            portfolio: 当前持仓
            cash: 当前现金
        """
        self.current_date = current_date
        self.current_portfolio = portfolio.copy() if portfolio else {}
        self.current_cash = float(cash) if cash is not None else 0.0
        
        # 更新持仓状态
        self._update_holding_state()
    
    def _update_holding_state(self):
        """更新持仓状态（持仓天数等）"""
        # 增加持仓天数
        for code in self.current_portfolio:
            if code not in self.holding_state:
                self.holding_state[code] = {
                    'entry_date': self.current_date,
                    'holding_days': 0,
                    'buy_price': 0.0
                }
            self.holding_state[code]['holding_days'] += 1
        
        # 清理已卖出的持仓
        for code in list(self.holding_state.keys()):
            if code not in self.current_portfolio:
                del self.holding_state[code]
    
    def generate_signals(
        self,
        current_date: str,
        stock_pool_today: pd.DataFrame,
        data_query: Any
    ) -> Dict[str, Any]:
        """
        生成交易信号（标准策略接口）
        
        参数：
            current_date: 当前日期
            stock_pool_today: 当日股票池
            data_query: 数据查询对象
        
        返回：
            Dict[str, Any]: 信号字典 {stock_code: signal}
        """
        if stock_pool_today is None or stock_pool_today.empty:
            return {}
        
        # 1. 预筛选股票
        candidate_df = self._apply_filters(stock_pool_today)
        if candidate_df.empty:
            return {}
        
        # 2. 计算指标
        data_with_indicators = self._calculate_indicators(candidate_df, current_date, data_query)
        
        # 3. 评估规则
        signals = self._evaluate_rules(data_with_indicators)
        
        # 4. 应用风控
        signals = self._apply_risk_management(signals, data_with_indicators)
        
        return signals
    
    def _apply_filters(self, stock_pool: pd.DataFrame) -> pd.DataFrame:
        """
        应用股票过滤条件
        
        参数：
            stock_pool: 原始股票池
        
        返回：
            pd.DataFrame: 过滤后的股票池
        """
        if not self.config.filters:
            return stock_pool
        
        filters = self.config.filters
        df = stock_pool.copy()
        mask = pd.Series(True, index=df.index)
        
        # 市值过滤
        if filters.market_cap_min is not None:
            mask &= df.get('total_mv', 0) >= filters.market_cap_min
        if filters.market_cap_max is not None:
            mask &= df.get('total_mv', 0) <= filters.market_cap_max
        
        # 价格过滤
        if filters.price_min is not None:
            mask &= df.get('close', 0) >= filters.price_min
        if filters.price_max is not None:
            mask &= df.get('close', 0) <= filters.price_max
        
        # 成交量过滤
        if filters.volume_min is not None:
            mask &= df.get('volume', 0) >= filters.volume_min
        
        # 排除ST
        if filters.exclude_st and 'is_st' in df.columns:
            mask &= df['is_st'] == 0
        
        # 排除科创板
        if filters.exclude_kc and 'is_kc' in df.columns:
            mask &= df['is_kc'] == 0
        
        # 排除创业板
        if filters.exclude_cy and 'is_cy' in df.columns:
            mask &= df['is_cy'] == 0
        
        # 上市天数过滤
        if filters.min_list_days is not None and 'list_days' in df.columns:
            mask &= df['list_days'] >= filters.min_list_days
        
        return df[mask]
    
    def _calculate_indicators(
        self,
        stock_pool: pd.DataFrame,
        current_date: str,
        data_query: Any
    ) -> pd.DataFrame:
        """
        计算策略所需的指标
        
        参数：
            stock_pool: 股票池
            current_date: 当前日期
            data_query: 数据查询对象
        
        返回：
            pd.DataFrame: 添加了指标列的DataFrame
        """
        if not self.config.indicators:
            return stock_pool
        
        # 获取历史数据
        codes = stock_pool['stock_code'].tolist()
        
        # 计算所需的历史天数
        max_window = 60  # 默认
        for ind_config in self.config.indicators:
            params = ind_config.resolve_params(self.parameters)
            window = params.get('window', 20)
            if isinstance(window, (int, float)):
                max_window = max(max_window, int(window) * 2)
        
        # 计算开始日期
        from datetime import datetime, timedelta
        current = datetime.strptime(current_date, "%Y-%m-%d")
        start = (current - timedelta(days=max_window + 30)).strftime("%Y-%m-%d")
        
        try:
            # 批量获取历史数据
            hist_data = data_query.get_batch_stock_history(
                codes,
                start,
                current_date,
                columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume"]
            )
            
            if hist_data.empty:
                return stock_pool
            
            # 准备指标配置
            indicator_configs = []
            for ind_config in self.config.indicators:
                indicator_configs.append({
                    'name': ind_config.name,
                    'type': ind_config.type.value if hasattr(ind_config.type, 'value') else ind_config.type,
                    'params': ind_config.resolve_params(self.parameters)
                })
            
            # 计算指标
            data_with_indicators = calculate_indicators(hist_data, indicator_configs)
            
            # 只保留当前日期的数据
            current_data = data_with_indicators[data_with_indicators['trade_date'] == current_date]
            
            # 合并回原始股票池
            result = stock_pool.merge(
                current_data,
                on='stock_code',
                how='left',
                suffixes=('', '_ind')
            )
            
            return result
            
        except Exception as e:
            import warnings
            warnings.warn(f"计算指标失败: {e}")
            return stock_pool
    
    def _evaluate_rules(self, data: pd.DataFrame) -> Dict[str, Any]:
        """
        评估交易规则
        
        参数：
            data: 包含指标的数据
        
        返回：
            Dict[str, Any]: 信号字典
        """
        if not self.config.rules:
            return {}
        
        signals = {}
        
        # 按优先级排序规则
        sorted_rules = sorted(self.config.rules, key=lambda r: r.priority, reverse=True)
        
        for rule in sorted_rules:
            action = rule.action.value if hasattr(rule.action, 'value') else rule.action
            condition = rule.condition.expr if hasattr(rule.condition, 'expr') else str(rule.condition)
            
            try:
                # 创建规则引擎
                if self._rule_engine is None or self._rule_engine.data is not data:
                    self._rule_engine = RuleEngine(data)
                
                # 评估条件
                mask = self._rule_engine.evaluate(condition)
                
                # 获取满足条件的股票
                matched_codes = data.loc[mask, 'stock_code'].tolist()
                
                # 限制每日最大选股数
                max_stocks = rule.max_stocks_per_day
                if max_stocks is not None and len(matched_codes) > max_stocks:
                    # 这里可以添加排序逻辑（如按市值、成交量等）
                    matched_codes = matched_codes[:max_stocks]
                
                # 生成信号
                for code in matched_codes:
                    if code not in signals:  # 避免覆盖高优先级规则的信号
                        position_ratio = rule.position_ratio or self.position_ratio
                        signals[code] = {
                            'action': action,
                            'position_ratio': position_ratio,
                            'rule': rule,
                            'indicators': {}
                        }
                        
                        # 提取指标值
                        row = data[data['stock_code'] == code]
                        if not row.empty:
                            for ind_config in self.config.indicators:
                                if ind_config.name in row.columns:
                                    signals[code]['indicators'][ind_config.name] = row[ind_config.name].iloc[0]
            
            except Exception as e:
                import warnings
                warnings.warn(f"评估规则失败 '{condition}': {e}")
                continue
        
        return signals
    
    def _apply_risk_management(
        self,
        signals: Dict[str, Any],
        data: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        应用风控规则
        
        参数：
            signals: 原始信号
            data: 股票数据
        
        返回：
            Dict[str, Any]: 过滤后的信号
        """
        if not self.risk_config:
            return signals
        
        filtered_signals = {}
        
        for code, signal in signals.items():
            action = signal.get('action')
            
            # 买入信号风控
            if action in ('buy', 'enter'):
                # 检查最大持仓数
                if self.max_positions is not None:
                    current_positions = len(self.current_portfolio)
                    if current_positions >= self.max_positions:
                        continue
                
                # 检查是否已持仓
                if code in self.current_portfolio:
                    continue
                
                filtered_signals[code] = signal
            
            # 卖出信号风控
            elif action in ('sell', 'exit'):
                # 检查止损
                if self.stop_loss is not None and code in self.holding_state:
                    # 这里需要实际价格数据来计算止损
                    pass
                
                # 检查最大持有天数
                if self.max_holding_days is not None and code in self.holding_state:
                    holding_days = self.holding_state[code].get('holding_days', 0)
                    if holding_days >= self.max_holding_days:
                        signal['reason'] = 'max_holding_days'
                
                filtered_signals[code] = signal
            
            else:
                filtered_signals[code] = signal
        
        return filtered_signals
    
    def get_required_indicators(self) -> List[Dict[str, Any]]:
        """
        获取策略所需的指标列表
        
        返回：
            List[Dict[str, Any]]: 指标配置列表
        """
        return [
            {
                'name': ind.name,
                'type': ind.type.value if hasattr(ind.type, 'value') else ind.type,
                'params': ind.resolve_params(self.parameters)
            }
            for ind in self.config.indicators
        ]
    
    def get_param_spec(self) -> List[Dict[str, Any]]:
        """
        获取参数规范（用于前端表单生成）
        
        返回：
            List[Dict[str, Any]]: 参数规范列表
        """
        return [
            {
                'name': p.name,
                'type': p.type.value if hasattr(p.type, 'value') else p.type,
                'default': p.default,
                'min': p.min,
                'max': p.max,
                'step': p.step,
                'label': p.label or p.name,
                'description': p.description,
                'unit': p.unit,
            }
            for p in self.config.parameters
        ]
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return self.config.to_dict()


# ==================== 便捷函数 ====================

def create_strategy_from_config(
    config_path: str,
    parameters: Optional[Dict[str, Any]] = None
) -> ConfigurableStrategy:
    """
    从配置文件创建策略实例
    
    示例：
        strategy = create_strategy_from_config("configs/dual_ma.yaml")
    """
    from .config_loader import StrategyConfigLoader
    
    loader = StrategyConfigLoader()
    config = loader.load(config_path)
    
    return ConfigurableStrategy(config, parameters)
