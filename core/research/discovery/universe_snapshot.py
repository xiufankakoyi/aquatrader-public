"""P2.1 截面构建器：从 universe provider + 字段组 assembler 组装截面。

按 P2 计划 §4 Task P2.1：
- 接受 as_of 与 universe 定义
- 对每个 symbol 校验 membership_status（delisted / pre_listing → 排除并记录原因）
- 对每个有效 symbol 调用 assembler 组装 9 个字段组
- 空字段进入 data_availability 维度（status=missing），不进入打分路径
- 计算 coverage_stats 与 data_fingerprint
- 返回 UniverseSnapshot

设计模式（依赖倒置）：
- UniverseProvider：抽象接口，提供 (symbol, membership_status, listing_date,
  delisting_date, industry_code, industry_name, company_name) 列表
- FieldGroupAssembler：抽象接口，为单个 symbol 组装 9 个 FieldGroup
- UniverseSnapshotBuilder：主构建器，编排 provider + assembler

测试友好：
- StaticUniverseProvider：固定列表（测试用）
- EmptyFieldGroupAssembler：返回 9 个空 FieldGroup（测试用）
- P1BackedFieldGroupAssembler：从 P1 仓储读取真实数据（生产用，best-effort）

强制原则（P2 §2）：
- 字段组分开存放，不合并成不可解释总分
- 所有筛选必须 point-in-time、无幸存者偏差、无未来函数
- 不输出投资建议（AGENTS.md）
- 缺数据降级处理（AGENTS.md）
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import (
    ExclusionRecord,
    FIELD_GROUP_NAMES,
    FieldGroup,
    MEMBERSHIP_DELISTED,
    MEMBERSHIP_HISTORICAL,
    MEMBERSHIP_INDEX_OUT,
    MEMBERSHIP_LISTED,
    MEMBERSHIP_PRE_LISTING,
    MEMBERSHIP_SUSPENDED,
    SnapshotField,
    SnapshotProvenance,
    UniverseMember,
    UniverseSnapshot,
    _normalize_as_of,
    new_id,
    utc_now,
)
from .repository import UniverseSnapshotRepository


# ---------------------------------------------------------------------------
# Universe Provider（抽象接口）
# ---------------------------------------------------------------------------


@dataclass
class UniverseEntry:
    """Universe 成员条目：as_of 时点的成分股信息。

    Attributes:
        symbol: 公司代码（如 600519.SH）
        company_name: 公司名称
        industry_code: 行业代码
        industry_name: 行业名称
        membership_status: 成分股状态（listed/suspended/delisted/pre_listing/historical/index_out）
        listing_date: 上市日期 YYYY-MM-DD
        delisting_date: 退市日期 YYYY-MM-DD（空表示未退市）
    """

    symbol: str = ""
    company_name: str = ""
    industry_code: str = ""
    industry_name: str = ""
    membership_status: str = MEMBERSHIP_LISTED
    listing_date: str = ""
    delisting_date: str = ""


class UniverseProvider(Protocol):
    """Universe 提供方抽象接口。

    任意实现需提供 ``list_universe(as_of)`` 方法，返回某 as_of 时点的
    成分股列表（含已退市/暂停上市/历史成分等，由 builder 决定是否排除）。
    """

    def list_universe(self, as_of: str) -> list[UniverseEntry]:
        """返回 as_of 时点的成分股列表。

        Args:
            as_of: 时点 YYYY-MM-DD

        Returns:
            UniverseEntry 列表（不应过滤，由 builder 决定排除策略）
        """
        ...


class StaticUniverseProvider:
    """静态 universe 提供方：固定列表（测试用）。

    不做 PIT 过滤，直接返回初始化时传入的列表。
    生产场景应使用真实 provider（如 CSI300IndexProvider）。
    """

    def __init__(self, entries: list[UniverseEntry]) -> None:
        """初始化。

        Args:
            entries: 固定成分股列表
        """
        self._entries: list[UniverseEntry] = [
            entry for entry in entries
            if isinstance(entry, UniverseEntry)
        ] if isinstance(entries, (list, tuple)) else []

    def list_universe(self, as_of: str) -> list[UniverseEntry]:
        """返回固定列表（忽略 as_of）。"""
        return list(self._entries)


# ---------------------------------------------------------------------------
# Field Group Assembler（抽象接口）
# ---------------------------------------------------------------------------


class FieldGroupAssembler(Protocol):
    """字段组组装器抽象接口。

    任意实现需提供 ``assemble(symbol, as_of)`` 方法，返回某公司在某 as_of
    时点的 9 个 FieldGroup（dict[group_name, FieldGroup]）。

    实现必须遵守 PIT：只能返回 available_at <= as_of 的字段。
    缺数据时返回 status=missing 的字段，不抛异常（AGENTS.md）。
    """

    def assemble(
        self,
        symbol: str,
        as_of: str,
    ) -> dict[str, FieldGroup]:
        """组装 9 个字段组。

        Args:
            symbol: 公司代码
            as_of: 时点 YYYY-MM-DD

        Returns:
            {group_name: FieldGroup} 字典；至少包含 9 个字段组（即使为空）
        """
        ...


class EmptyFieldGroupAssembler:
    """空字段组组装器：返回 9 个空 FieldGroup（测试用）。

    所有字段组的 fields 为空列表，coverage_ratio=0.0。
    用于验证 builder 框架而不依赖 P1 仓储。
    """

    def assemble(
        self,
        symbol: str,
        as_of: str,
    ) -> dict[str, FieldGroup]:
        """返回 9 个空 FieldGroup。"""
        return {name: FieldGroup(name=name) for name in FIELD_GROUP_NAMES}


# ---------------------------------------------------------------------------
# Builder 配置
# ---------------------------------------------------------------------------


@dataclass
class BuilderConfig:
    """截面构建器配置。

    Attributes:
        universe_name: 截面名称（如 "csi300" / "all_a_share"）
        universe_version: 截面 universe 版本
        policy_version: 构建策略版本
        provider: 数据提供方（如 "tushare" / "akshare" / "manual"）
        provider_version: provider 版本
        built_by: 构建者（builder / manual / scheduled_job）
        include_suspended: 是否包含暂停上市（默认 True）
        include_historical: 是否包含历史成分（默认 True）
        include_index_out: 是否包含已退出指数但 as_of 时仍在的（默认 True）
        exclude_delisted: 是否排除已退市（默认 True）
        exclude_pre_listing: 是否排除尚未上市（默认 True）
    """

    universe_name: str = "all_a_share"
    universe_version: str = ""
    policy_version: str = "p2.1.0"
    provider: str = "manual"
    provider_version: str = ""
    built_by: str = "builder"
    include_suspended: bool = True
    include_historical: bool = True
    include_index_out: bool = True
    exclude_delisted: bool = True
    exclude_pre_listing: bool = True


# ---------------------------------------------------------------------------
# 截面构建器
# ---------------------------------------------------------------------------


class UniverseSnapshotBuilder:
    """截面构建器：编排 universe provider + field group assembler。

    工作流程：
    1. 通过 provider 获取 as_of 时点的成分股列表
    2. 对每个 entry 校验 membership_status，决定是否进入截面或排除
    3. 对每个进入截面的 symbol，通过 assembler 组装 9 个字段组
    4. 组装 UniverseMember（含 data_availability_score）
    5. 计算 coverage_stats 与 data_fingerprint
    6. 返回 UniverseSnapshot

    不变量：
    - 不修改 provider 与 assembler 的内部状态
    - 排除决策可解释（每条排除记录有 reason 与 detail）
    - 同 (as_of, universe_name, provider, policy_version) 生成相同 snapshot_id
      （保证可复现，P2 §0 协议第 2 条）
    """

    def __init__(
        self,
        provider: UniverseProvider,
        assembler: FieldGroupAssembler,
        config: BuilderConfig | None = None,
    ) -> None:
        """初始化构建器。

        Args:
            provider: universe 提供方
            assembler: 字段组组装器
            config: 构建配置；None 使用默认
        """
        self.provider = provider
        self.assembler = assembler
        self.config = config or BuilderConfig()

    def build(self, as_of: str) -> UniverseSnapshot:
        """构建截面。

        Args:
            as_of: 截面时点 YYYY-MM-DD

        Returns:
            UniverseSnapshot 实例
        """
        cutoff = _normalize_as_of(as_of)
        raw_entries = self.provider.list_universe(cutoff)
        entries = [
            entry for entry in raw_entries
            if isinstance(entry, UniverseEntry)
        ] if isinstance(raw_entries, (list, tuple)) else []
        members: list[UniverseMember] = []
        exclusions: list[ExclusionRecord] = []

        for entry in entries:
            # 校验成分股状态，决定是否排除
            should_exclude, reason, detail = self._evaluate_membership(entry, cutoff)
            if should_exclude:
                exclusions.append(ExclusionRecord(
                    symbol=entry.symbol,
                    as_of=cutoff,
                    reason=reason,
                    detail=detail,
                    excluded_by=self.config.built_by,
                    excluded_at=utc_now(),
                ))
                continue

            # 组装字段组
            try:
                field_groups = self.assembler.assemble(entry.symbol, cutoff)
            except Exception as exc:  # noqa: BLE001
                # AGENTS.md：缺数据降级，不让查询或页面崩溃
                # 异常时填入 9 个空字段组，并在 member_note 记录原因
                field_groups = {name: FieldGroup(name=name) for name in FIELD_GROUP_NAMES}
                member_note = f"assembler_error: {type(exc).__name__}: {exc}"
            else:
                member_note = ""

            if not isinstance(field_groups, dict):
                field_groups = {}
            field_groups = {
                name: group for name, group in field_groups.items()
                if isinstance(name, str) and isinstance(group, FieldGroup)
            }

            # 确保所有 9 个字段组都存在（assembler 可能漏返回）
            for name in FIELD_GROUP_NAMES:
                if name not in field_groups:
                    field_groups[name] = FieldGroup(name=name)
                elif not field_groups[name].name:
                    field_groups[name].name = name

            member = UniverseMember(
                member_id=new_id("member"),
                snapshot_id="",  # 由 repository.save 填充
                symbol=entry.symbol,
                company_name=entry.company_name,
                industry_code=entry.industry_code,
                industry_name=entry.industry_name,
                as_of=cutoff,
                membership_status=entry.membership_status,
                listing_date=entry.listing_date,
                delisting_date=entry.delisting_date,
                field_groups=field_groups,
                member_note=member_note,
            )
            member.compute_data_availability_score()
            members.append(member)

        # 计算 snapshot_id（确定性，可复现）
        sorted_symbols = sorted(m.symbol for m in members)
        data_fingerprint = UniverseSnapshotRepository.compute_data_fingerprint(
            cutoff,
            self.config.universe_name,
            self.config.provider,
            sorted_symbols,
        )
        snapshot_id = self._compute_snapshot_id(
            cutoff,
            self.config.universe_name,
            self.config.policy_version,
            data_fingerprint,
        )

        # 构建来源元数据
        provenance = SnapshotProvenance(
            provider=self.config.provider,
            provider_version=self.config.provider_version,
            builder_version="p2.1.0",
            built_at=utc_now(),
            built_by=self.config.built_by,
            policy_version=self.config.policy_version,
            data_fingerprint=data_fingerprint,
            exclusion_count=len(exclusions),
            total_member_count=len(members),
            config_hash=self._compute_config_hash(),
        )

        snapshot = UniverseSnapshot(
            snapshot_id=snapshot_id,
            as_of=cutoff,
            universe_name=self.config.universe_name,
            universe_version=self.config.universe_version,
            members=members,
            exclusions=exclusions,
            provenance=provenance,
            policy_version=self.config.policy_version,
            created_at=utc_now(),
        )
        snapshot.compute_coverage_stats()
        return snapshot

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _evaluate_membership(
        self,
        entry: UniverseEntry,
        as_of: str,
    ) -> tuple[bool, str, str]:
        """评估成分股状态，决定是否排除。

        Args:
            entry: universe 条目
            as_of: 截面时点

        Returns:
            (should_exclude, reason, detail) 三元组；
            should_exclude=True 表示排除，reason/detail 用于 ExclusionRecord
        """
        # 1. 已退市（as_of 之前退市）
        if entry.membership_status == MEMBERSHIP_DELISTED:
            if self.config.exclude_delisted:
                return True, "delisted", (
                    f"公司已于 {entry.delisting_date or '未知日期'} 退市，"
                    f"as_of={as_of} 时已不在截面"
                )
        # 2. 尚未上市
        elif entry.membership_status == MEMBERSHIP_PRE_LISTING:
            if self.config.exclude_pre_listing:
                return True, "pre_listing", (
                    f"公司上市日期 {entry.listing_date or '未知'} 晚于 as_of={as_of}，"
                    f"不应进入截面（避免幸存者偏差）"
                )
        # 3. 暂停上市
        elif entry.membership_status == MEMBERSHIP_SUSPENDED:
            if not self.config.include_suspended:
                return True, "suspended", (
                    f"公司当时暂停上市，配置 include_suspended=False 排除"
                )
        # 4. 历史成分（已退出指数但 as_of 时仍在）
        elif entry.membership_status == MEMBERSHIP_HISTORICAL:
            if not self.config.include_historical:
                return True, "historical", (
                    f"历史成分股，配置 include_historical=False 排除"
                )
        # 5. 指数剔除（as_of 之后剔除，as_of 时仍在）
        elif entry.membership_status == MEMBERSHIP_INDEX_OUT:
            if not self.config.include_index_out:
                return True, "index_out", (
                    f"as_of={as_of} 时仍在截面，但已被指数剔除，"
                    f"配置 include_index_out=False 排除"
                )
        # 6. 上市日期晚于 as_of（防御性：即使 membership_status 标错也排除）
        if entry.listing_date:
            listing_cutoff = _normalize_as_of(entry.listing_date)
            if listing_cutoff > as_of and self.config.exclude_pre_listing:
                return True, "pre_listing", (
                    f"公司上市日期 {entry.listing_date} 晚于 as_of={as_of}，"
                    f"不应进入截面（避免幸存者偏差）"
                )
        # 7. 退市日期早于 as_of（防御性）
        if entry.delisting_date:
            delist_cutoff = _normalize_as_of(entry.delisting_date)
            if delist_cutoff <= as_of and self.config.exclude_delisted:
                return True, "delisted", (
                    f"公司退市日期 {entry.delisting_date} 早于或等于 as_of={as_of}，"
                    f"不应进入截面（避免幸存者偏差）"
                )
        return False, "", ""

    def _compute_snapshot_id(
        self,
        as_of: str,
        universe_name: str,
        policy_version: str,
        data_fingerprint: str,
    ) -> str:
        """计算确定性 snapshot_id。

        格式：snap_<as_of>_<universe_name>_<policy_version>_<fp_short>

        Args:
            as_of: 截面时点
            universe_name: 截面名称
            policy_version: 策略版本
            data_fingerprint: 数据指纹

        Returns:
            snapshot_id 字符串
        """
        # 数据指纹已含 sha256，取前 12 位足够区分
        fp_short = data_fingerprint[-12:]
        safe_universe = universe_name.replace(" ", "_").lower()
        return f"snap_{as_of}_{safe_universe}_{policy_version}_{fp_short}"

    def _compute_config_hash(self) -> str:
        """计算配置 hash（用于复现性校验）。"""
        config_str = (
            f"{self.config.universe_name}|{self.config.universe_version}|"
            f"{self.config.policy_version}|{self.config.provider}|"
            f"{self.config.provider_version}|{self.config.built_by}|"
            f"{self.config.include_suspended}|{self.config.include_historical}|"
            f"{self.config.include_index_out}|{self.config.exclude_delisted}|"
            f"{self.config.exclude_pre_listing}"
        )
        return f"cfg_{hashlib.sha256(config_str.encode('utf-8')).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# P1 集成：从 P1 仓储读取真实数据组装字段组
# ---------------------------------------------------------------------------


class P1BackedFieldGroupAssembler:
    """从 P1 仓储读取真实数据组装字段组（生产用，best-effort）。

    设计原则：
    - 任一 P1 仓储读取失败时降级为空 FieldGroup，不抛异常（AGENTS.md）
    - 字段 available_at 必须来自 P1 数据的 available_at（PIT 由 P1 仓储保证）
    - 缺数据时 status=missing，value=None
    - 不修改 P1 仓储状态

    覆盖 9 个字段组的 best-effort 实现：
    1. financial_quality：从 StatementRepository + QualityReport 引擎读取
    2. growth：从 StatementRepository 同比 + OperatingModelRepository 前瞻
    3. cashflow_and_capital_efficiency：从 StatementRepository 现金流字段
    4. consensus_and_revisions：从 ExpectationRepository
    5. valuation：从 ValuationRepository
    6. industry_timing：从 IndustryRepository
    7. operating_driver_completeness：从 OperatingModelRepository
    8. evidence_quality：留空（P2.4 Top-K 检索后填充）
    9. researchability：留空（需行情数据，P2.x 后续阶段填充）
    """

    def __init__(
        self,
        statement_repo: Any | None = None,
        quality_engine: Any | None = None,
        expectation_repo: Any | None = None,
        valuation_repo: Any | None = None,
        industry_repo: Any | None = None,
        operating_model_repo: Any | None = None,
    ) -> None:
        """初始化。

        Args:
            statement_repo: P1 StatementRepository（可选）
            quality_engine: P1 FinancialQualityEngine（可选）
            expectation_repo: P1 ExpectationRepository（可选）
            valuation_repo: P1 ValuationRepository（可选）
            industry_repo: P1 IndustryRepository（可选）
            operating_model_repo: P1 OperatingModelRepository（可选）
        """
        self.statement_repo = statement_repo
        self.quality_engine = quality_engine
        self.expectation_repo = expectation_repo
        self.valuation_repo = valuation_repo
        self.industry_repo = industry_repo
        self.operating_model_repo = operating_model_repo

    def assemble(
        self,
        symbol: str,
        as_of: str,
    ) -> dict[str, FieldGroup]:
        """组装 9 个字段组（best-effort，缺数据降级）。

        Args:
            symbol: 公司代码
            as_of: 时点 YYYY-MM-DD

        Returns:
            {group_name: FieldGroup} 字典（含 9 个字段组）
        """
        groups: dict[str, FieldGroup] = {}
        groups["financial_quality"] = self._assemble_financial_quality(symbol, as_of)
        groups["growth"] = self._assemble_growth(symbol, as_of)
        groups["cashflow_and_capital_efficiency"] = self._assemble_cashflow(symbol, as_of)
        groups["consensus_and_revisions"] = self._assemble_consensus(symbol, as_of)
        groups["valuation"] = self._assemble_valuation(symbol, as_of)
        groups["industry_timing"] = self._assemble_industry(symbol, as_of)
        groups["operating_driver_completeness"] = self._assemble_operating(symbol, as_of)
        groups["evidence_quality"] = FieldGroup(
            name="evidence_quality",
            group_note="P2.4 Top-K 检索后填充",
        )
        groups["researchability"] = FieldGroup(
            name="researchability",
            group_note="需行情数据，P2.x 后续阶段填充",
        )
        return groups

    # ---- 各字段组 best-effort 实现 ----

    def _assemble_financial_quality(
        self,
        symbol: str,
        as_of: str,
    ) -> FieldGroup:
        """组装财务质量字段组。"""
        group = FieldGroup(name="financial_quality")
        if self.statement_repo is None or self.quality_engine is None:
            return group
        try:
            # 获取最近一期报表
            stmt = self._get_latest_statement(symbol, as_of)
            if stmt is None:
                return group
            report = self.quality_engine.compute(stmt)
            if report is None:
                return group
            # 抽取关键质量指标
            for metric_name in ("roic", "roe", "gross_margin", "net_margin",
                                "ocf_to_net_income", "debt_to_equity",
                                "current_ratio", "interest_coverage"):
                metric = report.metrics.get(metric_name)
                if metric is None:
                    continue
                group.fields.append(SnapshotField(
                    name=metric_name,
                    value=metric.value,
                    unit=metric.unit,
                    available_at=stmt.available_at,
                    source_refs=[stmt.source_trace_id] if hasattr(stmt, "source_trace_id") else [],
                    status=("available" if metric.status == "ok" else "not_computable"),
                    formula=metric.formula,
                    note=metric.note,
                ))
        except Exception:  # noqa: BLE001
            pass
        return group

    def _assemble_growth(self, symbol: str, as_of: str) -> FieldGroup:
        """组装成长字段组。"""
        group = FieldGroup(name="growth")
        if self.statement_repo is None or self.quality_engine is None:
            return group
        try:
            stmt = self._get_latest_statement(symbol, as_of)
            if stmt is None:
                return group
            report = self.quality_engine.compute(stmt)
            if report is None:
                return group
            for metric_name in ("revenue_growth_yoy", "net_profit_growth_yoy",
                                "incremental_roic", "eps_growth_yoy"):
                metric = report.metrics.get(metric_name)
                if metric is None:
                    continue
                group.fields.append(SnapshotField(
                    name=metric_name,
                    value=metric.value,
                    unit=metric.unit,
                    available_at=stmt.available_at,
                    source_refs=[stmt.source_trace_id] if hasattr(stmt, "source_trace_id") else [],
                    status=("available" if metric.status == "ok" else "not_computable"),
                    formula=metric.formula,
                    note=metric.note,
                ))
        except Exception:  # noqa: BLE001
            pass
        return group

    def _assemble_cashflow(self, symbol: str, as_of: str) -> FieldGroup:
        """组装现金流与资本效率字段组。"""
        group = FieldGroup(name="cashflow_and_capital_efficiency")
        if self.statement_repo is None:
            return group
        try:
            stmt = self._get_latest_statement(symbol, as_of)
            if stmt is None:
                return group
            # 直接从现金流量表抽取关键字段
            cf = stmt.cash_flow_statement if hasattr(stmt, "cash_flow_statement") else None
            if cf is None:
                return group
            for line in (cf.lines if hasattr(cf, "lines") else []):
                if line.metric_name in (
                    "operating_cash_flow", "free_cash_flow",
                    "capex", "fcf_yield",
                ):
                    group.fields.append(SnapshotField(
                        name=line.metric_name,
                        value=line.value,
                        unit=line.unit,
                        available_at=stmt.available_at,
                        source_refs=[line.source_ref] if line.source_ref else [],
                        status=("available" if line.grounding_status == "grounding_ok"
                                else "missing"),
                    ))
        except Exception:  # noqa: BLE001
            pass
        return group

    def _assemble_consensus(self, symbol: str, as_of: str) -> FieldGroup:
        """组装一致预期及修正字段组。"""
        group = FieldGroup(name="consensus_and_revisions")
        if self.expectation_repo is None:
            return group
        try:
            estimates = self.expectation_repo.list_estimates(
                symbol=symbol, as_of=as_of,
            ) if hasattr(self.expectation_repo, "list_estimates") else []
            for est in estimates:
                group.fields.append(SnapshotField(
                    name=f"consensus_{est.metric}_{est.period}",
                    value=est.value,
                    unit=est.unit if hasattr(est, "unit") else "",
                    available_at=est.available_at if hasattr(est, "available_at") else as_of,
                    source_refs=[est.source_ref] if hasattr(est, "source_ref") and est.source_ref else [],
                    status="available",
                ))
        except Exception:  # noqa: BLE001
            pass
        return group

    def _assemble_valuation(self, symbol: str, as_of: str) -> FieldGroup:
        """组装估值字段组。"""
        group = FieldGroup(name="valuation")
        if self.valuation_repo is None:
            return group
        try:
            cases = self.valuation_repo.list_cases(
                symbol=symbol, as_of=as_of,
            ) if hasattr(self.valuation_repo, "list_cases") else []
            for case in cases:
                group.fields.append(SnapshotField(
                    name=f"valuation_{case.method}",
                    value=case.value if hasattr(case, "value") else None,
                    unit="倍" if hasattr(case, "method") and "pe" in str(case.method).lower() else "",
                    available_at=case.available_at if hasattr(case, "available_at") else as_of,
                    source_refs=case.source_refs if hasattr(case, "source_refs") else [],
                    status="available",
                ))
        except Exception:  # noqa: BLE001
            pass
        return group

    def _assemble_industry(self, symbol: str, as_of: str) -> FieldGroup:
        """组装产业阶段字段组。"""
        group = FieldGroup(name="industry_timing")
        if self.industry_repo is None:
            return group
        try:
            profile = self.industry_repo.get_latest_profile(
                symbol=symbol, as_of=as_of,
            ) if hasattr(self.industry_repo, "get_latest_profile") else None
            if profile is None:
                return group
            # 抽取产业阶段关键字段
            if hasattr(profile, "lifecycle_stage") and profile.lifecycle_stage:
                group.fields.append(SnapshotField(
                    name="lifecycle_stage",
                    value=None,
                    unit="category",
                    available_at=profile.period if hasattr(profile, "period") else as_of,
                    source_refs=[],
                    status="available",
                    note=str(profile.lifecycle_stage),
                ))
        except Exception:  # noqa: BLE001
            pass
        return group

    def _assemble_operating(self, symbol: str, as_of: str) -> FieldGroup:
        """组装经营驱动完整度字段组。"""
        group = FieldGroup(name="operating_driver_completeness")
        if self.operating_model_repo is None:
            return group
        try:
            model = self.operating_model_repo.get_latest_model(
                symbol=symbol, as_of=as_of,
            ) if hasattr(self.operating_model_repo, "get_latest_model") else None
            if model is None:
                return group
            # 统计 8 类驱动变量的覆盖情况
            driver_kinds = (
                "market_size", "market_share", "nameplate_capacity",
                "utilization_rate", "asp", "product_revenue",
                "gross_margin", "share_count",
            )
            observations = model.observations if hasattr(model, "observations") else []
            for kind in driver_kinds:
                has_obs = any(
                    getattr(obs, "kind", "") == kind for obs in observations
                )
                group.fields.append(SnapshotField(
                    name=f"driver_{kind}",
                    value=1.0 if has_obs else 0.0,
                    unit="indicator",
                    available_at=model.available_at if hasattr(model, "available_at") else as_of,
                    source_refs=[model.source_trace_id] if hasattr(model, "source_trace_id") else [],
                    status="available" if has_obs else "missing",
                ))
        except Exception:  # noqa: BLE001
            pass
        return group

    def _get_latest_statement(self, symbol: str, as_of: str) -> Any | None:
        """获取最近一期报表（兼容多种 API 命名）。"""
        if self.statement_repo is None:
            return None
        # 优先使用 get_latest_statement（P1.10 适配方法）
        if hasattr(self.statement_repo, "get_latest_statement"):
            return self.statement_repo.get_latest_statement(symbol, as_of=as_of)
        # 兜底：列出所有期间，取最新
        if hasattr(self.statement_repo, "list_periods"):
            periods = self.statement_repo.list_periods(symbol)
            for period in sorted(periods, reverse=True):
                stmt = self.statement_repo.get_latest(symbol, period, as_of=as_of)
                if stmt is not None:
                    return stmt
        return None


__all__ = [
    "UniverseEntry",
    "UniverseProvider",
    "StaticUniverseProvider",
    "FieldGroupAssembler",
    "EmptyFieldGroupAssembler",
    "P1BackedFieldGroupAssembler",
    "BuilderConfig",
    "UniverseSnapshotBuilder",
]
