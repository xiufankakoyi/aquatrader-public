"""
高性能向量化计算模块
"""
from .vectorized_ops import (
    calc_ma_vectorized,
    calc_cross_over,
    calc_cross_under,
)

__all__ = [
    'calc_ma_vectorized',
    'calc_cross_over',
    'calc_cross_under',
]

