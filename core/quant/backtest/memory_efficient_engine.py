"""
内存受限的高性能回测引擎 (<= 2GB)

核心设计：
1. 流式处理 - 不构建完整三维矩阵
2. 日期分片 - 5天为一片逐片处理
3. 内存映射 - 历史数据永不进 Python 堆
4. 就地计算 - 信号生成复用内存映射缓冲区

内存预算：
- L1 热缓存: 256MB (最近5天)
- L2 内存映射: 512MB (按需加载)
- L3 计算空间: 512MB (信号/矩阵运算)
- 系统开销: 256MB
- 预留缓冲: 464MB
"""

import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Generator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
import time
import logging
import json
import hashlib

# 可选依赖
try:
    import polars as pl
    POLARS_AVAILABLE = True
except ImportError:
    POLARS_AVAILABLE = False
    pl = None

try:
    from numba import njit, prange
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    njit = lambda *args, **kwargs: lambda f: f
    prange = range

logger = logging.getLogger(__name__)

# =============================================================================
# 内存常量配置
# =============================================================================
MAX_MEMORY_MB = 2048          # 最大内存限制
L1_CACHE_DAYS = 5             #