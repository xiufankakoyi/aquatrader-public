"""
core/strategies/trend_follow_strategy.py 趋势跟随策略测试

测试内容：
1. TrendFollowConfig 配置
2. TrendFollowStrategy 初始化
3. 辅助函数
"""

import pytest
import numpy as np
import pandas as pd
from unittest.mock import Mock, patch, MagicMock


class TestTrendFollowConfig:
    """趋势跟随配置测试"""
    
    def test_config_default_values(self):
        """测试默认配置值"""
        from core.quant.strategies.trend_follow_strategy import TrendFollowConfig
        
        config = TrendFollowConfig()
        
        assert config.market_cap_min == 50 * 10_000
        assert config.market_cap_max == 500 * 10_000
        assert config.volume_ratio_threshold == 1.2
        assert config.ma_short_days == 5
        assert config.ma_long_days == 10
    
    def test_config_custom_values(self):
        """测试自定义配置值"""
        from core.quant.strategies.trend_follow_strategy import TrendFollowConfig
        
        config = TrendFollowConfig(
            market_cap_min=100 * 10_000,
            market_cap_max=1000 * 10_000,
            volume_ratio_threshold=1.5
        )
        
        assert config.market_cap_min == 100 * 10_000
        assert config.market_cap_max == 1000 * 10_000
        assert config.volume_ratio_threshold == 1.5
    
    def test_config_position_ratio(self):
        """测试仓位比例配置"""
        from core.quant.strategies.trend_follow_strategy import TrendFollowConfig
        
        config = TrendFollowConfig()
        
        assert config.position_ratio == 0.25
        assert config.max_stocks_per_day == 5


class TestTrendFollowStrategyInit:
    """趋势跟随策略初始化测试"""
    
    def test_strategy_init_default(self):
        """测试默认初始化"""
        from core.quant.strategies.trend_follow_strategy import TrendFollowStrategy
        
        strategy = TrendFollowStrategy()
        
        assert strategy.strategy_id == "trend_follow_main_wave"
        assert strategy.strategy_name == "主升浪趋势跟随策略"
    
    def test_strategy_init_with_config(self):
        """测试带配置初始化"""
        from core.quant.strategies.trend_follow_strategy import TrendFollowStrategy, TrendFollowConfig
        
        config = TrendFollowConfig(
            market_cap_min=100 * 10_000,
            position_ratio=0.3
        )
        strategy = TrendFollowStrategy(config=config)
        
        assert strategy.config.market_cap_min == 100 * 10_000
        assert strategy.config.position_ratio == 0.3
    
    def test_strategy_required_days(self):
        """测试所需天数"""
        from core.quant.strategies.trend_follow_strategy import TrendFollowStrategy
        
        strategy = TrendFollowStrategy()
        
        assert strategy.required_days >= 60


class TestHelperFunctions:
    """辅助函数测试"""
    
    def test_is_empty_none(self):
        """测试 None 为空"""
        from core.quant.strategies.trend_follow_strategy import _is_empty
        
        assert _is_empty(None) is True
    
    def test_is_empty_empty_dataframe(self):
        """测试空 DataFrame"""
        from core.quant.strategies.trend_follow_strategy import _is_empty
        
        df = pd.DataFrame()
        assert _is_empty(df) is True
    
    def test_is_empty_non_empty_dataframe(self):
        """测试非空 DataFrame"""
        from core.quant.strategies.trend_follow_strategy import _is_empty
        
        df = pd.DataFrame({'a': [1, 2, 3]})
        assert _is_empty(df) is False
    
    def test_is_empty_empty_array(self):
        """测试空数组"""
        from core.quant.strategies.trend_follow_strategy import _is_empty
        
        arr = np.array([])
        assert _is_empty(arr) is True
    
    def test_is_empty_non_empty_array(self):
        """测试非空数组"""
        from core.quant.strategies.trend_follow_strategy import _is_empty
        
        arr = np.array([1, 2, 3])
        assert _is_empty(arr) is False
    
    def test_ensure_dataframe_none(self):
        """测试 None 转换"""
        from core.quant.strategies.trend_follow_strategy import _ensure_dataframe
        
        result = _ensure_dataframe(None)
        
        assert result is None
    
    def test_ensure_dataframe_array(self):
        """测试数组转换"""
        from core.quant.strategies.trend_follow_strategy import _ensure_dataframe
        
        arr = np.array([1, 2, 3])
        result = _ensure_dataframe(arr)
        
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
    
    def test_ensure_dataframe_2d_array(self):
        """测试二维数组转换"""
        from core.quant.strategies.trend_follow_strategy import _ensure_dataframe
        
        arr = np.array([[1, 2], [3, 4]])
        result = _ensure_dataframe(arr)
        
        assert isinstance(result, pd.DataFrame)
        assert result.shape == (2, 2)
