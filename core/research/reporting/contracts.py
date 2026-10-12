"""P1.9 CFA 风格研究报告契约。

按 P1 方案 §5 Task P1.9 定义固定 10 章节研究报告结构。
报告按 CFA 公司分析模板组织，每章节独立构建，缺数据时降级 partial，
不输出买入/卖出/仓位/止盈/止损等交易动作建议。

设计要点：
- 固定 10 章节（ReportSection 枚举顺序不可变）
- 每章节带 section_status（ok / partial / missing / not_applicable）
- overall_status = degraded 当多数章节 missing；任一 missing -> partial；全 ok -> ok
- notice 固定提示「研究数据产物，不包含交易动作建议。」
- model_version 与 created_at 用于审计链路
- to_dict / from_dict 支持持久化往返
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def _safe_int(value: Any, default: int = 120) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def utc_now() -> str:
    """返回当前 UTC ISO 时间。

    Returns:
        ISO 8601 格式时间字符串
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。

    Args:
        prefix: ID 前缀，如 "cfar"

    Returns:
        形如 "cfar_xxxxxxxxxxxxxxxx" 的唯一 ID
    """
    return f"{prefix}_{uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ReportSection(str, Enum):
    """CFA 研究报告固定 10 章节。

    顺序与 P1 方案 §5 Task P1.9 一致，不可变更：
    1. 研究范围与 as_of
    2. 公司战略和商业模式
    3. 行业、竞争和产业链
    4. 财务质量与会计调整
    5. 产品驱动和盈利预测
    6. 公司指引、一致预期和研究差异
    7. 估值方法与敏感性
    8. 管理层、治理和资本配置
    9. 重要风险、反证和模型限制
    10. 来源、模型版本和记录留存信息
    """

    SCOPE_AND_AS_OF = "scope_and_as_of"
    STRATEGY_BUSINESS_MODEL = "strategy_business_model"
    INDUSTRY_COMPETITION = "industry_competition"
    FINANCIAL_QUALITY = "financial_quality"
    DRIVERS_AND_FORECAST = "drivers_and_forecast"
    EXPECTATION_DIFFERENCES = "expectation_differences"
    VALUATION_SENSITIVITY = "valuation_sensitivity"
    GOVERNANCE_CAPITAL = "governance_capital"
    RISKS_COUNTEREVIDENCE = "risks_counterevidence"
    SOURCES_AND_VERSIONS = "sources_and_versions"

    @classmethod
    def ordered_values(cls) -> list[str]:
        """返回按枚举定义顺序的 value 列表。"""
        return [member.value for member in cls]


# 章节中文标题映射（与枚举顺序对齐）
SECTION_TITLES: dict[str, str] = {
    ReportSection.SCOPE_AND_AS_OF.value: "1. 研究范围与 as_of",
    ReportSection.STRATEGY_BUSINESS_MODEL.value: "2. 公司战略和商业模式",
    ReportSection.INDUSTRY_COMPETITION.value: "3. 行业、竞争和产业链",
    ReportSection.FINANCIAL_QUALITY.value: "4. 财务质量与会计调整",
    ReportSection.DRIVERS_AND_FORECAST.value: "5. 产品驱动和盈利预测",
    ReportSection.EXPECTATION_DIFFERENCES.value: "6. 公司指引、一致预期和研究差异",
    ReportSection.VALUATION_SENSITIVITY.value: "7. 估值方法与敏感性",
    ReportSection.GOVERNANCE_CAPITAL.value: "8. 管理层、治理和资本配置",
    ReportSection.RISKS_COUNTEREVIDENCE.value: "9. 重要风险、反证和模型限制",
    ReportSection.SOURCES_AND_VERSIONS.value: "10. 来源、模型版本和记录留存信息",
}


# 章节 status 合法值
SECTION_STATUS_OK: str = "ok"
SECTION_STATUS_PARTIAL: str = "partial"
SECTION_STATUS_MISSING: str = "missing"
SECTION_STATUS_NOT_APPLICABLE: str = "not_applicable"
SECTION_STATUSES: tuple[str, ...] = (
    SECTION_STATUS_OK,
    SECTION_STATUS_PARTIAL,
    SECTION_STATUS_MISSING,
    SECTION_STATUS_NOT_APPLICABLE,
)


# 报告级 status 合法值
OVERALL_STATUS_OK: str = "ok"
OVERALL_STATUS_PARTIAL: str = "partial"
OVERALL_STATUS_DEGRADED: str = "degraded"
OVERALL_STATUSES: tuple[str, ...] = (
    OVERALL_STATUS_OK,
    OVERALL_STATUS_PARTIAL,
    OVERALL_STATUS_DEGRADED,
)


# 固定 notice：研究数据产物，不包含交易动作建议
NOTICE: str = "研究数据产物，不包含交易动作建议。"

# 触发 degraded 阈值：missing 章节数 >= 此比例时整体降级
DEGRADED_MISSING_RATIO_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class ReportSectionContent:
    """单章节内容。

    Attributes:
        section: 章节标识（ReportSection.value）
        title: 中文标题
        content: 章节结构化内容（任意 dict）
        limitations: 章节限制说明列表
        source_refs: 章节来源引用列表（claim_id / evidence_id）
        section_status: 章节状态（ok / partial / missing / not_applicable）
    """

    section: str = ""
    title: str = ""
    content: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    section_status: str = SECTION_STATUS_MISSING

    def __post_init__(self) -> None:
        self.content = dict(self.content) if isinstance(self.content, dict) else {"value": self.content}
        self.limitations = _string_list(self.limitations)
        self.source_refs = _string_list(self.source_refs)
        if self.section_status not in SECTION_STATUSES:
            self.section_status = SECTION_STATUS_MISSING

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "section": self.section,
            "title": self.title,
            "content": dict(self.content),
            "limitations": list(self.limitations),
            "source_refs": list(self.source_refs),
            "section_status": self.section_status,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReportSectionContent":
        """从字典反序列化。"""
        data = dict(raw or {})
        content = data.get("content", {}) or {}
        if not isinstance(content, dict):
            content = {"value": content}
        return cls(
            section=str(data.get("section", "")),
            title=str(data.get("title", "")),
            content=content,
            limitations=list(data.get("limitations", []) or []),
            source_refs=list(data.get("source_refs", []) or []),
            section_status=str(
                data.get("section_status", SECTION_STATUS_MISSING)
            ),
        )


@dataclass
class CfaReport:
    """CFA 风格完整研究报告。

    Attributes:
        report_id: 报告唯一 ID（cfar_ 前缀）
        company_symbol: 公司代码
        company_name: 公司名称
        as_of: PIT 时点
        lookback_days: 回看天数
        sections: 10 章节内容列表（顺序与 ReportSection 一致）
        overall_status: 报告整体状态（ok / partial / degraded）
        degradation_reasons: 降级原因列表
        notice: 固定提示「研究数据产物，不包含交易动作建议。」
        model_version: 模型版本
        created_at: 创建时间（UTC ISO）
    """

    report_id: str = field(default_factory=lambda: new_id("cfar"))
    company_symbol: str = ""
    company_name: str = ""
    as_of: str = ""
    lookback_days: int = 120
    sections: list[ReportSectionContent] = field(default_factory=list)
    overall_status: str = OVERALL_STATUS_PARTIAL
    degradation_reasons: list[str] = field(default_factory=list)
    notice: str = NOTICE
    model_version: str = "v1"
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.lookback_days = max(1, _safe_int(self.lookback_days))
        self.sections = [item for item in self.sections if isinstance(item, ReportSectionContent)]
        self.degradation_reasons = _string_list(self.degradation_reasons)
        if self.overall_status not in OVERALL_STATUSES:
            self.overall_status = OVERALL_STATUS_PARTIAL

    def get_section(self, section: str) -> ReportSectionContent | None:
        """按 section value 获取章节。

        Args:
            section: ReportSection.value

        Returns:
            匹配的章节；无返回 None
        """
        for s in self.sections:
            if s.section == section:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "report_id": self.report_id,
            "company_symbol": self.company_symbol,
            "company_name": self.company_name,
            "as_of": self.as_of,
            "lookback_days": self.lookback_days,
            "sections": [s.to_dict() for s in self.sections],
            "overall_status": self.overall_status,
            "degradation_reasons": list(self.degradation_reasons),
            "notice": self.notice,
            "model_version": self.model_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CfaReport":
        """从字典反序列化。"""
        data = dict(raw) if isinstance(raw, dict) else {}
        sections_raw = data.get("sections", []) or []
        if not isinstance(sections_raw, (list, tuple)):
            sections_raw = []
        sections = [
            ReportSectionContent.from_dict(s) if isinstance(s, dict) else s
            for s in sections_raw
        ]
        return cls(
            report_id=str(data.get("report_id") or new_id("cfar")),
            company_symbol=str(data.get("company_symbol", "")),
            company_name=str(data.get("company_name", "")),
            as_of=str(data.get("as_of", "")),
            lookback_days=_safe_int(data.get("lookback_days", 120) or 120),
            sections=sections,
            overall_status=str(data.get("overall_status", OVERALL_STATUS_PARTIAL)),
            degradation_reasons=_string_list(data.get("degradation_reasons", [])),
            notice=str(data.get("notice", NOTICE)),
            model_version=str(data.get("model_version", "v1")),
            created_at=str(data.get("created_at", "")),
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def compute_overall_status(
    sections: list[ReportSectionContent],
) -> tuple[str, list[str]]:
    """根据各章节 status 计算整体 status 与降级原因。

    规则：
    - 任一 missing -> partial
    - missing 数 >= 50% -> degraded
    - 全 ok -> ok
    - not_applicable 不计入降级

    Args:
        sections: 章节内容列表

    Returns:
        (overall_status, degradation_reasons) 二元组
    """
    if not sections:
        return OVERALL_STATUS_DEGRADED, ["no_sections_built"]
    total = len(sections)
    missing_count = sum(
        1 for s in sections if s.section_status == SECTION_STATUS_MISSING
    )
    partial_count = sum(
        1 for s in sections if s.section_status == SECTION_STATUS_PARTIAL
    )
    reasons: list[str] = []
    for s in sections:
        if s.section_status == SECTION_STATUS_MISSING:
            reasons.append(f"section_missing:{s.section}")
        elif s.section_status == SECTION_STATUS_PARTIAL:
            reasons.append(f"section_partial:{s.section}")

    if missing_count >= max(1, int(total * DEGRADED_MISSING_RATIO_THRESHOLD)):
        return OVERALL_STATUS_DEGRADED, reasons
    if missing_count > 0 or partial_count > 0:
        return OVERALL_STATUS_PARTIAL, reasons
    return OVERALL_STATUS_OK, []


def empty_section(section: str, reason: str = "no_data") -> ReportSectionContent:
    """构造一个 missing 状态的空章节。

    Args:
        section: 章节标识
        reason: 缺失原因（写入 limitations）

    Returns:
        ReportSectionContent，section_status=missing
    """
    return ReportSectionContent(
        section=section,
        title=SECTION_TITLES.get(section, section),
        content={"no_data": True, "reason": reason},
        limitations=[reason],
        source_refs=[],
        section_status=SECTION_STATUS_MISSING,
    )
