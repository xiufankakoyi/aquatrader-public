"""PushPlus 通知通道单元测试。

覆盖：
1. 未配置 token 时返回 False（桩模式）
2. 配置 token 后调用 HTTP API 成功
3. content 优先级：markdown_content > 结构化数据 > summary
4. 超长标题截断到 30 字符
5. 超长 content 截断到安全阈值
6. 精简标的列表构造正确
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from core.notifications.models import NotificationPayload
from core.notifications.pushplus_notifier import (
    PUSHPLUS_CONTENT_MAX,
    PUSHPLUS_TITLE_MAX,
    PushPlusNotifier,
)


def _make_notifier(token: str = "test_token_abc123") -> PushPlusNotifier:
    """构造带 token 的 PushPlusNotifier 实例。"""
    notifier = PushPlusNotifier()
    notifier._token = token
    return notifier


def test_pushplus_no_token_returns_false():
    """未配置 token 时 send 应返回 False 并跳过推送。"""
    notifier = PushPlusNotifier()
    notifier._token = None
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="test",
        summary="summary",
        priority="low",
    )
    assert notifier.send(payload) is False


def test_pushplus_send_success():
    """配置 token 且 API 返回 code=200 时 send 应返回 True。"""
    notifier = _make_notifier()
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="AquaTrade 盘前研究复盘",
        summary="动态主题 10 个",
        priority="medium",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 200, "msg": "success"}

    with patch("core.notifications.pushplus_notifier.requests.post", return_value=mock_response) as mock_post:
        result = notifier.send(payload)

    assert result is True
    # 验证 POST 调用参数
    args, kwargs = mock_post.call_args
    assert args[0] == "https://www.pushplus.plus/send"
    body = json.loads(kwargs["data"].decode("utf-8"))
    assert body["token"] == "test_token_abc123"
    assert body["title"] == "AquaTrade 盘前研究复盘"
    assert body["template"] == "markdown"
    assert body["content"] == "动态主题 10 个"


def test_pushplus_send_api_error_returns_false():
    """API 返回非 200 code 时 send 应返回 False。"""
    notifier = _make_notifier()
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="test",
        summary="s",
        priority="low",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 903, "msg": "token invalid"}

    with patch("core.notifications.pushplus_notifier.requests.post", return_value=mock_response):
        result = notifier.send(payload)

    assert result is False


def test_pushplus_title_truncated_to_max():
    """标题超长时截断到 PUSHPLUS_TITLE_MAX 字符。"""
    notifier = _make_notifier()
    long_title = "A" * 50
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title=long_title,
        summary="s",
        priority="low",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 200}

    with patch("core.notifications.pushplus_notifier.requests.post", return_value=mock_response) as mock_post:
        notifier.send(payload)

    body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
    assert len(body["title"]) == PUSHPLUS_TITLE_MAX


def test_pushplus_content_uses_markdown_content_first():
    """data.markdown_content 存在时优先使用完整 markdown 作为 content。"""
    notifier = _make_notifier()
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="test",
        summary="短摘要",
        priority="medium",
        data={"markdown_content": "# 完整报告\n| 主题 |\n|---|\n| 先进封装 |"},
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 200}

    with patch("core.notifications.pushplus_notifier.requests.post", return_value=mock_response) as mock_post:
        notifier.send(payload)

    body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
    assert "# 完整报告" in body["content"]
    assert "先进封装" in body["content"]


def test_pushplus_content_uses_structured_data_when_no_markdown():
    """无 markdown_content 但有结构化数据时构造口语化充实版推送。"""
    notifier = _make_notifier()
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="AquaTrade 盘前研究复盘 2026-07-15",
        summary="动态主题 2 个，观察名单 1 个",
        priority="medium",
        data={
            "themes": [
                {
                    "theme_name": "先进封装",
                    "theme_type": "ice_reversal",
                    "research_gate_status": "validated",
                    "validated_core_count": 2,
                },
                {
                    "theme_name": "创新药",
                    "theme_type": "research_discovery",
                    "research_gate_status": "pending",
                    "negative_status": "clear",
                },
            ],
            "observations": [
                {
                    "symbol": "002185.SZ",
                    "company_name": "华天科技",
                    "theme_name": "先进封装",
                    "status": "WATCH",
                    "technical_state": "ACCELERATION_RISK",
                    "research_validated": True,
                    "market_features": {
                        "leadership_score": 0.9545,
                        "ret5": 0.171,
                        "ret20": 0.4991,
                        "close": 25.30,
                    },
                },
                {
                    "symbol": "000063.SZ",
                    "company_name": "",
                    "theme_name": "数字经济",
                    "status": "UNKNOWN",
                    "technical_state": "MA5_HOLD",
                    "research_validated": False,
                    "market_features": {},
                },
            ],
            "research_task_runs": [
                {
                    "task_id": "rt_xxx",
                    "status": "queued",
                    "evidence_saved": 0,
                }
            ],
        },
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 200}

    with patch("core.notifications.pushplus_notifier.requests.post", return_value=mock_response) as mock_post:
        notifier.send(payload)

    body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
    content = body["content"]

    # 必须包含观察名单标的
    assert "002185.SZ" in content
    assert "华天科技" in content
    assert "先进封装" in content

    # 必须包含口语化的技术面描述（不是代号）
    assert "短期涨幅过大，有回调风险" in content
    assert "近5天涨了 17.10%" in content
    assert "近20天涨了 49.91%" in content
    assert "最新收盘价 25.30 元" in content

    # 不应包含领导分数（用户认为没用）
    assert "领导分" not in content
    assert "leadership_score" not in content

    # 不应包含原始技术状态代号作为裸字符串
    assert "ACCELERATION_RISK" not in content

    # 必须用"观察名单"而非"WATCH 名单"
    assert "观察名单" in content
    assert "WATCH 名单" not in content

    # 必须包含已验证主题
    assert "已验证主题" in content
    assert "反转信号" in content  # ice_reversal → 反转信号

    # 必须包含待验证主题计数和原因说明
    assert "待验证主题（1 个）" in content
    assert "创新药" in content
    assert "研究发现" in content  # research_discovery → 研究发现

    # 必须包含"为什么还在等"诊断
    assert "为什么还在等" in content
    assert "rt_xxx" in content

    # queued 状态必须有排队诊断
    assert "排队中" in content
    assert "调度器" in content

    # 不应包含非 validated 的探索观察
    assert "000063.SZ" not in content


def test_pushplus_content_truncated_when_too_long():
    """content 超长时截断到 PUSHPLUS_CONTENT_MAX 并附加截断标记。"""
    notifier = _make_notifier()
    # 构造超长 markdown_content
    long_md = "x" * (PUSHPLUS_CONTENT_MAX + 1000)
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="test",
        summary="s",
        priority="low",
        data={"markdown_content": long_md},
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 200}

    with patch("core.notifications.pushplus_notifier.requests.post", return_value=mock_response) as mock_post:
        notifier.send(payload)

    body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
    # 应截断到 PUSHPLUS_CONTENT_MAX + 截断标记
    assert len(body["content"]) < PUSHPLUS_CONTENT_MAX + 100
    assert "已截断" in body["content"]


def test_pushplus_content_falls_back_to_summary():
    """无 markdown_content 也无结构化数据时退回 summary。"""
    notifier = _make_notifier()
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="test",
        summary="纯短摘要",
        priority="low",
        data={"report_path": "/tmp/x.md"},
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"code": 200}

    with patch("core.notifications.pushplus_notifier.requests.post", return_value=mock_response) as mock_post:
        notifier.send(payload)

    body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
    assert body["content"] == "纯短摘要"


def test_pushplus_exception_returns_false():
    """HTTP 异常时 send 应返回 False。"""
    notifier = _make_notifier()
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="test",
        summary="s",
        priority="low",
    )

    with patch("core.notifications.pushplus_notifier.requests.post", side_effect=Exception("network error")):
        result = notifier.send(payload)

    assert result is False
