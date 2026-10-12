"""通知服务核心模块

NotificationService 负责将通知消息分发到所有已注册的通道。
内置通道：local_log（始终活跃）、openclaw、feishu、pushplus。

设计原则：
- 通知失败不影响任务状态
- 通知内容不包含密钥/Token
- 每条通知 summary 不超过 200 字符
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from config.logger import get_logger
from core.notifications.models import NotificationPayload

logger = get_logger(__name__)

# 通知日志存储目录
NOTIFICATION_LOG_DIR = Path("data/notifications")


class NotificationService:
    """通知服务，将通知消息分发到所有已注册的通道。

    内置通道：
    - local_log: 记录通知到日志文件（始终活跃）
    - openclaw: 发送到 OpenClaw/ClawBot（需配置）
    - feishu: 发送到飞书 Webhook（需配置）
    - pushplus: 发送到 PushPlus（个人微信/QQ，需配置 token）

    Usage:
        svc = NotificationService()
        svc.add_channel("openclaw", openclaw_handler)
        svc.notify(NotificationPayload(...))
    """

    def __init__(self) -> None:
        """初始化通知服务，注册内置通道。"""
        self._channels: Dict[str, Callable[[NotificationPayload], bool]] = {}
        self._lock = threading.Lock()

        # 始终注册本地日志通道
        self._channels["local_log"] = self._local_log_handler

        # 尝试注册 OpenClaw 通道
        try:
            from core.notifications.wechat_clawbot_notifier import WeChatClawBotNotifier
            self._openclaw_notifier = WeChatClawBotNotifier()
            self._channels["openclaw"] = self._openclaw_notifier.send
        except Exception as e:
            logger.warning(f"OpenClaw 通道注册失败: {e}")

        # 尝试注册飞书通道
        try:
            from core.notifications.feishu_notifier import FeishuNotifier
            self._feishu_notifier = FeishuNotifier()
            self._channels["feishu"] = self._feishu_notifier.send
        except Exception as e:
            logger.warning(f"飞书通道注册失败: {e}")

        # 尝试注册 PushPlus 通道（个人微信/QQ 推送）
        try:
            from core.notifications.pushplus_notifier import PushPlusNotifier
            self._pushplus_notifier = PushPlusNotifier()
            self._channels["pushplus"] = self._pushplus_notifier.send
        except Exception as e:
            logger.warning(f"PushPlus 通道注册失败: {e}")

        # 确保通知日志目录存在
        NOTIFICATION_LOG_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"通知服务已初始化，通道: {list(self._channels.keys())}"
        )

    def notify(self, payload: NotificationPayload) -> bool:
        """发送通知到所有已注册的通道。

        通知失败不影响其他通道，也不影响任务状态。

        Args:
            payload: 通知消息

        Returns:
            bool: 是否至少有一个通道成功发送
        """
        any_success = False
        channel_results: dict[str, bool] = {}

        with self._lock:
            channel_names = list(self._channels.keys())

        for name in channel_names:
            try:
                with self._lock:
                    handler = self._channels.get(name)
                if handler is None:
                    continue

                result = handler(payload)
                channel_results[name] = bool(result)
                if result:
                    any_success = True
                    logger.debug(f"通知通道 '{name}' 发送成功: {payload.event_type}")
                else:
                    logger.warning(f"通知通道 '{name}' 发送返回 False: {payload.event_type}")

            except Exception as e:
                channel_results[name] = False
                # 通知失败不影响其他通道和任务状态
                logger.error(f"通知通道 '{name}' 发送异常: {e}", exc_info=True)

        required_any = []
        if isinstance(payload.data, dict):
            required_any = [
                str(name) for name in payload.data.get("delivery_required_any") or []
                if str(name)
            ]
        if required_any:
            primary_success = any(channel_results.get(name, False) for name in required_any)
            if not primary_success:
                logger.error(
                    "主推送通道全部失败 event=%s required=%s results=%s",
                    payload.event_type,
                    required_any,
                    {name: channel_results.get(name, False) for name in required_any},
                )
            else:
                logger.info(
                    "主推送通道送达 event=%s channels=%s",
                    payload.event_type,
                    [name for name in required_any if channel_results.get(name, False)],
                )
            return primary_success

        if not any_success:
            logger.warning(f"所有通知通道均发送失败: {payload.event_type}")

        return any_success

    def add_channel(self, channel_name: str, handler: Callable[[NotificationPayload], bool]) -> None:
        """注册通知通道。

        Args:
            channel_name: 通道名称
            handler: 通知处理函数，接收 NotificationPayload，返回 bool
        """
        with self._lock:
            self._channels[channel_name] = handler
        logger.info(f"通知通道已注册: {channel_name}")

    def remove_channel(self, channel_name: str) -> None:
        """移除通知通道。

        不允许移除 local_log 通道。

        Args:
            channel_name: 通道名称
        """
        if channel_name == "local_log":
            logger.warning("不允许移除 local_log 通道")
            return

        with self._lock:
            removed = self._channels.pop(channel_name, None)
        if removed is not None:
            logger.info(f"通知通道已移除: {channel_name}")
        else:
            logger.warning(f"通知通道不存在: {channel_name}")

    def get_channels(self) -> list[str]:
        """获取所有已注册的通道名称。

        Returns:
            通道名称列表
        """
        with self._lock:
            return list(self._channels.keys())

    # --------------------------------------------------------
    # 内置通道处理器
    # --------------------------------------------------------

    def _local_log_handler(self, payload: NotificationPayload) -> bool:
        """本地日志通道处理器。

        将通知写入 data/notifications/ 目录下的日志文件，
        同时输出到标准日志。

        Args:
            payload: 通知消息

        Returns:
            bool: 是否记录成功
        """
        try:
            # 写入日志文件
            log_file = NOTIFICATION_LOG_DIR / f"{payload.event_type}.jsonl"
            log_entry = payload.to_dict()

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

            # 同时输出到标准日志
            priority_map = {
                "high": logger.error,
                "medium": logger.warning,
                "low": logger.info,
            }
            log_fn = priority_map.get(payload.priority, logger.info)
            log_fn(
                f"[通知] [{payload.priority.upper()}] {payload.title}: "
                f"{payload.summary}"
            )

            return True

        except Exception as e:
            logger.error(f"本地日志通道写入失败: {e}", exc_info=True)
            return False


# ============================================================
# 全局单例
# ============================================================

_instance: Optional[NotificationService] = None
_instance_lock = threading.Lock()


def get_notification_service_core() -> NotificationService:
    """获取通知服务核心层全局单例。

    Returns:
        NotificationService: 通知服务实例
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = NotificationService()
    return _instance
