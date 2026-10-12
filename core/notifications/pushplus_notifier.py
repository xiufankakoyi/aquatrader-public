"""PushPlus 微信/QQ 推送通道。

PushPlus 是第三方免费推送服务（https://www.pushplus.plus/），
登录后绑定微信即可获得 token，调用 HTTP API 推送消息直达微信。

适用场景：个人使用，直接推送到微信，比飞书卡片配置更简单。
API 文档：https://www.pushplus.plus/doc/

消息格式：
- title: 通知标题（≤ 30 字符）
- content: markdown 内容
- template: html / markdown / json / txt
"""
from __future__ import annotations

import json
from typing import Optional

import requests

from config.logger import get_logger
from core.notifications.models import NotificationPayload

logger = get_logger(__name__)

# PushPlus 推送 API 端点
PUSHPLUS_API_URL = "https://www.pushplus.plus/send"

# PushPlus 标题字符上限（API 实际限制 30，留余量）
PUSHPLUS_TITLE_MAX = 30

# PushPlus content 字符上限（官方未明确文档化，超长会失败）
PUSHPLUS_CONTENT_MAX = 30000

# 技术状态代号 → 口语化解释
_TECH_STATE_PLAIN: dict[str, str] = {
    "ACCELERATION_RISK": "短期涨幅过大，有回调风险",
    "TRENDING": "处于上升趋势中",
    "MA5_HOLD": "在5日均线附近震荡，短期走势偏强",
    "MA10_RETEST": "回踩10日均线，短期可能整理",
    "MA20_RETEST": "回踩20日均线，中线支撑位测试",
    "MA30_RETEST": "回踩30日均线，中线趋势关键位",
    "NEUTRAL": "走势中性，没有明显方向",
}


class PushPlusNotifier:
    """PushPlus 通知发送器。

    通过 PushPlus HTTP API 推送消息到绑定微信/QQ。
    未配置 PUSHPLUS_TOKEN 时仅记录日志（桩模式），不影响主流程。

    Usage:
        notifier = PushPlusNotifier()
        notifier.send(payload)
    """

    def __init__(self) -> None:
        """初始化 PushPlus 通知器。"""
        self._token: Optional[str] = None
        try:
            from config.config import Config
            self._token = Config.PUSHPLUS_TOKEN
        except Exception:
            pass

        # 兼容 secrets.py 中命名为 Plus_TOKEN 的情况
        if not self._token:
            try:
                from config import secrets
                secrets_obj = getattr(secrets, "Secrets", secrets)
                for alias in ("Plus_TOKEN", "PLUS_TOKEN", "PUSH_PLUS_TOKEN"):
                    value = getattr(secrets_obj, alias, None)
                    if value:
                        self._token = value
                        break
            except Exception:
                pass

        if self._token:
            logger.info("PushPlus 通知器已配置 token")
        else:
            logger.info("PushPlus 通知器未配置 token，将使用桩模式")

    def send(self, payload: NotificationPayload) -> bool:
        """发送通知到 PushPlus。

        Args:
            payload: 通知消息

        Returns:
            bool: 是否发送成功
        """
        if not self._token:
            logger.debug(f"PushPlus 未配置 token，跳过推送: {payload.event_type}")
            return False

        title = payload.title or payload.event_type
        if len(title) > PUSHPLUS_TITLE_MAX:
            title = title[:PUSHPLUS_TITLE_MAX]

        content, template = self._build_content(payload)
        if len(content) > PUSHPLUS_CONTENT_MAX:
            content = content[:PUSHPLUS_CONTENT_MAX] + "\n\n...（已截断）"

        try:
            response = requests.post(
                PUSHPLUS_API_URL,
                headers={"Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "token": self._token,
                        "title": title,
                        "content": content,
                        "template": template,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                timeout=10,
            )
            response_data = response.json()
            if response.status_code == 200 and response_data.get("code") == 200:
                logger.info(f"PushPlus 推送成功: {payload.event_type}")
                return True
            logger.warning(f"PushPlus 推送失败: {response_data}")
            return False
        except Exception as e:
            logger.error(f"PushPlus 推送异常: {e}", exc_info=True)
            return False

    def _build_content(self, payload: NotificationPayload) -> tuple[str, str]:
        """构造 PushPlus 内容并决定模板类型。

        优先级：
        1. payload.data.html_content → HTML 模板（含分时图）
        2. payload.data 结构化数据 → 口语化充实表达（个人使用推荐）
        3. payload.data.markdown_content → 完整报告
        4. payload.summary 短文本

        Args:
            payload: 通知消息

        Returns:
            (content, template) 元组，template 为 "html" 或 "markdown"。
        """
        if isinstance(payload.data, dict):
            html = payload.data.get("html_content") or ""
            if isinstance(html, str) and html.strip():
                return html, "html"

        if isinstance(payload.data, dict):
            observations = payload.data.get("observations") or []
            themes = payload.data.get("themes") or []
            if observations or themes:
                return self._build_brief_markdown(payload, themes, observations), "markdown"

        if isinstance(payload.data, dict):
            raw_md = payload.data.get("markdown_content") or ""
            if isinstance(raw_md, str) and raw_md.strip():
                return raw_md, "markdown"

        return payload.summary, "markdown"

    @staticmethod
    def _plain_tech_state(code: str) -> str:
        """把技术状态代号翻译成口语化描述。

        Args:
            code: 技术状态代号（如 ACCELERATION_RISK）

        Returns:
            口语化描述字符串
        """
        return _TECH_STATE_PLAIN.get(code, code)

    @staticmethod
    def _format_pct(value) -> str:
        """把小数格式化为百分比字符串。

        Args:
            value: 小数值（如 0.171）

        Returns:
            百分比字符串（如 "17.10%"），非数值返回 "-"
        """
        if isinstance(value, (int, float)):
            return f"{value * 100:.2f}%"
        return "-"

    @classmethod
    def _build_brief_markdown(
        cls,
        payload: NotificationPayload,
        themes: list[dict],
        observations: list[dict],
    ) -> str:
        """构造口语化充实版推送内容。

        个人使用场景：直白描述观察名单和技术面，说明待验证原因。

        Args:
            payload: 通知消息
            themes: 主题列表
            observations: 观察列表

        Returns:
            口语化 markdown 字符串
        """
        lines: list[str] = []

        # 顶部：直接说话
        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
            payload.priority, "⚪"
        )
        lines.append(f"{priority_emoji} **{payload.title}**")
        lines.append("")
        lines.append(payload.summary)
        lines.append("")

        # 观察名单（直接推标的，口语化描述）
        watch_obs = [o for o in observations if o.get("research_validated")]
        if watch_obs:
            lines.append("## 观察名单")
            lines.append("")
            for o in watch_obs:
                symbol = o.get("symbol", "")
                company = o.get("company_name") or "暂无本地证据"
                theme = o.get("theme_name", "")
                tech = o.get("technical_state", "")
                mf = o.get("market_features") or {}
                ret5 = mf.get("ret5")
                ret20 = mf.get("ret20")
                close = mf.get("close")

                # 口语化描述每只标的
                line = f"**{symbol}** {company}"
                if theme:
                    line += f"｜{theme}"
                lines.append(f"- {line}")

                # 技术面用口语解释
                tech_parts: list[str] = []
                if tech:
                    tech_parts.append(cls._plain_tech_state(tech))
                if isinstance(ret5, (int, float)) and isinstance(ret20, (int, float)):
                    tech_parts.append(
                        f"近5天涨了 {_cls_format_pct(ret5)}，近20天涨了 {_cls_format_pct(ret20)}"
                    )
                if isinstance(close, (int, float)):
                    tech_parts.append(f"最新收盘价 {close:.2f} 元")
                if tech_parts:
                    lines.append(f"  - 技术面：{'，'.join(tech_parts)}")
                lines.append("")
        else:
            lines.append("## 观察名单")
            lines.append("")
            lines.append("今天没有通过 Research 验证的标的。")
            lines.append("")

        # 主题分析：已验证 vs 待验证，重点说明待验证原因
        if themes:
            validated = [t for t in themes if t.get("research_gate_status") == "validated"]
            pending = [t for t in themes if t.get("research_gate_status") != "validated"]

            if validated:
                lines.append("## 已验证主题")
                lines.append("")
                for t in validated:
                    name = t.get("theme_name", "")
                    ttype = t.get("theme_type", "")
                    core_count = t.get("validated_core_count", 0)
                    type_desc = "反转信号" if "reversal" in ttype else "研究发现"
                    lines.append(
                        f"- **{name}**（{type_desc}）：已找到 {core_count} 个核心标的通过验证"
                    )
                lines.append("")

            if pending:
                lines.append(f"## 待验证主题（{len(pending)} 个）")
                lines.append("")
                lines.append(
                    "以下主题虽然被行情扫描识别为动态主题，"
                    "但 Research 流水线还没走完验证流程，"
                    "暂时无法确认成分股的业务关联是否成立："
                )
                lines.append("")
                for t in pending:
                    name = t.get("theme_name", "")
                    ttype = t.get("theme_type", "")
                    negative = t.get("negative_status", "clear")
                    type_desc = "反转信号" if "reversal" in ttype else "研究发现"
                    warning = ""
                    if negative and negative != "clear":
                        warning = f"（注意：检测到{negative}负面信号）"
                    lines.append(f"- **{name}**（{type_desc}）{warning}")
                lines.append("")

                # 说明待验证的根本原因
                tasks = payload.data.get("research_task_runs") if isinstance(payload.data, dict) else None
                if tasks:
                    lines.append("**为什么还在等？**")
                    lines.append("")
                    for t in tasks:
                        tid = t.get("task_id", "")
                        status = t.get("status", "")
                        ev = t.get("evidence_saved", 0)
                        reason = t.get("stopped_reason", "")
                        if status == "succeeded":
                            lines.append(
                                f"- {tid}：已完成，新增证据 {ev} 条，"
                                f"Claim 已满足充分性条件"
                            )
                        elif status == "partial":
                            lines.append(
                                f"- {tid}：部分完成，新增证据 {ev} 条，"
                                f"但证据仍不充分"
                            )
                            if reason:
                                lines.append(f"  - 终止原因：{reason}")
                        else:
                            lines.append(
                                f"- {tid}：状态={status}，新增证据 {ev} 条"
                            )
                    lines.append("")
                    # 给出整体诊断
                    all_queued = all(t.get("status") == "queued" for t in tasks)
                    all_partial = all(t.get("status") == "partial" for t in tasks)
                    if all_queued:
                        lines.append(
                            "诊断：研究任务都还在排队中，没有真正开始执行。"
                            "这通常意味着任务调度器没在运行，或者预算/并发限制导致任务积压。"
                            "需要检查 monitor 的 task runner 是否启动。"
                        )
                    elif all_partial:
                        lines.append(
                            "诊断：研究任务执行了但证据不充分。"
                            "现有证据主要来自板块成分结构化数据（如\"所属概念\"字段），"
                            "缺少公告、研报、新闻等实质性证据。"
                            "可能原因：搜索源不可达、关键词命中率低、"
                            "或 LLM 提取器未从抓取的网页中识别出有效证据。"
                        )
                    lines.append("")

        # 底部免责声明
        lines.append("---")
        lines.append("_以上内容仅用于研究观察与后续验证，不构成买入、卖出、仓位、止盈或止损建议。_")

        return "\n".join(lines)


def _cls_format_pct(value) -> str:
    """模块级辅助函数：格式化百分比（供 _build_brief_markdown 类方法使用）。"""
    if isinstance(value, (int, float)):
        return f"{value * 100:.2f}%"
    return "-"
