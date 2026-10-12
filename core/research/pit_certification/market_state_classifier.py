"""P3.9 市场状态分类器。

按 P3.9 §范围「至少覆盖上行、下行和震荡市场状态」：
- 基于指数涨跌幅和波动率分类
- UP: 区间涨幅超过上行阈值
- DOWN: 区间跌幅超过下行阈值
- SIDEWAYS: 涨跌幅在阈值区间内

设计要点：
- 纯函数，无副作用，支持并行调用
- 字段缺失降级处理，不崩溃
- 阈值可配置，默认值基于 A 股历史波动特征
"""
from __future__ import annotations

import math
from typing import Sequence

from core.research.pit_certification.contracts import MarketState


# 默认阈值：基于 A 股年度波动特征
DEFAULT_UP_THRESHOLD: float = 0.10
DEFAULT_DOWN_THRESHOLD: float = -0.10
DEFAULT_HIGH_VOLATILITY: float = 0.25


def classify_market_state(
    market_data: Sequence[tuple[str, float]],
    time_window: tuple[str, str] | None = None,
    *,
    up_threshold: float = DEFAULT_UP_THRESHOLD,
    down_threshold: float = DEFAULT_DOWN_THRESHOLD,
) -> MarketState:
    """分类市场状态为上行、下行或震荡。

    基于区间涨跌幅分类：
    - 涨幅 >= up_threshold → UP
    - 跌幅 <= down_threshold → DOWN
    - 其他 → SIDEWAYS

    Args:
        market_data: 市场指数数据序列，每项为 (date_iso, index_value)。
            date_iso 为日期字符串，index_value 为指数收盘值。
        time_window: 可选的时间窗口 (start_iso, end_iso)，用于过滤数据；
            None 表示使用全部数据。
        up_threshold: 上行阈值（默认 0.10，即 10% 涨幅）。
        down_threshold: 下行阈值（默认 -0.10，即 10% 跌幅）。

    Returns:
        MarketState: 市场状态分类结果

    Raises:
        ValueError: market_data 为空时
    """
    if not market_data:
        raise ValueError("market_data 不能为空")

    # 过滤时间窗口
    filtered = [
        (str(item[0]), value)
        for item in market_data
        if isinstance(item, (tuple, list))
        and len(item) >= 2
        and (value := _finite_number(item[1])) is not None
    ]
    if time_window is not None:
        if not isinstance(time_window, (tuple, list)) or len(time_window) < 2:
            return MarketState.SIDEWAYS
        start, end = str(time_window[0]), str(time_window[1])
        filtered = [
            (d, v) for d, v in filtered if _in_window(d, start, end)
        ]

    if len(filtered) < 2:
        # 数据不足时降级为 SIDEWAYS，不崩溃
        return MarketState.SIDEWAYS

    # 按日期排序
    filtered.sort(key=lambda x: x[0])

    start_value = filtered[0][1]
    end_value = filtered[-1][1]

    # 防御性编程：起始值为 0 或负数时无法计算涨跌幅
    if start_value <= 0 or not math.isfinite(start_value) or not math.isfinite(end_value):
        return MarketState.SIDEWAYS

    total_return = (end_value - start_value) / start_value

    up_threshold = _finite_number(up_threshold, DEFAULT_UP_THRESHOLD)
    down_threshold = _finite_number(down_threshold, DEFAULT_DOWN_THRESHOLD)

    if total_return >= up_threshold:
        return MarketState.UP
    if total_return <= down_threshold:
        return MarketState.DOWN
    return MarketState.SIDEWAYS


def classify_market_state_by_return(
    total_return: float,
    *,
    up_threshold: float = DEFAULT_UP_THRESHOLD,
    down_threshold: float = DEFAULT_DOWN_THRESHOLD,
) -> MarketState:
    """直接按已知涨跌幅分类市场状态。

    用于已有区间收益率的场景，避免重复计算。

    Args:
        total_return: 区间总收益率（如 0.15 表示 15%）
        up_threshold: 上行阈值
        down_threshold: 下行阈值

    Returns:
        MarketState: 市场状态分类结果
    """
    total_return = _finite_number(total_return)
    up_threshold = _finite_number(up_threshold, DEFAULT_UP_THRESHOLD)
    down_threshold = _finite_number(down_threshold, DEFAULT_DOWN_THRESHOLD)
    if total_return is None:
        return MarketState.SIDEWAYS
    if total_return >= up_threshold:
        return MarketState.UP
    if total_return <= down_threshold:
        return MarketState.DOWN
    return MarketState.SIDEWAYS


def _in_window(date_iso: str, start: str, end: str) -> bool:
    """判断日期是否在窗口内。

    Args:
        date_iso: 待判断日期
        start: 窗口起始
        end: 窗口结束

    Returns:
        bool: 是否在窗口内
    """
    # 归一化为 YYYY-MM-DD 比较
    d = str(date_iso or "")[:10]
    s = str(start or "")[:10]
    e = str(end or "")[:10]
    return s <= d <= e


def _finite_number(value: object, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default
