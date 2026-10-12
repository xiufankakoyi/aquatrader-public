"""P2.1 全市场研究截面契约。

按 P2 计划 §4 Task P2.1，截面字段组覆盖 9 个维度：
1. financial_quality（财务质量）：ROIC、现金兑现、杠杆、利润质量
2. growth（成长）：收入/利润增长、增量 ROIC、产品驱动持续性
3. cashflow_and_capital_efficiency（现金流与资本效率）
4. consensus_and_revisions（一致预期及修正）
5. valuation（历史与前瞻估值）
6. industry_timing（产业阶段与风口持续性）
7. operating_driver_completeness（公司产品利润驱动完整度）
8. evidence_quality（证据完整度和不确定性）
9. researchability（流动性、停牌、上市时间等可研究性信息）

强制原则（P2 §2）：
- 基本面、成长、预期、估值、证据、技术状态分开，不合并成不可解释总分
- 所有筛选必须 point-in-time、无幸存者偏差、无未来函数
- 空字段不以零分惩罚，单独进入 data_availability 维度

硬门禁：
- 每个 SnapshotField 必须带 available_at 和 source_refs
- 任何 as_of 查询只能看到 available_at <= as_of 的字段（PIT 由 repository 强制）
- 截面构建保存 provider / version / coverage / exclusion_reasons
- 退市/暂停上市/历史成分处理不产生幸存者偏差（membership_status 显式标记）
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


# ---------------------------------------------------------------------------
# 9 个字段组名称（P2 §4 Task P2.1 截面字段组）
# ---------------------------------------------------------------------------

FIELD_GROUP_FINANCIAL_QUALITY: str = "financial_quality"
FIELD_GROUP_GROWTH: str = "growth"
FIELD_GROUP_CASHFLOW_CAPITAL_EFFICIENCY: str = "cashflow_and_capital_efficiency"
FIELD_GROUP_CONSENSUS_REVISIONS: str = "consensus_and_revisions"
FIELD_GROUP_VALUATION: str = "valuation"
FIELD_GROUP_INDUSTRY_TIMING: str = "industry_timing"
FIELD_GROUP_OPERATING_DRIVER_COMPLETENESS: str = "operating_driver_completeness"
FIELD_GROUP_EVIDENCE_QUALITY: str = "evidence_quality"
FIELD_GROUP_RESEARCHABILITY: str = "researchability"

FIELD_GROUP_NAMES: tuple[str, ...] = (
    FIELD_GROUP_FINANCIAL_QUALITY,
    FIELD_GROUP_GROWTH,
    FIELD_GROUP_CASHFLOW_CAPITAL_EFFICIENCY,
    FIELD_GROUP_CONSENSUS_REVISIONS,
    FIELD_GROUP_VALUATION,
    FIELD_GROUP_INDUSTRY_TIMING,
    FIELD_GROUP_OPERATING_DRIVER_COMPLETENESS,
    FIELD_GROUP_EVIDENCE_QUALITY,
    FIELD_GROUP_RESEARCHABILITY,
)


# ---------------------------------------------------------------------------
# 数据可用性维度（P2.1 验收点 4：空字段不以零分惩罚）
# ---------------------------------------------------------------------------

# 字段填充状态：与 P0 grounding_status 对齐，但额外区分"未填充"与"不可计算"
DATA_AVAILABILITY_AVAILABLE: str = "available"          # 字段有值且可追溯
DATA_AVAILABILITY_MISSING: str = "missing"               # 字段缺失（无来源）
DATA_AVAILABILITY_NOT_COMPUTABLE: str = "not_computable"  # 字段不可计算（分母为 0 等）
DATA_AVAILABILITY_NOT_APPLICABLE: str = "not_applicable"  # 字段不适用（如银行不适用 EV/EBITDA）
DATA_AVAILABILITY_PENDING: str = "pending"                # 字段待补（PIT 时点尚未披露）

DATA_AVAILABILITY_STATUSES: tuple[str, ...] = (
    DATA_AVAILABILITY_AVAILABLE,
    DATA_AVAILABILITY_MISSING,
    DATA_AVAILABILITY_NOT_COMPUTABLE,
    DATA_AVAILABILITY_NOT_APPLICABLE,
    DATA_AVAILABILITY_PENDING,
)


# ---------------------------------------------------------------------------
# 成分股状态（P2.1 验收点 2：退市/暂停上市/历史成分处理不产生幸存者偏差）
# ---------------------------------------------------------------------------

MEMBERSHIP_LISTED: str = "listed"            # 当时为正常上市
MEMBERSHIP_SUSPENDED: str = "suspended"      # 当时暂停上市（仍为成分，但不可交易）
MEMBERSHIP_DELISTED: str = "delisted"        # as_of 之前已退市（不应进入截面）
MEMBERSHIP_PRE_LISTING: str = "pre_listing"  # as_of 之后才上市（不应进入截面）
MEMBERSHIP_HISTORICAL: str = "historical"    # 历史成分（已退出指数但 as_of 时仍在）
MEMBERSHIP_INDEX_OUT: str = "index_out"      # as_of 之后被指数剔除（as_of 时仍在）

MEMBERSHIP_STATUSES: tuple[str, ...] = (
    MEMBERSHIP_LISTED,
    MEMBERSHIP_SUSPENDED,
    MEMBERSHIP_DELISTED,
    MEMBERSHIP_PRE_LISTING,
    MEMBERSHIP_HISTORICAL,
    MEMBERSHIP_INDEX_OUT,
)


def utc_now() -> str:
    """返回当前 UTC ISO 时间字符串。

    Returns:
        ISO 8601 格式时间字符串
    """
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。

    Args:
        prefix: ID 前缀，如 "snap" / "member" / "excl"

    Returns:
        形如 "snap_xxxxxxxxxxxxxxxx" 的唯一 ID
    """
    return f"{prefix}_{uuid4().hex[:16]}"


def _normalize_as_of(as_of: str | None) -> str:
    """规范化 as_of 为 YYYY-MM-DD 字符串。

    None 视为未来无穷大（用于无 PIT 限制的内部查询）。

    Args:
        as_of: 原始时点字符串（YYYY-MM-DD 或 ISO 8601）

    Returns:
        规范化后的 YYYY-MM-DD 字符串
    """
    if not as_of:
        return "9999-12-31"
    # 兼容 ISO 8601 与 YYYYMMDD
    text = str(as_of).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    # 取日期部分
    return text[:10]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if item is not None and str(item)]


# ---------------------------------------------------------------------------
# 截面字段（最细粒度）
# ---------------------------------------------------------------------------


@dataclass
class SnapshotField:
    """截面单字段：截面最细粒度的可追溯单元。

    每个字段必须带 available_at 和 source_refs；缺数据时 status 标记为
    missing / not_computable / not_applicable / pending，value 为 None，
    不进入打分路径，仅计入数据可用性维度。

    Attributes:
        name: 字段名（如 "roic"、"revenue_growth_yoy"）
        value: 字段值；缺数据为 None
        unit: 单位（"ratio" / "percent" / "元" / "天" / "倍"）
        available_at: 字段公开时点（PIT 边界，YYYY-MM-DD）
        source_refs: 来源引用列表（source_trace_id 或 evidence_id）
        status: 数据可用性状态
        formula: 公式文本（人可读，便于解释）
        note: 备注（如 "分母为 0"）
    """

    name: str = ""
    value: float | None = None
    unit: str = ""
    available_at: str = ""
    source_refs: list[str] = field(default_factory=list)
    status: str = DATA_AVAILABILITY_MISSING
    formula: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "available_at": self.available_at,
            "source_refs": list(self.source_refs),
            "status": self.status,
            "formula": self.formula,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotField":
        """从字典反序列化。

        Args:
            data: 序列化字典

        Returns:
            SnapshotField 实例
        """
        data = data if isinstance(data, Mapping) else {}
        return cls(
            name=str(data.get("name", "")),
            value=_finite_or_none(data.get("value")),
            unit=str(data.get("unit", "")),
            available_at=str(data.get("available_at", "")),
            source_refs=_text_list(data.get("source_refs")),
            status=str(data.get("status", DATA_AVAILABILITY_MISSING)),
            formula=str(data.get("formula", "")),
            note=str(data.get("note", "")),
        )


# ---------------------------------------------------------------------------
# 字段组（9 个维度之一）
# ---------------------------------------------------------------------------


@dataclass
class FieldGroup:
    """字段组：9 个维度之一，包含若干 SnapshotField。

    强制原则（P2 §2）：字段组之间不合并成不可解释总分。
    每个字段组独立保存统计信息：
    - filled_count：已填充字段数（status="available"）
    - missing_count：缺失字段数（status="missing"）
    - non_computable_count：不可计算字段数
    - not_applicable_count：不适用字段数
    - pending_count：待补字段数
    - coverage_ratio：填充率 = filled_count / total_count

    Attributes:
        name: 字段组名（FIELD_GROUP_NAMES 之一）
        fields: 字段列表
        group_note: 字段组级别备注（如 "银行不适用 EV/EBITDA"）
    """

    name: str = ""
    fields: list[SnapshotField] = field(default_factory=list)
    group_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，包含统计字段。"""
        stats = self.compute_stats()
        return {
            "name": self.name,
            "fields": [f.to_dict() for f in self.fields],
            "group_note": self.group_note,
            "stats": stats,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FieldGroup":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        raw_fields = data.get("fields", [])
        return cls(
            name=str(data.get("name", "")),
            fields=[
                SnapshotField.from_dict(f)
                for f in (raw_fields if isinstance(raw_fields, list) else [])
            ],
            group_note=str(data.get("group_note", "")),
        )

    def compute_stats(self) -> dict[str, int | float]:
        """计算字段组统计信息。

        Returns:
            包含 filled/missing/non_computable/not_applicable/pending 计数
            与 coverage_ratio 的字典
        """
        filled = sum(1 for f in self.fields if f.status == DATA_AVAILABILITY_AVAILABLE)
        missing = sum(1 for f in self.fields if f.status == DATA_AVAILABILITY_MISSING)
        non_comp = sum(1 for f in self.fields if f.status == DATA_AVAILABILITY_NOT_COMPUTABLE)
        not_app = sum(1 for f in self.fields if f.status == DATA_AVAILABILITY_NOT_APPLICABLE)
        pending = sum(1 for f in self.fields if f.status == DATA_AVAILABILITY_PENDING)
        total = len(self.fields)
        coverage = filled / total if total > 0 else 0.0
        return {
            "filled_count": filled,
            "missing_count": missing,
            "non_computable_count": non_comp,
            "not_applicable_count": not_app,
            "pending_count": pending,
            "total_count": total,
            "coverage_ratio": coverage,
        }


# ---------------------------------------------------------------------------
# 截面成员（单公司）
# ---------------------------------------------------------------------------


@dataclass
class UniverseMember:
    """截面成员：单个公司在某 as_of 的全部 9 个字段组。

    成分股状态（membership_status）显式标记，避免幸存者偏差：
    - listed / suspended：正常进入截面
    - delisted / pre_listing：不应进入截面（构建器负责过滤，仓储负责 PIT 校验）
    - historical / index_out：as_of 时仍在截面，需要保留

    Attributes:
        member_id: 成员唯一 ID（同 snapshot_id + symbol 唯一）
        snapshot_id: 所属截面 ID
        symbol: 公司代码（如 600519.SH）
        company_name: 公司名称
        industry_code: 行业代码（如 申万一级行业代码）
        industry_name: 行业名称
        as_of: 截面时点 YYYY-MM-DD
        membership_status: 成分股状态
        listing_date: 上市日期（用于 PIT 校验）
        delisting_date: 退市日期（None 表示未退市）
        field_groups: 9 个字段组的字典 {group_name: FieldGroup}
        data_availability_score: 数据可用性综合分（0-1，仅用于排序研究优先级）
        member_note: 成员级别备注
    """

    member_id: str = ""
    snapshot_id: str = ""
    symbol: str = ""
    company_name: str = ""
    industry_code: str = ""
    industry_name: str = ""
    as_of: str = ""
    membership_status: str = MEMBERSHIP_LISTED
    listing_date: str = ""
    delisting_date: str = ""
    field_groups: dict[str, FieldGroup] = field(default_factory=dict)
    data_availability_score: float = 0.0
    member_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "member_id": self.member_id,
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "company_name": self.company_name,
            "industry_code": self.industry_code,
            "industry_name": self.industry_name,
            "as_of": self.as_of,
            "membership_status": self.membership_status,
            "listing_date": self.listing_date,
            "delisting_date": self.delisting_date,
            "field_groups": {k: v.to_dict() for k, v in self.field_groups.items()},
            "data_availability_score": self.data_availability_score,
            "member_note": self.member_note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UniverseMember":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        fg_raw = data.get("field_groups", {}) or {}
        if not isinstance(fg_raw, Mapping):
            fg_raw = {}
        return cls(
            member_id=str(data.get("member_id", "")),
            snapshot_id=str(data.get("snapshot_id", "")),
            symbol=str(data.get("symbol", "")),
            company_name=str(data.get("company_name", "")),
            industry_code=str(data.get("industry_code", "")),
            industry_name=str(data.get("industry_name", "")),
            as_of=str(data.get("as_of", "")),
            membership_status=str(data.get("membership_status", MEMBERSHIP_LISTED)),
            listing_date=str(data.get("listing_date", "")),
            delisting_date=str(data.get("delisting_date", "")),
            field_groups={
                str(k): FieldGroup.from_dict(v)
                for k, v in fg_raw.items()
                if isinstance(v, Mapping)
            },
            data_availability_score=_safe_float(data.get("data_availability_score", 0.0)),
            member_note=str(data.get("member_note", "")),
        )

    def compute_data_availability_score(self) -> float:
        """计算数据可用性综合分（0-1）。

        综合分仅用于排序研究优先级，不作为基本面打分。
        计算方法：所有字段组 filled_count 之和 / 所有字段组 total_count 之和。

        Returns:
            数据可用性综合分
        """
        total_filled = 0
        total_fields = 0
        for group in self.field_groups.values():
            stats = group.compute_stats()
            total_filled += stats["filled_count"]
            total_fields += stats["total_count"]
        self.data_availability_score = (
            total_filled / total_fields if total_fields > 0 else 0.0
        )
        return self.data_availability_score


# ---------------------------------------------------------------------------
# 截面排除记录（P2.1 验收点 3：保存排除原因）
# ---------------------------------------------------------------------------


@dataclass
class ExclusionRecord:
    """截面排除记录：某公司在某 as_of 被排除出截面的原因。

    P2.1 验收点 3 要求：截面构建保存 provider、版本、覆盖率和排除原因。
    ExclusionRecord 记录每个被排除公司的：
    - 排除原因（delisted / pre_listing / index_out / data_corruption / manual_override）
    - 排除时点
    - 排除来源（构建器 / 人工 / 数据校验）

    Attributes:
        symbol: 公司代码
        as_of: 截面时点
        reason: 排除原因
        detail: 详细说明
        excluded_by: 排除来源（builder / manual / data_validator）
        excluded_at: 排除操作时间（UTC ISO）
    """

    symbol: str = ""
    as_of: str = ""
    reason: str = ""
    detail: str = ""
    excluded_by: str = "builder"
    excluded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "reason": self.reason,
            "detail": self.detail,
            "excluded_by": self.excluded_by,
            "excluded_at": self.excluded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExclusionRecord":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        return cls(
            symbol=str(data.get("symbol", "")),
            as_of=str(data.get("as_of", "")),
            reason=str(data.get("reason", "")),
            detail=str(data.get("detail", "")),
            excluded_by=str(data.get("excluded_by", "builder")),
            excluded_at=str(data.get("excluded_at", "")),
        )


# ---------------------------------------------------------------------------
# 截面覆盖率统计
# ---------------------------------------------------------------------------


@dataclass
class CoverageStat:
    """截面覆盖率统计：按字段组统计覆盖率。

    Attributes:
        group_name: 字段组名
        member_count: 截面成员数
        filled_field_count: 已填充字段总数
        total_field_count: 字段总数
        coverage_ratio: 覆盖率 = filled / total
        missing_ratio: 缺失率 = missing / total
    """

    group_name: str = ""
    member_count: int = 0
    filled_field_count: int = 0
    total_field_count: int = 0
    coverage_ratio: float = 0.0
    missing_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "group_name": self.group_name,
            "member_count": self.member_count,
            "filled_field_count": self.filled_field_count,
            "total_field_count": self.total_field_count,
            "coverage_ratio": self.coverage_ratio,
            "missing_ratio": self.missing_ratio,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CoverageStat":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        return cls(
            group_name=str(data.get("group_name", "")),
            member_count=_safe_int(data.get("member_count", 0)),
            filled_field_count=_safe_int(data.get("filled_field_count", 0)),
            total_field_count=_safe_int(data.get("total_field_count", 0)),
            coverage_ratio=_safe_float(data.get("coverage_ratio", 0.0)),
            missing_ratio=_safe_float(data.get("missing_ratio", 0.0)),
        )


# ---------------------------------------------------------------------------
# 截面来源（P2.1 验收点 3：保存 provider / version / coverage / exclusion）
# ---------------------------------------------------------------------------


@dataclass
class SnapshotProvenance:
    """截面来源元数据。

    P2.1 验收点 3 要求：截面构建保存 provider、版本、覆盖率和排除原因。

    Attributes:
        provider: 数据提供方（如 "tushare" / "akshare" / "manual"）
        provider_version: provider 版本（如 tushare pro 版本号）
        builder_version: 截面构建器版本（如 "p2.1.0"）
        built_at: 截面构建时间（UTC ISO）
        built_by: 构建者（builder / manual / scheduled_job）
        policy_version: 截面构建策略版本
        data_fingerprint: 数据指纹（universe + as_of + provider 的 sha256）
        coverage_stats: 按字段组的覆盖率统计列表
        exclusion_count: 排除公司总数
        total_member_count: 截面成员总数
        config_hash: 构建配置 hash（用于复现）
    """

    provider: str = ""
    provider_version: str = ""
    builder_version: str = "p2.1.0"
    built_at: str = ""
    built_by: str = "builder"
    policy_version: str = ""
    data_fingerprint: str = ""
    coverage_stats: list[CoverageStat] = field(default_factory=list)
    exclusion_count: int = 0
    total_member_count: int = 0
    config_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "builder_version": self.builder_version,
            "built_at": self.built_at,
            "built_by": self.built_by,
            "policy_version": self.policy_version,
            "data_fingerprint": self.data_fingerprint,
            "coverage_stats": [c.to_dict() for c in self.coverage_stats],
            "exclusion_count": self.exclusion_count,
            "total_member_count": self.total_member_count,
            "config_hash": self.config_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotProvenance":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        raw_stats = data.get("coverage_stats", [])
        return cls(
            provider=str(data.get("provider", "")),
            provider_version=str(data.get("provider_version", "")),
            builder_version=str(data.get("builder_version", "p2.1.0")),
            built_at=str(data.get("built_at", "")),
            built_by=str(data.get("built_by", "builder")),
            policy_version=str(data.get("policy_version", "")),
            data_fingerprint=str(data.get("data_fingerprint", "")),
            coverage_stats=[
                CoverageStat.from_dict(c)
                for c in (raw_stats if isinstance(raw_stats, list) else [])
            ],
            exclusion_count=_safe_int(data.get("exclusion_count", 0)),
            total_member_count=_safe_int(data.get("total_member_count", 0)),
            config_hash=str(data.get("config_hash", "")),
        )


# ---------------------------------------------------------------------------
# 截面（全市场某个 as_of 的完整快照）
# ---------------------------------------------------------------------------


@dataclass
class UniverseSnapshot:
    """全市场研究截面：某个 as_of 时点的完整快照。

    P2.1 完成标准：构建无未来泄漏的全市场研究截面。

    不变量：
    - 同 (snapshot_id, symbol) 唯一
    - snapshot_id 由 builder 生成（snap_<as_of>_<policy_version>_<short_hash>）
    - 所有 member.as_of 必须等于 snapshot.as_of
    - 所有 member.field_groups 必须覆盖 9 个字段组（即使为空 FieldGroup）
    - 不可覆盖：同 snapshot_id 重复保存抛异常（由 repository 强制）

    Attributes:
        snapshot_id: 截面唯一 ID
        as_of: 截面时点 YYYY-MM-DD
        universe_name: 截面名称（如 "csi300" / "all_a_share"）
        universe_version: 截面 universe 版本（如指数成分股版本）
        members: 成员列表
        exclusions: 排除记录列表
        provenance: 来源元数据
        policy_version: 截面构建策略版本
        created_at: 截面创建时间（UTC ISO）
    """

    snapshot_id: str = ""
    as_of: str = ""
    universe_name: str = "all_a_share"
    universe_version: str = ""
    members: list[UniverseMember] = field(default_factory=list)
    exclusions: list[ExclusionRecord] = field(default_factory=list)
    provenance: SnapshotProvenance = field(default_factory=SnapshotProvenance)
    policy_version: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of,
            "universe_name": self.universe_name,
            "universe_version": self.universe_version,
            "members": [m.to_dict() for m in self.members],
            "exclusions": [e.to_dict() for e in self.exclusions],
            "provenance": self.provenance.to_dict(),
            "policy_version": self.policy_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UniverseSnapshot":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        prov_raw = data.get("provenance", {}) or {}
        raw_members = data.get("members", [])
        raw_exclusions = data.get("exclusions", [])
        return cls(
            snapshot_id=str(data.get("snapshot_id", "")),
            as_of=str(data.get("as_of", "")),
            universe_name=str(data.get("universe_name", "all_a_share")),
            universe_version=str(data.get("universe_version", "")),
            members=[
                UniverseMember.from_dict(m)
                for m in (raw_members if isinstance(raw_members, list) else [])
            ],
            exclusions=[
                ExclusionRecord.from_dict(e)
                for e in (raw_exclusions if isinstance(raw_exclusions, list) else [])
            ],
            provenance=SnapshotProvenance.from_dict(prov_raw),
            policy_version=str(data.get("policy_version", "")),
            created_at=str(data.get("created_at", "")),
        )

    def compute_coverage_stats(self) -> list[CoverageStat]:
        """计算按字段组的覆盖率统计。

        Returns:
            覆盖率统计列表（9 个字段组各一项）
        """
        stats: list[CoverageStat] = []
        for group_name in FIELD_GROUP_NAMES:
            filled = 0
            total = 0
            for member in self.members:
                group = member.field_groups.get(group_name)
                if group is None:
                    continue
                g_stats = group.compute_stats()
                filled += g_stats["filled_count"]
                total += g_stats["total_count"]
            coverage = filled / total if total > 0 else 0.0
            missing = (total - filled) / total if total > 0 else 0.0
            stats.append(CoverageStat(
                group_name=group_name,
                member_count=len(self.members),
                filled_field_count=filled,
                total_field_count=total,
                coverage_ratio=coverage,
                missing_ratio=missing,
            ))
        self.provenance.coverage_stats = stats
        return stats
