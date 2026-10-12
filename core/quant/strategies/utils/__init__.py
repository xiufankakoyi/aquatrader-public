"""
策略工具库 - 因子加载与计算

提供统一的因子访问接口，优先使用数据库预计算结果，按需动态计算
"""

from .factor_loader import FactorLoader
from .factor_compute import FactorCompute

__all__ = ['FactorLoader', 'FactorCompute']
