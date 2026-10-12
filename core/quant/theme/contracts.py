"""动态主题控制面契约层。

定义 ThemePacket 作为 Research 与 QUANT 共享的主题契约。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ThemeCatalyst:
    """主题催化剂事件。"""

    catalyst_id: str = ""
    event_type: str = ""  # news/announcement/research_report/policy
    title: str = ""
    summary: str = ""
    source: str = ""
    source_url: str = ""
    event_date: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "catalyst_id": self.catalyst_id,
            "event_type": self.event_type,
            "title": self.title,
            "summary": self.summary,
            "source": self.source,
            "source_url": self.source_url,
            "event_date": self.event_date,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeCatalyst:
        """从字典反序列化，忽略未知字段以兼容旧数据。"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ThemeMember:
    """主题成员，携带来源与角色信息。"""

    symbol: str = ""
    company_id: str = ""
    company_name: str = ""

    membership_sources: list[str] = field(default_factory=list)
    # eastmoney_concept / ths_concept / news_cluster / market_resonance / research

    membership_reasons: list[str] = field(default_factory=list)
    membership_evidence: list[dict[str, Any]] = field(default_factory=list)
    membership_confidence: float = 0.0
    # 高(0.8+) / 中高(0.6-0.8) / 中(0.4-0.6) / 低(0.2-0.4)

    market_role: str = "secondary"
    # core / leader / secondary / diffusion

    research_claim_ids: list[str] = field(default_factory=list)
    research_claim_status: str = ""
    mapping_confidence: float = 0.0
    evidence_as_of: str = ""
    research_validated_at: str = ""
    market_features: dict[str, Any] = field(default_factory=dict)
    first_seen_at: str = ""
    last_confirmed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "company_id": self.company_id,
            "company_name": self.company_name,
            "membership_sources": self.membership_sources,
            "membership_reasons": self.membership_reasons,
            "membership_evidence": self.membership_evidence,
            "membership_confidence": self.membership_confidence,
            "market_role": self.market_role,
            "research_claim_ids": self.research_claim_ids,
            "research_claim_status": self.research_claim_status,
            "mapping_confidence": self.mapping_confidence,
            "evidence_as_of": self.evidence_as_of,
            "research_validated_at": self.research_validated_at,
            "market_features": self.market_features,
            "first_seen_at": self.first_seen_at,
            "last_confirmed_at": self.last_confirmed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeMember:
        """从字典反序列化，忽略未知字段以兼容旧数据。"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ThemePacket:
    """动态主题包，Research 与 QUANT 共享的主题契约。"""

    theme_run_id: str = ""
    theme_id: str = ""
    theme_name: str = ""
    aliases: list[str] = field(default_factory=list)

    theme_type: str = ""
    # news_persistence / ice_reversal / research_discovery / legacy_reentry

    detected_at: str = ""
    as_of: str = ""
    expires_at: str = ""

    catalysts: list[ThemeCatalyst] = field(default_factory=list)
    members: list[ThemeMember] = field(default_factory=list)
    core_members: list[ThemeMember] = field(default_factory=list)

    # 评分字段
    news_score: float = 0.0
    persistence_score: float = 0.0
    reversal_score: float = 0.0
    breadth_score: float = 0.0
    crowding_score: float = 0.0
    negative_risk_score: float = 0.0

    phase: str = ""
    phase_confidence: float = 0.0
    market_features: dict[str, Any] = field(default_factory=dict)

    source_status: dict[str, str] = field(default_factory=dict)
    source_snapshot_id: str = ""
    strategy_version: str = ""
    trace_id: str = ""

    degraded: bool = False
    degradation_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（含嵌套对象）。"""
        return {
            "theme_run_id": self.theme_run_id,
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "aliases": self.aliases,
            "theme_type": self.theme_type,
            "detected_at": self.detected_at,
            "as_of": self.as_of,
            "expires_at": self.expires_at,
            "catalysts": [c.to_dict() for c in self.catalysts],
            "members": [m.to_dict() for m in self.members],
            "core_members": [m.to_dict() for m in self.core_members],
            "news_score": self.news_score,
            "persistence_score": self.persistence_score,
            "reversal_score": self.reversal_score,
            "breadth_score": self.breadth_score,
            "crowding_score": self.crowding_score,
            "negative_risk_score": self.negative_risk_score,
            "phase": self.phase,
            "phase_confidence": self.phase_confidence,
            "market_features": self.market_features,
            "source_status": self.source_status,
            "source_snapshot_id": self.source_snapshot_id,
            "strategy_version": self.strategy_version,
            "trace_id": self.trace_id,
            "degraded": self.degraded,
            "degradation_reasons": self.degradation_reasons,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemePacket:
        """从字典反序列化，处理嵌套对象并忽略未知字段以兼容旧数据。"""
        catalysts = [ThemeCatalyst.from_dict(c) for c in data.get("catalysts", [])]
        members = [ThemeMember.from_dict(m) for m in data.get("members", [])]
        core_members = [ThemeMember.from_dict(m) for m in data.get("core_members", [])]

        flat = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        flat["catalysts"] = catalysts
        flat["members"] = members
        flat["core_members"] = core_members
        return cls(**flat)


def generate_theme_id(theme_keywords: str, event_entities: str, core_symbols: list[str]) -> str:
    """生成稳定叙事 theme_id，不让成员变化切断历史版本链。

    Args:
        theme_keywords: 主题关键词（如 "HBM"）。
        event_entities: 核心事件实体（如 "AI PC 需求"）。
        core_symbols: 兼容旧调用方保留；成员属于 episode 数据，不再参与身份哈希。

    Returns:
        16 字符的 theme_id，以 "theme_" 前缀。
    """
    del core_symbols
    import re

    normalize = lambda value: re.sub(
        r"[^0-9a-z\u4e00-\u9fa5]+", "", str(value or "").lower()
    )
    raw = f"{normalize(theme_keywords)}|{normalize(event_entities)}"
    return "theme_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def generate_theme_run_id(theme_id: str, as_of: str) -> str:
    """生成 theme_run_id。

    Args:
        theme_id: 主题 ID。
        as_of: 时间戳。

    Returns:
        以 "run_" 前缀的运行 ID。
    """
    raw = f"{theme_id}|{as_of}"
    return "run_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
