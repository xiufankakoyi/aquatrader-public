"""有效期分域配置。

P4.3: 有效期由 Claim 类型/Evidence 类型/来源声明/适用政策版本决定，
不在代码中统一写死天数。无法确定时 valid_until=UNKNOWN。

有效期来源优先级：
1. 来源声明（公告/财报明确标注的有效期）
2. Claim 类型默认（按业务语义分域配置）
3. Evidence 类型默认（按来源类型分域配置）
4. UNKNOWN（无法确定时不设过期时间）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import _as_mapping, _safe_int, _safe_text


#: Claim 类型 → 默认有效天数映射（None 表示 UNKNOWN）
CLAIM_TYPE_VALIDITY_DAYS: dict[str, int | None] = {
    # 事实型 Claim：长期有效，不设过期
    "business_facts": None,
    "product_facts": None,
    "supply_chain": None,
    # 预测型 Claim：有限有效期
    "earnings_forecast": 90,    # 季度预测，90 天
    "guidance": 180,            # 公司指引，半年
    # 事件型 Claim：短期有效
    "event_impact": 30,         # 事件影响，30 天
    "policy_impact": 90,        # 政策影响，90 天
    # 状态型 Claim：中期有效
    "market_position": 180,     # 市场地位，半年
    "technology_readiness": 365,  # 技术成熟度，一年
}

#: Evidence 来源类型 → 默认有效天数映射
EVIDENCE_TYPE_VALIDITY_DAYS: dict[str, int | None] = {
    # 第一方来源：长期可信
    "announcement": None,       # 公告：长期有效
    "annual_report": None,      # 年报：长期有效
    "quarterly_report": 180,    # 季报：半年
    "interactive_platform": 90,  # 互动平台：90 天
    "company_website": 180,     # 官网：半年
    # 二手来源：有限有效
    "research_report": 90,      # 研报：90 天
    "news": 7,                  # 新闻：7 天
    # 市场数据：不作为事实 Evidence，仅作特征
    "market_data": 1,           # 行情：1 天
}


def compute_valid_until(
    *,
    claim_type: str = "",
    evidence_type: str = "",
    source_declared_validity: str = "",
    published_at: str = "",
    policy_version: str = "",
    custom_config: dict[str, Any] | None = None,
) -> str:
    """计算有效截止时间。

    优先级：
    1. 来源声明的有效期（source_declared_validity）
    2. Claim 类型默认有效期
    3. Evidence 类型默认有效期
    4. UNKNOWN（返回空串）

    Args:
        claim_type: Claim 类型键。
        evidence_type: Evidence 来源类型键。
        source_declared_validity: 来源明确声明的有效期（ISO 日期）。
        published_at: 发布时间（ISO 日期），用于计算相对有效期。
        policy_version: 策略版本（预留，未来可按版本覆盖配置）。
        custom_config: 自定义配置覆盖（键为类型，值为天数或 None）。

    Returns:
        有效截止时间 ISO 字符串，UNKNOWN 时返回空串。
    """
    # 1. 来源声明优先
    declared = _safe_text(source_declared_validity)
    if declared:
        try:
            _parse_timestamp(declared)
        except ValueError:
            return ""
        return declared

    # 合并自定义配置
    claim_config = {**CLAIM_TYPE_VALIDITY_DAYS}
    evidence_config = {**EVIDENCE_TYPE_VALIDITY_DAYS}
    custom_config = _as_mapping(custom_config)
    claim_config.update(_normalized_config(custom_config.get("claim_types")))
    evidence_config.update(_normalized_config(custom_config.get("evidence_types")))

    # 2. Claim 类型默认
    claim_type = _safe_text(claim_type).lower()
    evidence_type = _safe_text(evidence_type).lower()
    days = claim_config.get(claim_type)
    # 3. Evidence 类型默认
    if days is None and evidence_type:
        days = evidence_config.get(evidence_type)

    # 4. UNKNOWN
    days = _safe_int(days, -1)
    if days < 0:
        return ""

    # 基于 published_at 计算绝对过期时间
    published_at = _safe_text(published_at)
    if not published_at:
        return ""

    try:
        expires = _parse_timestamp(published_at) + timedelta(days=days)
        return expires.isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError):
        return ""


def _normalized_config(value: Any) -> dict[str, Any]:
    return {
        _safe_text(key).lower(): item
        for key, item in _as_mapping(value).items()
        if _safe_text(key)
    }


def _parse_timestamp(value: str) -> datetime:
    text = _safe_text(value)
    if not text:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_validity_configurable(claim_type: str = "", evidence_type: str = "") -> bool:
    """判断给定类型是否有可配置的有效期。

    Args:
        claim_type: Claim 类型键。
        evidence_type: Evidence 来源类型键。

    Returns:
        True 表示有明确的有效期配置（非 UNKNOWN）。
    """
    days = CLAIM_TYPE_VALIDITY_DAYS.get(_safe_text(claim_type).lower())
    if days is None and evidence_type:
        days = EVIDENCE_TYPE_VALIDITY_DAYS.get(_safe_text(evidence_type).lower())
    return days is not None
