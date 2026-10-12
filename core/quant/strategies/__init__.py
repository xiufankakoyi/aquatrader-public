"""
策略模块

包含各种量化交易策略的实现。策略类会由 StrategyFactory 自动扫描和注册。

注意：本模块使用延迟加载机制，策略类只在真正需要时才被导入，
以优化启动性能。无需在此文件中预先导入所有策略。
"""

# 只导入策略框架和工厂（必需的核心组件）
from . import strategy_framework
from . import strategy_factory

# 策略类会被 StrategyFactory 动态扫描和加载（通过 pkgutil.iter_modules）
# 无需在此处显式导入策略文件
