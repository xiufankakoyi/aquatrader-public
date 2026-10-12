"""W-TinyLFU 缓存：频率感知的准入策略 + SLRU 主缓存。

架构（参考 Caffeine）:
    Window LRU (1%) → TinyLFU 准入门 → Main Cache (SLRU)
                                            ├── Probation (20%)
                                            └── Protected (80%)

核心思想:
    1. 新条目先进入 Window，保留近期突发访问的准入机会
    2. Window 淘汰时与 Probation 尾部竞争，频率高者胜出（TinyLFU 准入）
    3. Main Cache 二次命中从 Probation 升级到 Protected
    4. Protected 淘汰后降级到 Probation，不直接驱逐
    5. Count-Min Sketch 定期老化（halving），适应访问模式漂移

适用场景:
    - 热节点、热邻接、热 Claim、查询结果
    - 进程内共享，跨主题复用
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Generic, Hashable, TypeVar

logger = logging.getLogger(__name__)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class CountMinSketch:
    """Count-Min Sketch 频率草图：4-bit 计数器，双哈希，周期性老化。

    Args:
        capacity: 草图容量（建议为缓存最大容量的 10 倍）。
        seed: 哈希种子。
    """

    __slots__ = ("_width", "_seed", "_table", "_reset_mask", "_sample", "_sample_limit")

    def __init__(self, capacity: int, seed: int = 0x1A9C7) -> None:
        # 确保宽度为 2 的幂次，便于位运算取模
        width = 1
        while width < capacity:
            width <<= 1
        self._width = max(width, 16)
        self._seed = seed
        # 4-bit 计数器，每行 width 个，共 2 行（双哈希）
        # 用 bytearray 存储，每 2 个计数器占 1 byte
        self._table: list[bytearray] = [bytearray(self._width // 2) for _ in range(2)]
        # 4-bit 最大值 15，halving 老化
        self._reset_mask = 0x0F
        self._sample = 0
        # 每 10 * capacity 次采样后执行一次 halving
        self._sample_limit = max(capacity * 10, 1000)

    def _hash(self, key: Hashable) -> tuple[int, int]:
        """双哈希：基于 SHA1 的确定性双哈希。

        Args:
            key: 哈希键。

        Returns:
            (index_row0, index_row1) 两个行的索引。
        """
        raw = repr(key).encode("utf-8")
        digest = hashlib.sha1(self._seed.to_bytes(4, "little") + raw).digest()
        idx0 = int.from_bytes(digest[:4], "little") & (self._width - 1)
        idx1 = int.from_bytes(digest[4:8], "little") & (self._width - 1)
        return idx0, idx1

    def _get_counter(self, row: int, idx: int) -> int:
        """读取 4-bit 计数器。"""
        byte_idx = idx >> 1
        if idx & 1:
            return self._table[row][byte_idx] >> 4
        return self._table[row][byte_idx] & 0x0F

    def _set_counter(self, row: int, idx: int, value: int) -> None:
        """写入 4-bit 计数器。"""
        byte_idx = idx >> 1
        if idx & 1:
            self._table[row][byte_idx] = (self._table[row][byte_idx] & 0x0F) | ((value & 0x0F) << 4)
        else:
            self._table[row][byte_idx] = (self._table[row][byte_idx] & 0xF0) | (value & 0x0F)

    def increment(self, key: Hashable) -> None:
        """增加 key 的频率计数（两行均增量，取最小值作为估计频率，不超过 15）。

        Args:
            key: 待增频率的键。
        """
        idx0, idx1 = self._hash(key)
        # 两行都增量，frequency() 取最小值保证保守估计
        c0 = self._get_counter(0, idx0)
        if c0 < 0x0F:
            self._set_counter(0, idx0, c0 + 1)
        c1 = self._get_counter(1, idx1)
        if c1 < 0x0F:
            self._set_counter(1, idx1, c1 + 1)

        self._sample += 1
        if self._sample >= self._sample_limit:
            self._reset()
            self._sample = 0

    def frequency(self, key: Hashable) -> int:
        """返回 key 的估计频率（两行最小值）。

        Args:
            key: 查询键。

        Returns:
            估计频率值 [0, 15]。
        """
        idx0, idx1 = self._hash(key)
        return min(self._get_counter(0, idx0), self._get_counter(1, idx1))

    def _reset(self) -> None:
        """周期性老化：所有 4-bit 计数器右移 1 位（halving），保留相对频率。"""
        for row in range(2):
            for i in range(len(self._table[row])):
                b = self._table[row][i]
                low = (b & 0x0F) >> 1
                high = ((b >> 4) & 0x0F) >> 1
                self._table[row][i] = low | (high << 4)

    def clear(self) -> None:
        """清空草图。"""
        for row in range(2):
            for i in range(len(self._table[row])):
                self._table[row][i] = 0
        self._sample = 0


class WTinyLFUCache(Generic[K, V]):
    """W-TinyLFU 缓存：Window + TinyLFU 准入 + SLRU 主缓存。

    分区:
        - Window LRU: 1% 容量，接收新条目
        - Probation LRU: 20% 主缓存容量，新准入条目
        - Protected LRU: 80% 主缓存容量，二次命中条目

    Args:
        max_size: 最大缓存条目数。
        ttl_seconds: 条目 TTL（0 表示不过期）。
        on_evict: 条目驱逐时的回调函数。
    """

    def __init__(
        self,
        max_size: int = 10000,
        ttl_seconds: float = 0.0,
        on_evict: Callable[[K, V], None] | None = None,
    ) -> None:
        self._max_size = max(max_size, 10)
        self._ttl = ttl_seconds
        self._on_evict = on_evict

        # 分区容量
        self._window_size = max(1, self._max_size // 100)
        self._main_size = self._max_size - self._window_size
        self._protected_size = max(1, int(self._main_size * 0.80))
        self._probation_size = max(1, self._main_size - self._protected_size)

        # 三段 LRU（OrderedDict: 尾部为最新）
        self._window: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._probation: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._protected: OrderedDict[K, tuple[float, V]] = OrderedDict()

        # 频率草图（容量为缓存大小的 10 倍）
        self._sketch = CountMinSketch(self._max_size * 10)

        # 统计
        self._hits = 0
        self._misses = 0
        self._admissions = 0
        self._evictions = 0

        self._lock = threading.RLock()

    def get(self, key: K) -> V | None:
        """查询缓存。命中时更新频率并升级段位。

        Args:
            key: 缓存键。

        Returns:
            缓存值，未命中返回 None。
        """
        now = time.time()
        with self._lock:
            # 1. Window 命中
            if key in self._window:
                ts, value = self._window[key]
                if self._ttl > 0 and now - ts > self._ttl:
                    del self._window[key]
                    self._misses += 1
                    return None
                self._hits += 1
                self._sketch.increment(key)
                # Window 命中不升级，仅 move_to_end
                self._window.move_to_end(key)
                return value

            # 2. Probation 命中 → 升级到 Protected
            if key in self._probation:
                ts, value = self._probation[key]
                if self._ttl > 0 and now - ts > self._ttl:
                    del self._probation[key]
                    self._misses += 1
                    return None
                self._hits += 1
                self._sketch.increment(key)
                del self._probation[key]
                self._protected[key] = (ts, value)
                # Protected 溢出 → 降级到 Probation
                if len(self._protected) > self._protected_size:
                    demoted_key, demoted_val = self._protected.popitem(last=False)
                    self._probation[demoted_key] = demoted_val
                    self._rebalance_probation()
                return value

            # 3. Protected 命中
            if key in self._protected:
                ts, value = self._protected[key]
                if self._ttl > 0 and now - ts > self._ttl:
                    del self._protected[key]
                    self._misses += 1
                    return None
                self._hits += 1
                self._sketch.increment(key)
                self._protected.move_to_end(key)
                return value

            self._misses += 1
            return None

    def put(self, key: K, value: V) -> None:
        """写入缓存。新条目进入 Window，Window 溢出经 TinyLFU 准入到 Main。

        Args:
            key: 缓存键。
            value: 缓存值。
        """
        now = time.time()
        with self._lock:
            # 已存在则更新
            if key in self._window:
                self._window[key] = (now, value)
                self._window.move_to_end(key)
                self._sketch.increment(key)
                return
            if key in self._probation:
                self._probation[key] = (now, value)
                del self._probation[key]
                self._protected[key] = (now, value)
                self._sketch.increment(key)
                if len(self._protected) > self._protected_size:
                    demoted_key, demoted_val = self._protected.popitem(last=False)
                    self._probation[demoted_key] = demoted_val
                    self._rebalance_probation()
                return
            if key in self._protected:
                self._protected[key] = (now, value)
                self._protected.move_to_end(key)
                self._sketch.increment(key)
                return

            # 新条目进入 Window
            self._window[key] = (now, value)
            self._sketch.increment(key)

            # Window 溢出 → TinyLFU 准入
            if len(self._window) > self._window_size:
                self._admit_from_window()

    def _admit_from_window(self) -> None:
        """Window 溢出时，尾部条目与 Probation 尾部竞争，频率高者进入 Main。"""
        # 取 Window 最老条目
        win_key, win_val = self._window.popitem(last=False)

        if len(self._probation) < self._probation_size:
            # Probation 有空位，直接准入
            self._probation[win_key] = win_val
            self._admissions += 1
            return

        # Probation 满，与尾部竞争
        prob_key = next(iter(self._probation))
        prob_val = self._probation[prob_key]

        # TinyLFU 准入：频率高者胜出
        win_freq = self._sketch.frequency(win_key)
        prob_freq = self._sketch.frequency(prob_key)

        if win_freq > prob_freq:
            # 新条目胜出，驱逐旧条目
            self._evictions += 1
            if self._on_evict:
                try:
                    self._on_evict(prob_key, prob_val[1])
                except Exception as exc:
                    logger.warning("on_evict 回调失败: %s", exc)
            del self._probation[prob_key]
            self._probation[win_key] = win_val
            self._admissions += 1
        else:
            # 旧条目留下，新条目被拒绝
            self._evictions += 1
            if self._on_evict:
                try:
                    self._on_evict(win_key, win_val[1])
                except Exception as exc:
                    logger.warning("on_evict 回调失败: %s", exc)

    def _rebalance_probation(self) -> None:
        """Probation 溢出时驱逐尾部。"""
        while len(self._probation) > self._probation_size:
            evict_key, evict_val = self._probation.popitem(last=False)
            self._evictions += 1
            if self._on_evict:
                try:
                    self._on_evict(evict_key, evict_val[1])
                except Exception as exc:
                    logger.warning("on_evict 回调失败: %s", exc)

    def invalidate(self, key: K) -> None:
        """失效单个条目。"""
        with self._lock:
            self._window.pop(key, None)
            self._probation.pop(key, None)
            self._protected.pop(key, None)

    def invalidate_all(self) -> None:
        """清空全部缓存。"""
        with self._lock:
            self._window.clear()
            self._probation.clear()
            self._protected.clear()
            self._sketch.clear()
            self._hits = 0
            self._misses = 0
            self._admissions = 0
            self._evictions = 0

    def snapshot(self) -> dict[str, Any]:
        """返回缓存统计快照。"""
        with self._lock:
            total = self._hits + self._misses
            return {
                "max_size": self._max_size,
                "window_size": self._window_size,
                "probation_size": self._probation_size,
                "protected_size": self._protected_size,
                "current_window": len(self._window),
                "current_probation": len(self._probation),
                "current_protected": len(self._protected),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total > 0 else 0.0,
                "admissions": self._admissions,
                "evictions": self._evictions,
            }
