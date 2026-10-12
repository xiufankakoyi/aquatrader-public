"""任务数据模型

定义 Task 数据类，用于表示异步任务的完整生命周期信息。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Task:
    """异步任务数据模型

    Attributes:
        task_id: 任务唯一标识
        task_type: 任务类型，如 "run_perilla_topic_fast"
        status: 任务状态: queued / running / success / failed / partial / cancelled
        params: 任务参数字典
        source: 任务来源标识（如 "mobile" / "web" / "scheduler"）
        created_at: 创建时间，ISO 格式字符串
        started_at: 开始执行时间，ISO 格式字符串，未开始为 None
        finished_at: 完成时间，ISO 格式字符串，未完成为 None
        elapsed_ms: 执行耗时（毫秒），未完成为 None
        log_path: 日志文件路径，未完成为 None
        report_paths: 产出报告文件路径列表
        error: 错误信息字典，包含 code / message 等字段，无错误为 None
        progress: 进度信息字典，默认 {"percent": 0, "message": ""}
    """

    task_id: str
    task_type: str
    status: str = "queued"
    params: dict = field(default_factory=dict)
    source: str = ""
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_ms: int | None = None
    log_path: str | None = None
    report_paths: list[str] = field(default_factory=list)
    error: dict | None = None
    progress: dict = field(default_factory=lambda: {"percent": 0, "message": ""})

    def to_dict(self) -> dict[str, Any]:
        """将 Task 转换为可序列化字典

        Returns:
            包含所有字段的字典
        """
        return asdict(self)

    def to_json(self) -> str:
        """将 Task 序列化为 JSON 字符串

        Returns:
            JSON 格式字符串
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """从字典反序列化 Task 实例

        Args:
            data: 包含 Task 字段的字典

        Returns:
            Task 实例
        """
        return cls(
            task_id=data.get("task_id", ""),
            task_type=data.get("task_type", ""),
            status=data.get("status", "queued"),
            params=data.get("params", {}),
            source=data.get("source", ""),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            elapsed_ms=data.get("elapsed_ms"),
            log_path=data.get("log_path"),
            report_paths=data.get("report_paths", []),
            error=data.get("error"),
            progress=data.get("progress", {"percent": 0, "message": ""}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> Task:
        """从 JSON 字符串反序列化 Task 实例

        Args:
            json_str: JSON 格式字符串

        Returns:
            Task 实例
        """
        return cls.from_dict(json.loads(json_str))
