"""基于本地 JSON 文件的数据源同步水位管理。"""

import json
import os
from datetime import datetime
from typing import Optional


class WatermarkManager:
    """跟踪每个数据源的最后成功同步时间。"""

    def __init__(self, state_file: str = "data/watermarks.json"):
        """
        初始化水位管理器。

        Args:
            state_file: 水位状态文件路径。
        """
        self.state_file = state_file
        parent = os.path.dirname(state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(state_file):
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def _load(self) -> dict:
        """加载当前水位状态。"""
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        """保存水位状态。"""
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

    def get_watermark(self, source: str) -> datetime:
        """获取指定数据源的水位，默认为 2000-01-01。"""
        data = self._load()
        ts = data.get(source)
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                pass
        return datetime(2000, 1, 1)

    def set_watermark(self, source: str, dt: Optional[datetime] = None) -> None:
        """设置指定数据源的水位。"""
        data = self._load()
        data[source] = (dt or datetime.now()).isoformat()
        self._save(data)
