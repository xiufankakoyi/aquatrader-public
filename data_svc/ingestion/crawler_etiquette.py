"""爬虫请求频率控制。"""

import time
from collections import defaultdict
from threading import Lock


class CrawlerEtiquette:
    """按数据源控制最小请求间隔，避免触发限流。"""

    def __init__(self, min_interval: float = 1.0):
        """
        初始化频率控制器。

        Args:
            min_interval: 两次请求之间的最小间隔（秒）。
        """
        self.min_interval = min_interval
        self._last_call: dict[str, float] = defaultdict(float)
        self._lock = Lock()

    def wait_if_needed(self, source: str) -> None:
        """
        如果需要，阻塞当前线程以满足最小请求间隔。

        Args:
            source: 数据源标识。
        """
        with self._lock:
            elapsed = time.time() - self._last_call[source]
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call[source] = time.time()
