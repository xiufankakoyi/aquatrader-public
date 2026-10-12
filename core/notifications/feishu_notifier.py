"""飞书 Webhook 通知通道

FeishuNotifier 通过飞书自定义机器人 Webhook 发送通知消息。
v1 为桩实现：未配置 Webhook 时仅记录日志。

消息格式：短文本 + 链接。
"""

from __future__ import annotations

from typing import Optional

from config.logger import get_logger
from core.notifications.models import NotificationPayload

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书 Webhook 通知发送器。

    v1 桩实现：未配置 FEISHU_WEBHOOK 时仅记录日志。
    配置后通过 HTTP POST 发送消息到飞书群。

    Usage:
        notifier = FeishuNotifier()
        notifier.send(payload)
    """

    def __init__(self) -> None:
        """初始化飞书通知器。"""
        self._webhook_url: Optional[str] = None

        # 尝试从配置加载飞书 Webhook URL
        try:
            from config.config import Config
            self._webhook_url = Config.FEISHU_WEBHOOK
        except Exception:
            pass

        if self._webhook_url:
            logger.info("飞书通知器已配置 Webhook")
        else:
            logger.info("飞书通知器未配置 Webhook，将使用桩模式")

    def send(self, payload: NotificationPayload) -> bool:
        """发送通知到飞书 Webhook。

        v1 桩实现：未配置 Webhook 时仅记录日志。
    配置后通过 HTTP POST 发送短文本消息到飞书群。

        消息格式：短文本 + 链接，不使用长 Markdown。

        Args:
            payload: 通知消息

        Returns:
            bool: 是否发送成功（桩模式始终返回 True）
        """
        try:
            # 构建短文本消息
            message = self._format_short_text(payload)

            if self._webhook_url:
                # 有 Webhook 配置时，尝试实际发送
                return self._send_via_webhook(message, payload)
            else:
                # 桩模式：仅记录日志
                logger.info(f"[飞书桩] {message}")
                return True

        except Exception as e:
            logger.error(f"飞书通知发送异常: {e}", exc_info=True)
            return False

    def _format_short_text(self, payload: NotificationPayload) -> str:
        """将通知格式化为短文本消息。

        格式：[优先级] 标题: 摘要
        如果有链接，追加到末尾。

        Args:
            payload: 通知消息

        Returns:
            格式化后的短文本
        """
        priority_label = {
            "high": "!!",
            "medium": "!",
            "low": "",
        }.get(payload.priority, "")

        text = f"{priority_label}{payload.title}: {payload.summary}"

        # 追加链接
        for link in payload.links:
            link_text = link.get("text", "")
            link_url = link.get("url", "")
            if link_url:
                text += f"\n{link_text}: {link_url}"
                break  # 只追加第一个链接

        return text

    def _send_via_webhook(self, message: str, payload: NotificationPayload) -> bool:
        """通过飞书 Webhook 发送通知。

        推送优先级：
        1. 若 payload.data 含结构化 themes/observations，构造飞书原生 table 卡片
           （飞书 V7.4+ 原生 table 组件，渲染远优于 markdown 表格）
        2. 若 payload.data.markdown_content 存在，发送 markdown 卡片
        3. 退回到 title + summary 短摘要
        4. 任意卡片失败时降级为纯文本

        Args:
            message: 格式化后的短文本
            payload: 原始通知消息

        Returns:
            bool: 是否发送成功
        """
        try:
            from core.notifications.feishu_push import FeishuPush

            pusher = FeishuPush(self._webhook_url)
            priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(payload.priority, "⚪")

            # 优先尝试构造原生 table 卡片（盘前复盘等结构化场景）
            card = self._build_native_card(payload, priority_emoji)
            if card is not None:
                if pusher.push_card(card, title=payload.title):
                    logger.info(f"飞书原生卡片通知发送成功: {payload.event_type}")
                    return True
                logger.warning("飞书原生卡片发送失败，尝试 markdown 降级")

            # 退回到 markdown 卡片
            full_markdown = ""
            if isinstance(payload.data, dict):
                raw_md = payload.data.get("markdown_content") or ""
                if isinstance(raw_md, str):
                    full_markdown = raw_md.strip()

            if full_markdown:
                md_content = f"{priority_emoji} **{payload.title}**\n\n{full_markdown}"
            else:
                md_content = f"{priority_emoji} **{payload.title}**\n\n{payload.summary}"

            for link in payload.links:
                link_text = link.get("text", "查看详情")
                link_url = link.get("url", "")
                if link_url:
                    md_content += f"\n\n[{link_text}]({link_url})"

            if pusher.push_markdown(md_content, title=payload.title):
                logger.info(f"飞书 markdown 卡片通知发送成功: {payload.event_type}")
                return True

            logger.warning("飞书 markdown 卡片发送失败，降级为纯文本")
            return self._send_text_fallback(message)

        except ImportError:
            logger.warning("FeishuPush 不可用，使用纯文本降级")
            return self._send_text_fallback(message)
        except Exception as e:
            logger.error(f"飞书 Webhook 请求异常: {e}", exc_info=True)
            return False

    def _build_native_card(self, payload: NotificationPayload, priority_emoji: str):
        """从 payload.data 构造飞书原生 table 卡片。

        仅当 payload.data 包含 themes/observations 等结构化字段时才构造，
        否则返回 None 让上游退回到 markdown 渲染。

        Args:
            payload: 通知消息
            priority_emoji: 优先级 emoji

        Returns:
            飞书卡片 dict 或 None（无结构化数据时）
        """
        if not isinstance(payload.data, dict):
            return None
        themes = payload.data.get("themes") or []
        observations = payload.data.get("observations") or []
        if not themes and not observations:
            return None

        header_template = {"high": "red", "medium": "yellow", "low": "green"}.get(
            payload.priority, "blue"
        )
        elements: list[dict] = []

        # 顶部摘要
        summary_parts: list[str] = []
        if themes:
            validated = sum(1 for t in themes if t.get("research_gate_status") == "validated")
            summary_parts.append(f"动态主题 **{len(themes)}** 个，Research 已验证 **{validated}** 个")
        watch_count = payload.data.get("ready_watch_count")
        if watch_count is not None:
            summary_parts.append(f"READY/WATCH **{watch_count}** 个")
        status = payload.data.get("status") or ""
        if status:
            summary_parts.append(f"运行状态 `{status}`")
        if summary_parts:
            elements.append(
                {"tag": "markdown", "content": f"{priority_emoji} " + " · ".join(summary_parts)}
            )
            elements.append({"tag": "hr"})

        # 动态主题表
        if themes:
            elements.append({"tag": "markdown", "content": "**动态主题**"})
            elements.append(
                self._build_themes_table(themes)
            )
            elements.append({"tag": "hr"})

        # WATCH 名单（从 observations 过滤 research_validated）
        watch_obs = [o for o in observations if o.get("research_validated")]
        if watch_obs:
            elements.append({"tag": "markdown", "content": "**Research 验证核心（日线状态）**"})
            elements.append(self._build_watch_table(watch_obs))
            elements.append({"tag": "hr"})

        # 探索观察（非 validated）
        explore_obs = [o for o in observations if not o.get("research_validated")]
        if explore_obs:
            elements.append({"tag": "markdown", "content": "**探索观察（证据未闭环）**"})
            elements.append(self._build_explore_table(explore_obs))
            elements.append({"tag": "hr"})

        # 研究任务结果
        tasks = payload.data.get("research_task_runs") or []
        if tasks:
            elements.append({"tag": "markdown", "content": "**研究任务**"})
            elements.append(self._build_tasks_table(tasks))

        # 底部免责声明
        elements.append(
            {"tag": "markdown", "content": "_状态仅用于研究观察与后续验证，不构成投资建议。_"}
        )

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": payload.title},
                "template": header_template,
            },
            "elements": elements,
        }

    @staticmethod
    def _build_themes_table(themes: list[dict]) -> dict:
        """构造动态主题飞书原生 table 组件。

        Args:
            themes: payload.themes 列表

        Returns:
            飞书 table 组件 dict
        """
        columns = [
            {"name": "theme_name", "display_name": "主题", "data_type": "text"},
            {"name": "theme_type", "display_name": "类型", "data_type": "text"},
            {"name": "phase", "display_name": "阶段", "data_type": "text"},
            {
                "name": "negative_status",
                "display_name": "负面状态",
                "data_type": "text",
            },
            {
                "name": "research_gate_status",
                "display_name": "Research 门禁",
                "data_type": "text",
            },
            {
                "name": "validated_core_count",
                "display_name": "验证核心数",
                "data_type": "number",
            },
        ]
        rows = []
        for t in themes:
            rows.append(
                {
                    "theme_name": t.get("theme_name", ""),
                    "theme_type": t.get("theme_type", ""),
                    "phase": t.get("phase", ""),
                    "negative_status": t.get("negative_status", ""),
                    "research_gate_status": t.get("research_gate_status", ""),
                    "validated_core_count": t.get("validated_core_count", 0),
                }
            )
        return {
            "tag": "table",
            "page_size": min(len(rows), 20),
            "row_height": "low",
            "columns": columns,
            "rows": rows,
        }

    @staticmethod
    def _build_watch_table(observations: list[dict]) -> dict:
        """构造 WATCH 名单飞书原生 table 组件。

        Args:
            observations: research_validated=True 的观察列表

        Returns:
            飞书 table 组件 dict
        """
        columns = [
            {"name": "status", "display_name": "状态", "data_type": "text"},
            {"name": "symbol", "display_name": "证券", "data_type": "text"},
            {"name": "company_name", "display_name": "公司", "data_type": "text"},
            {"name": "theme_name", "display_name": "主题", "data_type": "text"},
            {"name": "technical_state", "display_name": "技术状态", "data_type": "text"},
            {"name": "claim_status", "display_name": "Claim", "data_type": "text"},
            {
                "name": "leadership_score",
                "display_name": "领导分数",
                "data_type": "number",
            },
            {"name": "ret5_pct", "display_name": "5日", "data_type": "text"},
            {"name": "ret20_pct", "display_name": "20日", "data_type": "text"},
        ]
        rows = []
        for o in observations:
            mf = o.get("market_features") or {}
            ret5 = mf.get("ret5")
            ret20 = mf.get("ret20")
            company = o.get("company_name") or "暂无本地证据"
            rows.append(
                {
                    "status": o.get("status", ""),
                    "symbol": o.get("symbol", ""),
                    "company_name": company,
                    "theme_name": o.get("theme_name", ""),
                    "technical_state": o.get("technical_state", ""),
                    "claim_status": o.get("claim_status", ""),
                    "leadership_score": mf.get("leadership_score", 0.0),
                    "ret5_pct": f"{ret5 * 100:.2f}%" if isinstance(ret5, (int, float)) else "-",
                    "ret20_pct": f"{ret20 * 100:.2f}%" if isinstance(ret20, (int, float)) else "-",
                }
            )
        return {
            "tag": "table",
            "page_size": min(len(rows), 20),
            "row_height": "low",
            "columns": columns,
            "rows": rows,
        }

    @staticmethod
    def _build_explore_table(observations: list[dict]) -> dict:
        """构造探索观察飞书原生 table 组件。

        Args:
            observations: research_validated=False 的观察列表

        Returns:
            飞书 table 组件 dict
        """
        columns = [
            {"name": "symbol", "display_name": "证券", "data_type": "text"},
            {"name": "company_name", "display_name": "公司", "data_type": "text"},
            {"name": "theme_name", "display_name": "主题", "data_type": "text"},
            {"name": "technical_state", "display_name": "技术状态", "data_type": "text"},
            {"name": "status", "display_name": "研究状态", "data_type": "text"},
        ]
        rows = []
        for o in observations:
            company = o.get("company_name") or "暂无本地证据"
            rows.append(
                {
                    "symbol": o.get("symbol", ""),
                    "company_name": company,
                    "theme_name": o.get("theme_name", ""),
                    "technical_state": o.get("technical_state", ""),
                    "status": o.get("status", "UNKNOWN"),
                }
            )
        return {
            "tag": "table",
            "page_size": min(len(rows), 20),
            "row_height": "low",
            "columns": columns,
            "rows": rows,
        }

    @staticmethod
    def _build_tasks_table(tasks: list[dict]) -> dict:
        """构造研究任务飞书原生 table 组件。

        Args:
            tasks: research_task_runs 列表

        Returns:
            飞书 table 组件 dict
        """
        columns = [
            {"name": "task_id", "display_name": "任务ID", "data_type": "text"},
            {"name": "status", "display_name": "状态", "data_type": "text"},
            {"name": "evidence_saved", "display_name": "新增证据", "data_type": "number"},
            {"name": "stopped_reason", "display_name": "终止原因", "data_type": "text"},
        ]
        rows = []
        for t in tasks:
            rows.append(
                {
                    "task_id": t.get("task_id", ""),
                    "status": t.get("status", ""),
                    "evidence_saved": t.get("evidence_saved", 0),
                    "stopped_reason": t.get("stopped_reason", ""),
                }
            )
        return {
            "tag": "table",
            "page_size": min(len(rows), 20),
            "row_height": "low",
            "columns": columns,
            "rows": rows,
        }

    def _send_text_fallback(self, message: str) -> bool:
        """纯文本降级发送。

        Args:
            message: 纯文本消息内容

        Returns:
            bool: 是否发送成功
        """
        try:
            import requests
            request_body = {"msg_type": "text", "content": {"text": message}}
            response = requests.post(self._webhook_url, json=request_body, timeout=10)
            if response.status_code == 200:
                return True
            return False
        except Exception:
            return False
