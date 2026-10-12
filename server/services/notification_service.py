"""通知服务适配层。

作为 server 层到 core/notifications 的桥接，提供 task_runner.py 所需的
notify_daily_brief_ready / notify_task_succeeded / notify_task_failed 函数。

飞书推送中的详情链接使用 Secrets.SITE_BASE_URL 拼接，指向前端 /mobile/daily 页面。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from config.config import Config
from config.logger import get_logger
from config.secrets import Secrets
from core.notifications.models import NotificationPayload
from core.notifications.notification_service import get_notification_service_core

logger = get_logger(__name__)

# 日报文件路径
_DAILY_BRIEF_PATH = Path("data/reports/daily_brief.md")
_PERILLA_DISCOVERY_DIR = Path("data/reports/perilla_discovery")


def _get_site_base_url() -> str:
    """获取站点外部可访问地址。

    Returns:
        站点基础 URL，默认 http://localhost:5173
    """
    return getattr(Secrets, "SITE_BASE_URL", "http://localhost:5173").rstrip("/")


def _build_daily_link() -> dict[str, str]:
    """构造日报详情链接。

    Returns:
        含 text 和 url 的链接字典
    """
    return {
        "text": "查看紫苏叶日报",
        "url": f"{_get_site_base_url()}/mobile/daily",
    }


def notify_daily_brief_ready() -> bool:
    """日报生成完成后触发飞书推送。

    读取日报文件摘要，构造 NotificationPayload 并分发到所有通道（local_log / feishu）。

    Returns:
        bool: 是否至少有一个通道成功发送
    """
    try:
        # 读取日报摘要
        summary = "紫苏叶日报已生成"
        if _DAILY_BRIEF_PATH.exists():
            content = _DAILY_BRIEF_PATH.read_text(encoding="utf-8")
            # 取前 150 字符作为摘要
            summary = content[:150].replace("\n", " ").strip()
            if len(content) > 150:
                summary += "..."

        payload = NotificationPayload(
            event_type="daily_brief_ready",
            title="AquaTrader 紫苏叶日报",
            summary=summary,
            priority="medium",
            links=[_build_daily_link()],
            data={
                "report_path": str(_DAILY_BRIEF_PATH),
            },
        )

        service = get_notification_service_core()
        return service.notify(payload)
    except Exception as exc:
        logger.error("notify_daily_brief_ready 失败: %s", exc, exc_info=True)
        return False


def notify_task_succeeded(task_id: str, task_type: str, elapsed_ms: int) -> bool:
    """任务成功完成后触发通知。

    Args:
        task_id: 任务 ID
        task_type: 任务类型
        elapsed_ms: 耗时（毫秒）

    Returns:
        bool: 是否至少有一个通道成功发送
    """
    try:
        payload = NotificationPayload(
            event_type="task_succeeded",
            title=f"任务完成: {task_type}",
            summary=f"任务 {task_id} ({task_type}) 成功，耗时 {elapsed_ms}ms",
            priority="low",
            links=[],
            data={
                "task_id": task_id,
                "task_type": task_type,
                "elapsed_ms": elapsed_ms,
            },
        )

        service = get_notification_service_core()
        return service.notify(payload)
    except Exception as exc:
        logger.error("notify_task_succeeded 失败: %s", exc, exc_info=True)
        return False


def notify_task_failed(task_id: str, task_type: str, error_msg: str) -> bool:
    """任务失败后触发通知。

    Args:
        task_id: 任务 ID
        task_type: 任务类型
        error_msg: 错误信息

    Returns:
        bool: 是否至少有一个通道成功发送
    """
    try:
        payload = NotificationPayload(
            event_type="task_failed",
            title=f"任务失败: {task_type}",
            summary=f"任务 {task_id} ({task_type}) 失败: {error_msg[:150]}",
            priority="high",
            links=[],
            data={
                "task_id": task_id,
                "task_type": task_type,
                "error": error_msg,
            },
        )

        service = get_notification_service_core()
        return service.notify(payload)
    except Exception as exc:
        logger.error("notify_task_failed 失败: %s", exc, exc_info=True)
        return False
