"""P3.9 未来信息泄漏检查器。

按 P3.9 硬门槛「无已知未来泄漏」：
- 财报数据使用发布日期而非期末日期
- 分析师预期使用快照日期
- 行业数据使用可用日期
- 模型版本是否在当时有效

设计要点：
- 复用 core.research.earnings.pit 的时间过滤逻辑
- 每个输入项检查 visible_at 是否晚于 pit_time
- 模型版本检查 effective_from/effective_to 是否覆盖 pit_time
- 返回详细泄漏项列表，不抛异常
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from core.research.pit_certification.contracts import PITLeakageCheck


def _parse_time(value: str) -> datetime | None:
    """解析 ISO 时间字符串为 UTC datetime。

    Args:
        value: ISO 时间字符串

    Returns:
        datetime | None: 解析失败返回 None
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def check_future_leakage(
    symbol: str,
    pit_time: str,
    inputs: dict[str, Any],
    model_version_info: dict[str, Any] | None = None,
) -> PITLeakageCheck:
    """检查输入是否包含未来信息。

    检查项：
    1. financial_reports: 每条财报的 published_at 不得晚于 pit_time
    2. analyst_expectations: 每条预期的 snapshot_date 不得晚于 pit_time
    3. industry_data: 每条行业数据的 available_at 不得晚于 pit_time
    4. model_version: 模型版本在 pit_time 时必须有效

    Args:
        symbol: 公司代码
        pit_time: PIT 检查时点（ISO）
        inputs: 输入数据字典，可含以下键：
            - financial_reports: list[dict]，每项含 published_at/period_end
            - analyst_expectations: list[dict]，每项含 snapshot_date
            - industry_data: list[dict]，每项含 available_at
        model_version_info: 模型版本信息，含 effective_from/effective_to/version

    Returns:
        PITLeakageCheck: 泄漏检查结果
    """
    inputs_map = inputs if isinstance(inputs, Mapping) else {}
    cutoff = _parse_time(pit_time)
    if cutoff is None:
        return PITLeakageCheck(
            symbol=symbol,
            pit_time=pit_time,
            has_leakage=False,
            leakage_items=[],
            checked_count=0,
            blocked_count=0,
        )

    leakage_items: list[dict[str, Any]] = []
    checked_count = 0

    # 1. 检查财报数据：必须使用发布日期，缺失即视为泄漏
    # P3 规则：时间未知数据不得进入历史测算；period_end 不可替代 published_at
    for report in _records(inputs_map.get("financial_reports")):
        checked_count += 1
        published_at = str(report.get("published_at", ""))
        visible_at = _parse_time(published_at)
        if visible_at is None:
            leakage_items.append({
                "category": "financial_report",
                "field": "published_at",
                "reason": "财报缺少发布日期，时间未知数据不得进入历史测算",
                "item": report,
            })
            continue
        if visible_at > cutoff:
            leakage_items.append({
                "category": "financial_report",
                "field": "published_at",
                "expected": f"<= {pit_time}",
                "actual": published_at,
                "reason": f"财报发布日期 {published_at} 晚于 PIT 时点 {pit_time}",
                "item": report,
            })

    # 2. 检查分析师预期：使用快照日期
    for expectation in _records(inputs_map.get("analyst_expectations")):
        checked_count += 1
        snapshot_date = str(expectation.get("snapshot_date", ""))
        visible_at = _parse_time(snapshot_date)
        if visible_at is None:
            leakage_items.append({
                "category": "analyst_expectation",
                "field": "snapshot_date",
                "reason": "分析师预期缺少快照日期，时间未知数据不得进入历史测算",
                "item": expectation,
            })
            continue
        if visible_at > cutoff:
            leakage_items.append({
                "category": "analyst_expectation",
                "field": "snapshot_date",
                "expected": f"<= {pit_time}",
                "actual": snapshot_date,
                "reason": f"分析师预期快照日期 {snapshot_date} 晚于 PIT 时点 {pit_time}",
                "item": expectation,
            })

    # 3. 检查行业数据：使用可用日期
    for industry in _records(inputs_map.get("industry_data")):
        checked_count += 1
        available_at = str(industry.get("available_at", ""))
        visible_at = _parse_time(available_at)
        if visible_at is None:
            leakage_items.append({
                "category": "industry_data",
                "field": "available_at",
                "reason": "行业数据缺少可用日期，时间未知数据不得进入历史测算",
                "item": industry,
            })
            continue
        if visible_at > cutoff:
            leakage_items.append({
                "category": "industry_data",
                "field": "available_at",
                "expected": f"<= {pit_time}",
                "actual": available_at,
                "reason": f"行业数据可用日期 {available_at} 晚于 PIT 时点 {pit_time}",
                "item": industry,
            })

    # 4. 检查模型版本是否在当时有效
    if model_version_info is not None:
        checked_count += 1
        version_info = model_version_info if isinstance(model_version_info, Mapping) else {}
        version = str(version_info.get("version", ""))
        effective_from = str(version_info.get("effective_from", ""))
        effective_to = str(version_info.get("effective_to", ""))
        from_time = _parse_time(effective_from)
        to_time = _parse_time(effective_to) if effective_to else None

        if from_time is None:
            leakage_items.append({
                "category": "model_version",
                "field": "effective_from",
                "reason": f"模型版本 {version} 缺少 effective_from，无法确认在 PIT 时点有效",
                "item": dict(version_info),
            })
        elif from_time > cutoff:
            leakage_items.append({
                "category": "model_version",
                "field": "effective_from",
                "expected": f"<= {pit_time}",
                "actual": effective_from,
                "reason": f"模型版本 {version} 生效日期 {effective_from} 晚于 PIT 时点 {pit_time}",
                "item": dict(version_info),
            })
        elif to_time is not None and to_time <= cutoff:
            leakage_items.append({
                "category": "model_version",
                "field": "effective_to",
                "expected": f"> {pit_time}",
                "actual": effective_to,
                "reason": f"模型版本 {version} 失效日期 {effective_to} 早于或等于 PIT 时点 {pit_time}",
                "item": dict(version_info),
            })

    blocked_count = len(leakage_items)
    return PITLeakageCheck(
        symbol=symbol,
        pit_time=pit_time,
        has_leakage=blocked_count > 0,
        leakage_items=leakage_items,
        checked_count=checked_count,
        blocked_count=blocked_count,
    )
