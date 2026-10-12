"""P3.9 认证样本构建器。

按 P3.9 §范围：
- 至少 300 家公司
- 至少 20 个行业
- 至少 5 年 PIT 可见历史；不足 5 年的公司按上市时间记录
- 至少覆盖上行、下行和震荡市场状态

设计要点：
- 通过 company_provider 注入公司数据源，支持真实运行
- 不在工具中硬编码公司列表
- 不足 5 年的公司按实际上市时间记录（不排除）
- 验证范围是否满足 scope 要求，不满足时标记 insufficient
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Protocol

from core.research.pit_certification.contracts import (
    CertificationUniverse,
    CompanyCertificationMember,
    MarketState,
    PITCertificationScope,
)


class CompanyProvider(Protocol):
    """公司数据源协议。

    真实运行时由调用方实现此协议，从数据库或文件加载公司列表。
    """

    def load_companies(self) -> list[CompanyCertificationMember]:
        """加载公司列表。"""
        ...


def build_certification_universe(
    scope: PITCertificationScope,
    company_provider: Callable[[], list[CompanyCertificationMember]] | None = None,
    *,
    market_state_classifier: Callable[[CompanyCertificationMember], frozenset[MarketState]] | None = None,
) -> CertificationUniverse:
    """构建认证样本全集。

    从 company_provider 加载公司列表，验证是否满足 scope 要求。
    不足 5 年的公司按实际上市时间记录，不排除。

    Args:
        scope: 认证范围定义
        company_provider: 公司数据源可调用对象，返回公司成员列表；
            None 时返回空样本（用于 dry-run 或测试）
        market_state_classifier: 可选的市场状态分类器；
            None 时使用公司成员自带的 market_states_covered

    Returns:
        CertificationUniverse: 认证样本全集

    Note:
        本函数不抛异常；样本不足时仍返回样本，由调用方判断是否满足 scope。
    """
    if company_provider is None or not isinstance(scope, PITCertificationScope):
        return CertificationUniverse()

    members = company_provider()
    members = [
        member for member in members
        if isinstance(member, CompanyCertificationMember)
    ] if isinstance(members, (list, tuple)) else []
    if not members:
        return CertificationUniverse()

    # 可选：为每个公司分类市场状态
    if market_state_classifier is not None:
        updated: list[CompanyCertificationMember] = []
        for member in members:
            try:
                states = market_state_classifier(member)
            except (TypeError, ValueError):
                states = member.market_states_covered
            if not isinstance(states, (set, frozenset, list, tuple)):
                states = member.market_states_covered
            states = frozenset(
                state for state in states if isinstance(state, MarketState)
            )
            updated.append(
                CompanyCertificationMember(
                    symbol=member.symbol,
                    company_name=member.company_name,
                    industry=member.industry,
                    sub_industry=member.sub_industry,
                    listed_at=member.listed_at,
                    pit_start=member.pit_start,
                    pit_end=member.pit_end,
                    available_years=member.available_years,
                    market_states_covered=states,
                )
            )
        members = updated

    # 统计行业分布
    industry_distribution: dict[str, int] = {}
    for member in members:
        if not member.industry:
            continue
        industry_distribution[member.industry] = (
            industry_distribution.get(member.industry, 0) + 1
        )

    # 统计市场状态分布
    market_state_distribution: dict[str, int] = {s.value: 0 for s in MarketState}
    for member in members:
        for state in member.market_states_covered:
            if not isinstance(state, MarketState):
                continue
            market_state_distribution[state.value] = (
                market_state_distribution.get(state.value, 0) + 1
            )

    # 计算 PIT 时间窗口
    pit_starts = [m.pit_start for m in members if m.pit_start]
    pit_ends = [m.pit_end for m in members if m.pit_end]
    pit_window_start = min(pit_starts) if pit_starts else ""
    pit_window_end = max(pit_ends) if pit_ends else ""

    return CertificationUniverse(
        members=members,
        industry_distribution=industry_distribution,
        market_state_distribution=market_state_distribution,
        pit_window_start=pit_window_start,
        pit_window_end=pit_window_end,
    )


def validate_universe_scope(
    universe: CertificationUniverse,
    scope: PITCertificationScope,
) -> dict[str, Any]:
    """验证样本是否满足认证范围要求。

    Args:
        universe: 认证样本全集
        scope: 认证范围定义

    Returns:
        dict: 验证结果，包含每个维度的 satisfied 状态和实际值
    """
    actual_companies = universe.total_companies
    actual_industries = universe.total_industries

    # 计算最少可用年数
    available_years_list = [m.available_years for m in universe.members if m.available_years > 0]
    min_actual_years = min(available_years_list) if available_years_list else 0.0

    # 检查市场状态覆盖
    covered_states: set[MarketState] = set()
    for key, value in universe.market_state_distribution.items():
        if value <= 0:
            continue
        try:
            covered_states.add(MarketState(key))
        except (TypeError, ValueError):
            continue
    required_states = scope.require_market_states
    missing_states = required_states - covered_states

    return {
        "companies_satisfied": actual_companies >= scope.min_companies,
        "companies_actual": actual_companies,
        "companies_required": scope.min_companies,
        "industries_satisfied": actual_industries >= scope.min_industries,
        "industries_actual": actual_industries,
        "industries_required": scope.min_industries,
        "years_satisfied": min_actual_years >= scope.min_years,
        "years_actual_min": min_actual_years,
        "years_required": scope.min_years,
        "market_states_satisfied": len(missing_states) == 0,
        "market_states_covered": sorted(s.value for s in covered_states),
        "market_states_required": sorted(s.value for s in required_states),
        "market_states_missing": sorted(s.value for s in missing_states),
        "all_satisfied": (
            actual_companies >= scope.min_companies
            and actual_industries >= scope.min_industries
            and min_actual_years >= scope.min_years
            and len(missing_states) == 0
        ),
    }


def compute_available_years(listed_at: str, pit_end: str) -> float:
    """计算可用 PIT 年数。

    不足 5 年的公司按实际上市时间记录。

    Args:
        listed_at: 上市日期 YYYY-MM-DD
        pit_end: PIT 结束日期 YYYY-MM-DD

    Returns:
        float: 可用年数（保留 1 位小数）
    """
    if not listed_at or not pit_end:
        return 0.0
    try:
        start = datetime.fromisoformat(listed_at[:10])
        end = datetime.fromisoformat(pit_end[:10])
    except (ValueError, TypeError):
        return 0.0
    if end <= start:
        return 0.0
    return round((end - start).days / 365.25, 1)
