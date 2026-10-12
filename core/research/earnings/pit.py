"""Point-in-time 过滤：未来财报、未来股本和未来输入在历史 as_of 下不可见。

时间未知数据不得进入历史测算。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .contracts import ImpactAssumption, ShareCapitalVersion


def parse_time(value: str) -> datetime | None:
    """解析 ISO 时间，失败返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def input_visible_as_of(item: ImpactAssumption, as_of: str, *, historical: bool = True) -> bool:
    """判断假设在 as_of 时点是否可见。

    历史模式（historical=True）下：
    - as_of 为空 → False（时间未知数据不得进入历史测算）
    - retrieved_at/published_at/available_at 晚于 as_of → 不可见
    - retrieved_at/published_at 缺失且 available_at 缺失 → 不可见（时间未知）
    - expires_at <= as_of → 已过期，不可见

    实时模式（historical=False）下：允许但由调用方标记。

    Args:
        item: 计算假设。
        as_of: PIT 时点（ISO）。
        historical: 是否历史模式。
    """
    if not isinstance(item, ImpactAssumption):
        return False
    cutoff = parse_time(as_of)
    if cutoff is None:
        return not historical

    published = parse_time(item.published_at)
    retrieved = parse_time(item.retrieved_at)
    available = parse_time(item.available_at)
    expires = parse_time(item.expires_at)

    visible_at = retrieved or published or available
    if visible_at is None:
        # 时间未知数据不得进入历史测算
        return not historical
    if visible_at > cutoff:
        return False
    if published is not None and published > cutoff:
        return False
    if available is not None and available > cutoff:
        return False
    if expires is not None and expires <= cutoff:
        return False
    return True


def capital_visible_as_of(item: ShareCapitalVersion, as_of: str, *, historical: bool = True) -> bool:
    """判断股本版本在 as_of 时点是否可见。

    未来股本版本在历史 as_of 下不可见。
    优先以 published_at（披露时点）作为可见性判据；其次 capital_as_of（生效时点）；
    最后 retrieved_at（采集时点）。时间未知时历史模式排除。
    """
    if not isinstance(item, ShareCapitalVersion):
        return False
    cutoff = parse_time(as_of)
    if cutoff is None:
        return not historical
    published = parse_time(item.published_at)
    capital_as_of = parse_time(item.as_of)
    retrieved = parse_time(item.retrieved_at)
    visible_at = published or capital_as_of or retrieved
    if visible_at is None:
        return not historical
    if visible_at > cutoff:
        return False
    if published is not None and published > cutoff:
        return False
    if capital_as_of is not None and capital_as_of > cutoff:
        return False
    return True


def filter_inputs_as_of(
    inputs: Iterable[ImpactAssumption],
    as_of: str,
    *,
    historical: bool = True,
) -> tuple[list[ImpactAssumption], int]:
    """过滤假设，返回 (可见输入, 被拦截数)。"""
    visible: list[ImpactAssumption] = []
    blocked = 0
    try:
        iterator = iter(inputs)
    except TypeError:
        return visible, 0
    for item in iterator:
        if input_visible_as_of(item, as_of, historical=historical):
            visible.append(item)
        else:
            blocked += 1
    return visible, blocked


def filter_capitals_as_of(
    capitals: Iterable[ShareCapitalVersion],
    as_of: str,
    *,
    historical: bool = True,
) -> tuple[list[ShareCapitalVersion], int]:
    """过滤股本版本，返回 (可见版本, 被拦截数)。"""
    visible: list[ShareCapitalVersion] = []
    blocked = 0
    try:
        iterator = iter(capitals)
    except TypeError:
        return visible, 0
    for item in iterator:
        if capital_visible_as_of(item, as_of, historical=historical):
            visible.append(item)
        else:
            blocked += 1
    return visible, blocked
