"""区间算术：用于盈利测算的区间传播。

所有定量输入保留 low/high 区间，运算时按区间算术传播，避免伪精确。
缺失值不当作 0；缺输入返回 insufficient_data。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Interval:
    """闭区间 [low, high]。

    Attributes:
        low: 下界。
        high: 上界。
    """

    low: float
    high: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.low) or not math.isfinite(self.high):
            raise ValueError("区间端点必须为有限数")
        if self.low > self.high:
            # 容错：自动交换，避免输入顺序错误导致计算崩溃
            # frozen dataclass 需先用临时变量保存原值再交换
            low_val, high_val = self.low, self.high
            object.__setattr__(self, "low", high_val)
            object.__setattr__(self, "high", low_val)

    @classmethod
    def point(cls, value: float) -> "Interval":
        """构造点区间。"""
        return cls(float(value), float(value))

    @classmethod
    def from_pair(cls, low: float, high: float | None = None) -> "Interval":
        """从 low/high 构造，high 缺省时退化为点区间。"""
        return cls(float(low), float(high if high is not None else low))

    @property
    def mid(self) -> float:
        """中点。"""
        return (self.low + self.high) / 2.0

    @property
    def width(self) -> float:
        """区间宽度。"""
        return self.high - self.low

    @property
    def is_point(self) -> bool:
        """是否为点区间。"""
        return self.low == self.high

    def __add__(self, other: "Interval | float") -> "Interval":
        if isinstance(other, (int, float)):
            return Interval(self.low + float(other), self.high + float(other))
        return Interval(self.low + other.low, self.high + other.high)

    __radd__ = __add__

    def __sub__(self, other: "Interval | float") -> "Interval":
        if isinstance(other, (int, float)):
            return Interval(self.low - float(other), self.high - float(other))
        return Interval(self.low - other.high, self.high - other.low)

    def __rsub__(self, other: float) -> "Interval":
        return Interval(float(other) - self.high, float(other) - self.low)

    def __mul__(self, other: "Interval | float") -> "Interval":
        if isinstance(other, (int, float)):
            s = float(other)
            if s >= 0:
                return Interval(self.low * s, self.high * s)
            return Interval(self.high * s, self.low * s)
        corners = [self.low * other.low, self.low * other.high,
                   self.high * other.low, self.high * other.high]
        return Interval(min(corners), max(corners))

    __rmul__ = __mul__

    def __truediv__(self, other: "Interval | float") -> "Interval":
        if isinstance(other, (int, float)):
            divisor = float(other)
            if divisor == 0:
                raise ZeroDivisionError("区间除以零")
            return self * (1.0 / divisor)
        if other.low <= 0.0 <= other.high:
            raise ZeroDivisionError("除数区间包含零，无法做区间除法")
        inv_low, inv_high = (1.0 / other.high, 1.0 / other.low) if other.low > 0 else (1.0 / other.low, 1.0 / other.high)
        return self * Interval(inv_low, inv_high)

    def __floordiv__(self, other: "Interval | float") -> "Interval":
        # 整除用于股数取整场景：向下取整下界、向上... 实际可兑现销量用 min 后再向下取整
        if isinstance(other, (int, float)):
            divisor = float(other)
            if divisor == 0:
                raise ZeroDivisionError("区间除以零")
            return Interval(math.floor(self.low / divisor), math.floor(self.high / divisor))
        raise TypeError("Interval floordiv 仅支持标量")

    @staticmethod
    def minimum(a: "Interval", b: "Interval") -> "Interval":
        """区间最小值：min(a,b) 的区间。"""
        return Interval(min(a.low, b.low), min(a.high, b.high))

    @staticmethod
    def maximum(a: "Interval", b: "Interval") -> "Interval":
        """区间最大值：max(a,b) 的区间。"""
        return Interval(max(a.low, b.low), max(a.high, b.high))

    @staticmethod
    def min_chain(items: Iterable["Interval"]) -> "Interval | None":
        """对一组区间连续取最小。"""
        items = list(items)
        if not items:
            return None
        result = items[0]
        for item in items[1:]:
            result = Interval.minimum(result, item)
        return result

    def clamp(self, floor: float | None = None, ceil: float | None = None) -> "Interval":
        """裁剪到 [floor, ceil]。"""
        low = self.low
        high = self.high
        if floor is not None:
            low = max(low, float(floor))
            high = max(high, float(floor))
        if ceil is not None:
            low = min(low, float(ceil))
            high = min(high, float(ceil))
        if low > high:
            low = high = (low + high) / 2.0
        return Interval(low, high)

    def to_dict(self) -> dict[str, float]:
        """序列化。"""
        return {"low": round(self.low, 6), "high": round(self.high, 6), "mid": round(self.mid, 6), "width": round(self.width, 6)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object] | None) -> "Interval | None":
        """反序列化。"""
        if not isinstance(payload, Mapping) or not payload:
            return None
        try:
            if "low" in payload and "high" in payload:
                return cls(float(payload["low"]), float(payload["high"]))
            if "mid" in payload and "width" in payload:
                mid = float(payload["mid"])
                half = float(payload["width"]) / 2.0
                return cls(mid - half, mid + half)
            if "low" in payload:
                return cls.point(float(payload["low"]))
        except (TypeError, ValueError, OverflowError):
            return None
        return None
