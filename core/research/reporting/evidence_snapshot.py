"""P1.10 业务复核字段快照构建器。

按业务人员 5 项复核需求（三表勾稽 / 分部加总 / 前瞻假设 / 估值假设 / 同业口径）
从各领域仓储抽取完整字段与证据，组装为单公司证据快照 dict，配合
``cfa_report_markdown.render_cfa_report_to_markdown`` 即可生成人类可读 Markdown。

设计要点：
- 防御性编程：每个字段缺失时降级 null，绝不抛异常
- 复用各领域 to_dict()，不重新发明数据结构（DRY）
- 数值保持原始精度，Markdown 渲染时再做格式化
- 不输出买入/卖出/仓位/止盈/止损等投资建议（AGENTS.md）
- 不凭空编造公司业务、概念归属、收入占比或证据来源（AGENTS.md）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .contracts import (
    NOTICE,
    OVERALL_STATUS_DEGRADED,
    OVERALL_STATUS_OK,
    OVERALL_STATUS_PARTIAL,
    ReportSection,
    SECTION_STATUS_MISSING,
    SECTION_STATUS_OK,
    SECTION_STATUS_PARTIAL,
    SECTION_TITLES,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


# 关键指标在 NormalizedFinancialStatement 中的取数路径
# (key_metrics 顶层键, statement_type, metric_name 候选列表)
_KEY_METRIC_PATHS: list[tuple[str, str, list[str]]] = [
    ("revenue", "income", ["revenue", "operating_revenue", "total_revenue"]),
    ("net_profit", "income", ["net_profit", "net_income", "profit_attributable_to_parent"]),
    ("total_assets", "balance_sheet", ["total_assets"]),
    ("total_liabilities", "balance_sheet", ["total_liabilities", "total_debt"]),
    ("equity", "balance_sheet", ["total_equity", "equity", "shareholders_equity"]),
    ("operating_cash_flow", "cash_flow", [
        "operating_cash_flow", "net_operating_cash_flow",
        "cash_flow_from_operating", "operating_activities_net_cash_flow",
    ]),
]


def _utc_now() -> str:
    """返回当前 UTC ISO 时间（秒级精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_call(func, *args, **kwargs) -> Any:
    """安全调用函数，异常返回 None。

    Args:
        func: 可调用对象
        *args: 位置参数
        **kwargs: 关键字参数

    Returns:
        函数返回值；异常返回 None
    """
    try:
        return func(*args, **kwargs)
    except Exception:
        return None


def _safe_get(obj: Any, attr: str, default: Any = None) -> Any:
    """安全获取对象属性。"""
    if obj is None:
        return default
    return getattr(obj, attr, default)


def _to_dict_safe(value: Any) -> Any:
    """安全序列化任意对象为 dict 或基础类型。"""
    if value is None:
        return None
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return value.to_dict()
        except Exception:
            return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_dict_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_dict_safe(v) for k, v in value.items()}
    return str(value)


def _extract_key_metrics(
    statement_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    """从 NormalizedFinancialStatement.to_dict() 提取 6 个关键指标。

    Args:
        statement_dict: 三表序列化字典；None 时返回全 null 指标

    Returns:
        dict，每个指标含 value / unit / currency / period / source_trace_id / data_gap
    """
    metrics: dict[str, Any] = {}
    statement_dict = _as_dict(statement_dict)
    if not statement_dict:
        for key, _stmt_type, _names in _KEY_METRIC_PATHS:
            metrics[key] = {
                "value": None, "unit": "", "currency": "",
                "period": "", "source_trace_id": "",
                "data_gap": "no_fundamentals",
            }
        return metrics

    period = str(statement_dict.get("period", ""))
    currency = str(statement_dict.get("currency", ""))
    default_unit = str(statement_dict.get("unit", ""))
    source_trace_id = str(statement_dict.get("source_trace_id", ""))

    stmt_map: dict[str, list[dict[str, Any]]] = {
        "income": _dict_list(statement_dict.get("income_statement", [])),
        "balance_sheet": _dict_list(statement_dict.get("balance_sheet", [])),
        "cash_flow": _dict_list(statement_dict.get("cash_flow_statement", [])),
    }

    for key, stmt_type, names in _KEY_METRIC_PATHS:
        lines = stmt_map.get(stmt_type, [])
        matched_line: dict[str, Any] | None = None
        for line in lines:
            metric_name = str(line.get("metric_name", ""))
            if metric_name in names:
                matched_line = line
                break
        if matched_line is None:
            metrics[key] = {
                "value": None, "unit": default_unit, "currency": currency,
                "period": period, "source_trace_id": source_trace_id,
                "data_gap": f"metric_not_found_in_{stmt_type}",
            }
            continue
        metrics[key] = {
            "value": matched_line.get("value"),
            "unit": str(matched_line.get("unit", default_unit) or default_unit),
            "currency": str(matched_line.get("currency", currency) or currency),
            "period": period,
            "source_trace_id": str(matched_line.get("source_ref", "") or source_trace_id),
            "data_gap": "" if matched_line.get("value") is not None else "value_is_null",
        }
    return metrics


def _compute_balance_check(
    key_metrics: dict[str, Any],
    statement_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    """手工计算三表勾稽。

    - asset_liability_equity_diff = total_assets - total_liabilities - equity
    - cash_reconciliation_diff = 期末现金 - 期初现金 - 现金净增加额
      （若 cash_flow_statement 含 cash_begin / cash_end / net_increase 三个 metric）

    Args:
        key_metrics: _extract_key_metrics 输出
        statement_dict: 三表序列化字典

    Returns:
        勾稽检查结果 dict；缺字段时对应字段为 null
    """
    total_assets = _as_dict(key_metrics.get("total_assets")).get("value")
    total_liabilities = _as_dict(key_metrics.get("total_liabilities")).get("value")
    equity = _as_dict(key_metrics.get("equity")).get("value")

    if total_assets is None or total_liabilities is None or equity is None:
        diff: float | None = None
        balanced: bool | None = None
    else:
        try:
            diff = float(total_assets) - float(total_liabilities) - float(equity)
            balanced = abs(diff) < 1.0
        except (TypeError, ValueError):
            diff = None
            balanced = None

    # 现金流勾稽（可选）
    cash_diff: float | None = None
    cash_explained: bool | None = None
    if statement_dict:
        cf_lines = _dict_list(statement_dict.get("cash_flow_statement", []))
        cf_map: dict[str, float] = {}
        for line in cf_lines:
            name = str(line.get("metric_name", ""))
            value = line.get("value")
            if name and value is not None:
                try:
                    cf_map[name] = float(value)
                except (TypeError, ValueError):
                    continue
        def _first_metric(*names: str) -> float | None:
            return next((cf_map[name] for name in names if name in cf_map), None)

        # 兼容标准别名与 P1DataLoader 实际保存的字段名。
        begin = _first_metric(
            "cash_begin", "cash_at_beginning", "beginning_cash",
            "cash_and_equivalents_begin",
        )
        end = _first_metric(
            "cash_end", "cash_at_end", "ending_cash",
            "cash_and_equivalents_end",
        )
        net_increase = _first_metric(
            "net_increase_in_cash", "net_cash_increase", "cash_net_increase",
            "cash_change",
        )
        if begin is not None and end is not None and net_increase is not None:
            try:
                cash_diff = float(end) - float(begin) - float(net_increase)
                cash_explained = abs(cash_diff) < 1.0
            except (TypeError, ValueError):
                cash_diff = None
                cash_explained = None

    notes: list[str] = []
    if diff is None:
        notes.append("asset_liability_equity_check_unavailable")
    elif not balanced:
        notes.append(f"asset_liability_equity_unbalanced_diff={diff}")
    if cash_diff is not None and not cash_explained:
        notes.append(f"cash_reconciliation_unbalanced_diff={cash_diff}")

    return {
        "asset_liability_equity_diff": diff,
        "asset_liability_equity_balanced": balanced,
        "cash_reconciliation_diff": cash_diff,
        "cash_reconciliation_explained": cash_explained,
        "notes": notes,
    }


def _build_valuation_cases(
    valuation_repo: Any,
    symbol: str,
    as_of: str,
) -> list[dict[str, Any]]:
    """从 valuation_repo 取所有案例，保留业务复核需要的完整字段。

    Args:
        valuation_repo: ValuationRepository 或兼容仓储
        symbol: 公司代码
        as_of: PIT 时点

    Returns:
        案例列表，每个含 case_id / method / as_of / scenario / output_interval /
        output_per_share / assumptions / formula_trace / source_refs /
        comparable_companies / sensitivity / computability / limitations / model_version
    """
    if valuation_repo is None:
        return []
    cases = _safe_call(
        lambda: valuation_repo.list_cases(
            company_symbol=symbol, as_of=as_of, limit=50,
        )
    )
    if not cases:
        return []
    result: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            case = _to_dict_safe(case) or {}
        result.append({
            "case_id": str(case.get("case_id", "")),
            "method": str(case.get("method", "")),
            "as_of": str(case.get("as_of", "")),
            "scenario": str(case.get("scenario", "")),
            "output_interval": case.get("output_interval") or {"low": None, "high": None},
            "output_per_share": case.get("output_per_share") or {"low": None, "high": None},
            "assumptions": list(case.get("assumptions", []) or []),
            "formula_trace": list(case.get("formula_trace", []) or []),
            "source_refs": list(case.get("source_refs", []) or []),
            "comparable_companies": list(case.get("inputs_snapshot", {}).get("comparable_companies", []) or []),
            "sensitivity": list(case.get("sensitivity", []) or []),
            "computability": str(case.get("computability", "")),
            "limitations": list(case.get("limitations", []) or []),
            "model_version": str(case.get("model_version", "")),
            "inputs_snapshot": case.get("inputs_snapshot") or {},
            "created_at": str(case.get("created_at", "")),
        })
    return result


def _build_consensus_estimates(
    expectation_repo: Any,
    symbol: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """从 expectation_repo 取当前公司的一致预期，保留分机构/分分析师字段。

    Args:
        expectation_repo: ExpectationRepository 或兼容仓储
        symbol: 公司代码
        limit: 上限，默认 500

    Returns:
        一致预期列表，每条含 consensus_id / company_symbol / provider /
        institution_id / analyst_id / values
    """
    if expectation_repo is None:
        return []
    rows = _safe_call(
        lambda: expectation_repo.list("consensus_estimates", limit=limit)
    )
    if not rows:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("company_symbol", "")) != symbol:
            continue
        result.append({
            "consensus_id": str(row.get("consensus_id", "")),
            "company_symbol": str(row.get("company_symbol", "")),
            "provider": str(row.get("provider", "")),
            "institution_id": str(row.get("institution_id", "")),
            "analyst_id": str(row.get("analyst_id", "")),
            "sample_size": row.get("sample_size"),
            "revision_of": str(row.get("revision_of", "")),
            "values": list(row.get("values", []) or []),
            "created_at": str(row.get("created_at", "")),
        })
    return result


def _build_cfa_report_summary(cfa_report: Any) -> dict[str, Any] | None:
    """构建 CFA 报告 10 章节状态摘要。

    Args:
        cfa_report: CfaReport 对象或 dict；None 返回 None

    Returns:
        dict 含 section / title / section_status / source_refs_count 列表
    """
    if cfa_report is None:
        return None
    report_dict = _to_dict_safe(cfa_report)
    if not isinstance(report_dict, dict):
        return None
    sections = report_dict.get("sections", []) or []
    summary_sections: list[dict[str, Any]] = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        summary_sections.append({
            "section": str(sec.get("section", "")),
            "title": str(sec.get("title", "")),
            "section_status": str(sec.get("section_status", "")),
            "source_refs_count": len(sec.get("source_refs", []) or []),
            "limitations_count": len(sec.get("limitations", []) or []),
        })
    return {
        "report_id": str(report_dict.get("report_id", "")),
        "overall_status": str(report_dict.get("overall_status", "")),
        "sections": summary_sections,
    }


def _collect_data_gaps(
    fundamentals_latest: dict[str, Any] | None,
    key_metrics: dict[str, Any],
    balance_check: dict[str, Any],
    valuation_cases: list[dict[str, Any]],
    consensus_estimates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """汇总所有缺失字段与原因。

    Args:
        fundamentals_latest: 三表序列化字典
        key_metrics: 关键指标 dict
        balance_check: 三表勾稽结果
        valuation_cases: 估值案例列表
        consensus_estimates: 一致预期列表

    Returns:
        data_gap 列表，每项含 field / reason
    """
    gaps: list[dict[str, str]] = []
    if fundamentals_latest is None:
        gaps.append({"field": "fundamentals_latest", "reason": "no_fundamentals"})
    else:
        for stmt_key in ("income_statement", "balance_sheet", "cash_flow_statement"):
            if not fundamentals_latest.get(stmt_key):
                gaps.append({"field": f"fundamentals_latest.{stmt_key}", "reason": "empty"})
    for metric_key, metric_val in key_metrics.items():
        if not isinstance(metric_val, dict):
            continue
        if metric_val.get("value") is None:
            gaps.append({
                "field": f"key_metrics.{metric_key}",
                "reason": str(metric_val.get("data_gap", "value_is_null")),
            })
    if balance_check.get("asset_liability_equity_balanced") is None:
        gaps.append({
            "field": "balance_check.asset_liability_equity",
            "reason": "missing_required_metrics",
        })
    if not valuation_cases:
        gaps.append({"field": "valuation_cases", "reason": "no_valuation_cases"})
    if not consensus_estimates:
        gaps.append({"field": "consensus_estimates", "reason": "no_consensus_estimates"})
    return gaps


def _build_review_checklist_hints(
    balance_check: dict[str, Any],
    valuation_cases: list[dict[str, Any]],
    fundamentals_latest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """构建 5 项复核提示，告诉业务人员每项该看哪个字段。

    Args:
        balance_check: 三表勾稽结果
        valuation_cases: 估值案例列表
        fundamentals_latest: 三表序列化字典

    Returns:
        5 项复核提示列表，每项含 item / field_location / pass_criteria /
        computed_result / data_availability
    """
    has_fundamentals = fundamentals_latest is not None
    has_valuation = bool(valuation_cases)

    # 三表勾稽已有计算结果
    ale_balanced = balance_check.get("asset_liability_equity_balanced")
    cash_explained = balance_check.get("cash_reconciliation_explained")
    if ale_balanced is True and cash_explained is True:
        ale_result = "通过（diff < 1.0）"
    elif ale_balanced is False:
        ale_result = f"不通过（diff={balance_check.get('asset_liability_equity_diff')}）"
    elif cash_explained is False:
        ale_result = f"不通过（现金流勾稽 diff={balance_check.get('cash_reconciliation_diff')}）"
    elif ale_balanced is True:
        ale_result = "无法计算（现金流勾稽缺字段）"
    else:
        ale_result = "无法计算（缺字段）"

    if not has_fundamentals:
        balance_availability = "missing"
    elif ale_balanced is None or cash_explained is None:
        balance_availability = "partial"
    else:
        balance_availability = "available"

    # 估值假设来源齐全度
    val_assumptions_count = 0
    val_assumptions_with_source = 0
    for case in valuation_cases:
        for asm in case.get("assumptions", []) or []:
            val_assumptions_count += 1
            if str(asm.get("source_ref", "")).strip():
                val_assumptions_with_source += 1

    # 可比公司数据
    comparable_count = 0
    for case in valuation_cases:
        comparable_count += len(case.get("comparable_companies", []) or [])

    return [
        {
            "item": "1_三表勾稽",
            "field_location": "balance_check / fundamentals_latest.{income_statement,balance_sheet,cash_flow_statement}",
            "pass_criteria": "asset_liability_equity_balanced=true (diff<1.0) 且 cash_reconciliation_explained=true",
            "computed_result": ale_result,
            "data_availability": balance_availability,
        },
        {
            "item": "2_分部收入加总",
            "field_location": "fundamentals_latest.income_statement[metric_name=revenue] vs OperatingModel.segments[*].product_revenue",
            "pass_criteria": "分部收入加总 = 利润表营业收入（误差 < 1%）; 划分口径可追溯",
            "computed_result": "需人工核对（OperatingModel 未在本快照中）",
            "data_availability": "available" if has_fundamentals else "missing",
        },
        {
            "item": "3_前瞻假设",
            "field_location": "valuation_cases[*].assumptions[*] (name/value/source_ref/as_of)",
            "pass_criteria": "100% 前瞻假设有 source_ref，且来源可追溯",
            "computed_result": (
                f"{val_assumptions_with_source}/{val_assumptions_count} 个假设有 source_ref"
                if val_assumptions_count > 0 else "暂无前瞻假设"
            ),
            "data_availability": "available" if val_assumptions_count > 0 else "missing",
        },
        {
            "item": "4_估值关键假设",
            "field_location": "valuation_cases[*].assumptions[*].source_ref",
            "pass_criteria": "100% 估值关键假设（WACC/永续增长率/Beta/风险溢价/折现率/倍数中位数）有 source_ref",
            "computed_result": (
                f"{val_assumptions_with_source}/{val_assumptions_count} 个假设有 source_ref"
                if val_assumptions_count > 0 else "暂无估值假设"
            ),
            "data_availability": "available" if val_assumptions_with_source > 0 else "missing",
        },
        {
            "item": "5_同业可比口径",
            "field_location": "valuation_cases[*].comparable_companies",
            "pass_criteria": "可比公司同行业、相似规模、相似业务结构; 财务指标口径统一（PE TTM/Forward、PB LTM/MRQ）",
            "computed_result": f"共 {comparable_count} 家可比公司",
            "data_availability": "available" if comparable_count > 0 else "missing",
        },
    ]


def build_company_evidence_snapshot(
    symbol: str,
    as_of: str,
    fundamentals_repo: Any,
    expectation_repo: Any,
    valuation_repo: Any,
    company_meta: dict[str, str] | None = None,
    cfa_report: Any = None,
    load_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建单公司完整证据快照，供业务人员做 5 项复核。

    Args:
        symbol: 公司代码（如 "600519.SH"）
        as_of: PIT 时点（YYYY-MM-DD）
        fundamentals_repo: StatementRepository，提供 get_latest_statement
        expectation_repo: ExpectationRepository，提供 list 方法
        valuation_repo: ValuationRepository，提供 list_cases 方法
        company_meta: 公司元数据 dict，含 name / industry 等；None 时降级
        cfa_report: CfaReport 对象或 dict；None 时 cfa_report_summary=None
        load_result: 原 LoadResult 统计 dict，向后兼容；None 时不附加

    Returns:
        完整证据快照 dict，包含：
        - meta: 元数据（symbol/name/industry/as_of/generated_at/data_source/notice）
        - fundamentals_latest: NormalizedFinancialStatement.to_dict()
        - key_metrics: 6 个关键指标
        - balance_check: 三表勾稽结果
        - valuation_cases: 估值案例列表
        - consensus_estimates: 一致预期列表
        - cfa_report_summary: CFA 报告 10 章节状态摘要
        - data_gaps: 缺失字段列表
        - review_checklist_hints: 5 项复核提示
        - load_result: 原 LoadResult 统计（向后兼容）
    """
    company_meta = company_meta or {}
    name = str(company_meta.get("name", ""))
    industry = str(company_meta.get("industry", ""))
    data_source = str(company_meta.get("data_source", "tushare"))

    # 1. 取三表
    statement_obj = None
    if fundamentals_repo is not None:
        statement_obj = _safe_call(
            lambda: fundamentals_repo.get_latest_statement(symbol, as_of)
        )
    fundamentals_latest: dict[str, Any] | None = None
    if statement_obj is not None:
        fundamentals_latest = _to_dict_safe(statement_obj)
        if not isinstance(fundamentals_latest, dict):
            fundamentals_latest = None

    # 2. 关键指标
    key_metrics = _extract_key_metrics(fundamentals_latest)

    # 3. 三表勾稽
    balance_check = _compute_balance_check(key_metrics, fundamentals_latest)

    # 4. 估值案例
    valuation_cases = _build_valuation_cases(valuation_repo, symbol, as_of)

    # 5. 一致预期
    consensus_estimates = _build_consensus_estimates(expectation_repo, symbol)

    # 6. CFA 报告摘要
    cfa_report_summary = _build_cfa_report_summary(cfa_report)

    # 7. 数据缺口
    data_gaps = _collect_data_gaps(
        fundamentals_latest, key_metrics, balance_check,
        valuation_cases, consensus_estimates,
    )

    # 8. 复核提示
    review_checklist_hints = _build_review_checklist_hints(
        balance_check, valuation_cases, fundamentals_latest,
    )

    snapshot: dict[str, Any] = {
        "meta": {
            "symbol": symbol,
            "name": name,
            "industry": industry,
            "as_of": as_of,
            "generated_at": _utc_now(),
            "data_source": data_source,
            "notice": NOTICE,
        },
        "fundamentals_latest": fundamentals_latest,
        "key_metrics": key_metrics,
        "balance_check": balance_check,
        "valuation_cases": valuation_cases,
        "consensus_estimates": consensus_estimates,
        "cfa_report_summary": cfa_report_summary,
        "data_gaps": data_gaps,
        "review_checklist_hints": review_checklist_hints,
    }
    if load_result is not None:
        snapshot["load_result"] = load_result
    return snapshot
