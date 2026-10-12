"""任务持久化存储

将任务数据同时写入：
1. data/tasks/tasks.jsonl — 追加写入日志，用于审计和恢复
2. data/tasks/{task_id}.json — 单任务文件，用于快速查询

所有写入操作均为线程安全。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from config.logger import get_logger
from core.tasks.models import Task

logger = get_logger(__name__)

# 项目根目录下的 data/tasks 路径
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_TASKS_DIR = _PROJECT_ROOT / "data" / "tasks"
_LOGS_DIR = _TASKS_DIR / "logs"
_JSONL_PATH = _TASKS_DIR / "tasks.jsonl"


class TaskStore:
    """任务持久化存储

    提供任务的增删改查能力，数据同时写入 JSONL 日志和独立 JSON 文件。
    """

    def __init__(self, tasks_dir: Optional[Path] = None) -> None:
        """初始化存储

        Args:
            tasks_dir: 自定义存储目录，默认为 data/tasks/
        """
        self._tasks_dir = Path(tasks_dir) if tasks_dir else _TASKS_DIR
        self._logs_dir = self._tasks_dir / "logs"
        self._jsonl_path = self._tasks_dir / "tasks.jsonl"
        self._lock = threading.Lock()
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        """确保 data/tasks/ 和 data/tasks/logs/ 目录存在"""
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

    def save(self, task: Task) -> None:
        """保存任务到 JSONL 日志和独立 JSON 文件

        Args:
            task: 要保存的 Task 实例
        """
        task_json = task.to_json()

        # 追加写入 JSONL
        with self._lock:
            try:
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(task_json + "\n")
            except OSError as exc:
                logger.error("写入 tasks.jsonl 失败: %s", exc)

        # 写入独立 JSON 文件
        task_file = self._tasks_dir / f"{task.task_id}.json"
        try:
            task_file.write_text(task_json, encoding="utf-8")
        except OSError as exc:
            logger.error("写入任务文件 %s 失败: %s", task_file, exc)

    def get(self, task_id: str) -> Optional[Task]:
        """根据 task_id 获取任务

        优先从独立 JSON 文件读取，文件不存在时回退到 JSONL 日志扫描。

        Args:
            task_id: 任务唯一标识

        Returns:
            Task 实例，不存在时返回 None
        """
        # 优先读独立文件
        task_file = self._tasks_dir / f"{task_id}.json"
        if task_file.exists():
            try:
                data = json.loads(task_file.read_text(encoding="utf-8"))
                return Task.from_dict(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取任务文件 %s 失败: %s，回退到 JSONL", task_file, exc)

        # 回退到 JSONL 扫描
        return self._find_in_jsonl(task_id)

    def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """列出任务，支持按状态过滤和分页

        从独立 JSON 文件加载所有任务，按创建时间倒序排列。

        Args:
            status: 可选的状态过滤条件
            limit: 返回数量上限
            offset: 跳过前 N 条记录

        Returns:
            Task 实例列表
        """
        tasks: list[Task] = []

        # 从独立 JSON 文件批量加载
        try:
            json_files = sorted(
                self._tasks_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            json_files = []

        for json_file in json_files:
            if json_file.name == "tasks.jsonl":
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                task = Task.from_dict(data)
                if status and task.status != status:
                    continue
                tasks.append(task)
            except (json.JSONDecodeError, OSError):
                continue

        # 应用分页
        return tasks[offset : offset + limit]

    def update(self, task_id: str, **kwargs) -> Optional[Task]:
        """更新任务字段

        读取现有任务，合并更新字段后重新写入。

        Args:
            task_id: 任务唯一标识
            **kwargs: 要更新的字段及值

        Returns:
            更新后的 Task 实例，任务不存在时返回 None
        """
        task = self.get(task_id)
        if task is None:
            return None

        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)

        # 重新写入（不追加 JSONL，仅更新独立文件）
        task_json = task.to_json()
        task_file = self._tasks_dir / f"{task.task_id}.json"
        try:
            task_file.write_text(task_json, encoding="utf-8")
        except OSError as exc:
            logger.error("更新任务文件 %s 失败: %s", task_file, exc)

        return task

    def _find_in_jsonl(self, task_id: str) -> Optional[Task]:
        """在 JSONL 日志中查找指定 task_id 的最新记录

        Args:
            task_id: 任务唯一标识

        Returns:
            Task 实例，不存在时返回 None
        """
        if not self._jsonl_path.exists():
            return None

        last_match: Optional[str] = None
        try:
            with open(self._jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get("task_id") == task_id:
                            last_match = line
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.error("读取 tasks.jsonl 失败: %s", exc)
            return None

        if last_match is None:
            return None

        try:
            return Task.from_json(last_match)
        except (json.JSONDecodeError, KeyError):
            return None
