"""召回与事实确认分离的数据契约。

- MembershipEvidence: 公司—主题关系的事实证据，必须来自可定位材料
  （公告/互动/官网/研报），龙虎榜、K线、成交数据不得进入。
- ThemeMemberCandidate: 外部概念表（问财/东财/同花顺）召回的候选成员，
  仅作为未确认候选，需经事实证据确认后方可进入正式视图。
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。"""
    return f"{prefix}_{uuid4().hex[:16]}"


# 成员关系状态（替代 0.3/0.5 硬编码置信度语义）
# 召回候选默认状态：只进入探索视图，不进入正式成员
MEMBERSHIP_STATUS_RECALL_ONLY = "recall_only"
# 已落地状态：有可定位材料支撑的 grounded support 证据
MEMBERSHIP_STATUS_GROUNDED = "grounded"
# 冲突状态：存在 counter 证据或冲突材料
MEMBERSHIP_STATUS_CONFLICTED = "conflicted"
# 过期状态：证据已过期
MEMBERSHIP_STATUS_EXPIRED = "expired"

# 兼容旧代码的别名（deprecated，新代码应使用 MEMBERSHIP_STATUS_*）
RECALL_CONFIDENCE = 0.0  # shadow only，不再用于生产语义
CONFIRMED_CONFIDENCE = 0.0  # shadow only，不再用于生产语义

# grounding_status 取值
GROUNDING_GROUNDED = "grounded"  # 已落地（有可定位材料支撑）
GROUNDING_UNVERIFIED = "unverified"  # 未验证
GROUNDING_CONFLICTED = "conflicted"  # 冲突

# stance 取值
STANCE_SUPPORT = "support"
STANCE_COUNTER = "counter"
STANCE_NEUTRAL = "neutral"


@dataclass
class MembershipEvidence:
    """公司—主题关系的事实证据。

    每条证据必须可追溯到可定位材料（公告/互动/官网/研报），
    龙虎榜、K线、成交数据不得作为事实 Evidence。

    Attributes:
        evidence_id: 证据 ID（自动生成）。
        symbol: 公司代码。
        company_id: 公司 ID。
        theme_id: 主题 ID。
        stance: 立场（support/counter/neutral）。
        source_type: 来源类型（announcement/qa/website/research_report）。
        provider: 提供方（cninfo/sse_irm/szse_irm/company_website/research）。
        source_url: 来源 URL。
        document_id: 文档 ID。
        document_version_id: 文档版本 ID。
        quote: 原文引用。
        locator: 原文定位（字段/偏移等）。
        published_at: 发布时间（UNKNOWN 时为空）。
        retrieved_at: 抓取时间。
        valid_from: 有效起始时间。
        valid_until: 有效截止时间（UNKNOWN 时为空）。
        lineage_id: 血缘 ID。
        grounding_status: 落地状态（grounded/unverified/conflicted）。
        policy_version: 策略版本。
        confidence: 置信度 0-1。
    """

    evidence_id: str = field(default_factory=lambda: new_id("evd"))
    symbol: str = ""
    company_id: str = ""
    theme_id: str = ""
    stance: str = STANCE_SUPPORT
    source_type: str = "announcement"
    provider: str = "cninfo"
    source_url: str = ""
    document_id: str = ""
    document_version_id: str = ""
    quote: str = ""
    locator: dict[str, Any] = field(default_factory=dict)
    published_at: str = ""
    retrieved_at: str = field(default_factory=utc_now)
    valid_from: str = ""
    valid_until: str = ""
    lineage_id: str = ""
    grounding_status: str = GROUNDING_UNVERIFIED
    policy_version: str = ""
    confidence: float = 0.0

    def __post_init__(self) -> None:
        self.evidence_id = self.evidence_id or new_id("evd")
        self.locator = dict(self.locator) if isinstance(self.locator, Mapping) else {}
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError, OverflowError):
            confidence = 0.0
        self.confidence = min(1.0, max(0.0, confidence)) if math.isfinite(confidence) else 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。locator 以 dict 原样输出。"""
        result: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            result[f.name] = value
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MembershipEvidence:
        """从字典反序列化，忽略未知字段。"""
        allowed = {item.name for item in fields(cls)}
        kwargs: dict[str, Any] = {}
        source = data if isinstance(data, Mapping) else {}
        for key, value in source.items():
            if key not in allowed:
                continue
            kwargs[key] = value
        return cls(**kwargs)


@dataclass
class ThemeMemberCandidate:
    """外部概念表召回的候选成员（未确认）。

    由问财/东财/同花顺等外部概念表召回，仅作为候选，
    需经 MembershipEvidence 确认后方可进入正式视图。
    未确认时 membership_status 为 recall_only，只进入探索视图。
    confidence 字段仅作为 shadow 分数，不用于生产门禁。

    Attributes:
        symbol: 公司代码。
        theme_id: 主题 ID。
        source_provider: 来源提供方（wencai/eastmoney/ths/efinance）。
        source_board_name: 来源板块名称。
        fetched_at: 抓取时间。
        membership_status: 成员关系状态（recall_only/grounded/conflicted/expired）。
        membership_confidence: shadow 分数，不用于生产门禁。
        truncation_info: 截断结构化信息（如总数、返回数等）。
    """

    symbol: str = ""
    theme_id: str = ""
    source_provider: str = "wencai"
    source_board_name: str = ""
    fetched_at: str = field(default_factory=utc_now)
    membership_status: str = MEMBERSHIP_STATUS_RECALL_ONLY
    membership_confidence: float = RECALL_CONFIDENCE
    truncation_info: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.truncation_info = (
            dict(self.truncation_info) if isinstance(self.truncation_info, Mapping) else {}
        )
        try:
            confidence = float(self.membership_confidence)
        except (TypeError, ValueError, OverflowError):
            confidence = 0.0
        self.membership_confidence = (
            min(1.0, max(0.0, confidence)) if math.isfinite(confidence) else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeMemberCandidate:
        """从字典反序列化，忽略未知字段。"""
        allowed = {item.name for item in fields(cls)}
        kwargs: dict[str, Any] = {}
        source = data if isinstance(data, Mapping) else {}
        for key, value in source.items():
            if key not in allowed:
                continue
            kwargs[key] = value
        return cls(**kwargs)
