"""
产业链自动更新调度器（占位实现）

完整实现需接入定时任务与数据源同步，当前提供空实现避免启动失败。
"""
from typing import Optional


class IndustryAutoUpdateScheduler:
    """产业链数据自动更新调度器"""

    def __init__(self) -> None:
        self._started = False

    def start(self) -> None:
        """启动调度器"""
        if self._started:
            return
        self._started = True

    def stop(self) -> None:
        """停止调度器"""
        self._started = False


_scheduler_instance: Optional[IndustryAutoUpdateScheduler] = None


def get_industry_auto_update_scheduler() -> IndustryAutoUpdateScheduler:
    """获取调度器单例"""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = IndustryAutoUpdateScheduler()
    return _scheduler_instance
