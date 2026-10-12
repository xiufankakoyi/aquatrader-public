"""CFA 风格研究报告 Markdown 渲染器。

把 ``CfaReport`` 10 章节渲染为业务人员易读的 Markdown，含：
- 顶部公司元数据 + 固定 notice
- 10 章节按顺序渲染（标题 + 状态图标 + 结构化内容 + 限制说明 + 来源引用）
- 字段位置速查表（业务人员快速定位复核字段）
- 人工复核 Checklist 提示（5 项复核指引）
- 复核结论表格（业务人员填写）

数值格式统一（AGENTS.md）：
- 百分比 2 位
- 分数 4 位
- 金额用 万/亿 单位（原始值是元，自动转换显示）
- Interval 区间渲染为 ``[low, high]``
- 缺字段降级显示「暂无数据」

不输出买入/卖出/仓位/止盈/止损等投资建议（AGENTS.md）。
"""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any, Iterable

from .contracts import (
    NOTICE,
    OVERALL_STATUS_DEGRADED,
    OVERALL_STATUS_OK,
    OVERALL_STATUS_PARTIAL,
    ReportSection,
    ReportSectionContent,
    SECTION_STATUS_MISSING,
    SECTION_STATUS_NOT_APPLICABLE,
    SECTION_STATUS_OK,
    SECTION_STATUS_PARTIAL,
    SECTION_TITLES,
    CfaReport,
)


# 章节状态图标
_STATUS_ICONS: dict[str, str] = {
    SECTION_STATUS_OK: "✓",
    SECTION_STATUS_PARTIAL: "⚠",
    SECTION_STATUS_MISSING: "✗",
    SECTION_STATUS_NOT_APPLICABLE: "—",
}

# 整体状态中文标签
_OVERALL_STATUS_LABELS: dict[str, str] = {
    OVERALL_STATUS_OK: "完整",
    OVERALL_STATUS_PARTIAL: "部分降级",
    OVERALL_STATUS_DEGRADED: "降级",
}


# ---------------------------------------------------------------------------
# 数值格式化
# ---------------------------------------------------------------------------


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None

def _format_amount(value: Any, unit: str = "元") -> str:
    """金额格式化：原始值（元）转换为 万/亿 单位显示。

    Args:
        value: 数值；None 返回「暂无数据」
        unit: 原始单位（默认 "元"）；非元单位直接显示原值 + 单位

    Returns:
        格式化字符串，如 "1.23 亿元" / "4567.89 万元" / "123 元"
    """
    if value is None:
        return "暂无数据"
    v = _finite_float(value)
    if v is None:
        if isinstance(value, (float, int)):
            return "暂无数据"
        return str(value)
    if unit and unit != "元":
        return f"{v:,.2f} {unit}"
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.2f} 亿元"
    if abs(v) >= 1e4:
        return f"{v / 1e4:,.2f} 万元"
    return f"{v:,.2f} 元"


def _format_percent(value: Any) -> str:
    """百分比格式化（2 位小数）。

    Args:
        value: 0-1 之间的小数（如 0.1234 → "12.34%"）；
               若绝对值 > 1 视为已是百分比，直接附加 %

    Returns:
        格式化字符串；None 返回「暂无数据」
    """
    if value is None:
        return "暂无数据"
    v = _finite_float(value)
    if v is None:
        if isinstance(value, (float, int)):
            return "暂无数据"
        return str(value)
    if abs(v) > 1.0:
        return f"{v:,.2f}%"
    return f"{v * 100:,.2f}%"


def _format_ratio(value: Any) -> str:
    """分数/比率格式化（4 位小数）。"""
    if value is None:
        return "暂无数据"
    number = _finite_float(value)
    if number is None:
        if isinstance(value, (float, int)):
            return "暂无数据"
        return str(value)
    return f"{number:,.4f}"


def _format_interval(value: Any) -> str:
    """区间格式化为 ``[low, high]``。"""
    if value is None:
        return "暂无数据"
    if isinstance(value, Mapping):
        low = value.get("low")
        high = value.get("high")
        if low is None and high is None:
            return "暂无数据"
        if low == high:
            return f"[{_format_amount(low)}]"
        return f"[{_format_amount(low)}, {_format_amount(high)}]"
    return str(value)


def _format_value(value: Any, hint: str = "") -> str:
    """根据 hint 提示选择格式化方法。

    Args:
        value: 任意值
        hint: "percent" / "ratio" / "amount" / "interval" / "money" 之一
    """
    if hint in ("percent",):
        return _format_percent(value)
    if hint in ("ratio",):
        return _format_ratio(value)
    if hint in ("amount", "money"):
        return _format_amount(value)
    if hint in ("interval",):
        return _format_interval(value)
    # 自动推断：dict 含 low/high → interval；浮点数判断是否像金额
    if isinstance(value, Mapping) and "low" in value and "high" in value:
        return _format_interval(value)
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite_float(value)
        return "暂无数据" if number is None else f"{number:,.4f}"
    if value is None:
        return "暂无数据"
    if isinstance(value, str) and not value:
        return "暂无数据"
    return str(value)


# ---------------------------------------------------------------------------
# 递归渲染 dict / list
# ---------------------------------------------------------------------------

# 关键词匹配 → 格式化 hint
_AMOUNT_KEYS: tuple[str, ...] = (
    "revenue", "net_profit", "total_assets", "total_liabilities", "equity",
    "operating_cash_flow", "value", "amount", "market_cap", "total_debt",
    "cash_and_equivalents", "minority_interest", "ebitda_ttm", "fcf_ttm",
    "output_low", "output_high", "low", "high", "mid", "trailing_eps",
    "book_value_per_share", "revenue_per_share", "consensus_eps",
    "consensus_net_profit", "consensus_revenue", "dividends_per_share",
    "current_price", "shares_outstanding", "diluted_shares",
    "terminal_value_pct", "implied_growth", "output_per_share",
)
_PERCENT_KEYS: tuple[str, ...] = (
    "wacc", "risk_free_rate", "risk_premium", "terminal_growth",
    "cost_of_debt", "growth_rate", "margin", "tax_rate",
    "historical_percentile", "crowding", "probability",
    "confidence", "uncertainty", "discount_rate",
)
_RATIO_KEYS: tuple[str, ...] = (
    "beta", "pe_ratio", "pb_ratio", "peg", "ev_ebitda", "fcf_yield",
    "leverage", "current_ratio", "quick_ratio",
)


def _infer_hint(key: str) -> str:
    """根据 key 名称推断格式化 hint。"""
    key_lower = key.lower()
    for k in _AMOUNT_KEYS:
        if k in key_lower:
            return "amount"
    for k in _PERCENT_KEYS:
        if k in key_lower:
            return "percent"
    for k in _RATIO_KEYS:
        if k in key_lower:
            return "ratio"
    return ""


def _render_scalar(key: str, value: Any) -> str:
    """渲染标量值为「key: value」形式。"""
    hint = _infer_hint(key)
    return f"- **{key}**: {_format_value(value, hint)}"


def _render_dict(
    data: Mapping[str, Any],
    indent: int = 0,
) -> list[str]:
    """递归渲染 dict 为 Markdown 项目符号列表。

    Args:
        data: 待渲染 dict
        indent: 当前缩进层级（每层 2 空格）

    Returns:
        Markdown 行列表
    """
    if not isinstance(data, Mapping) or not data:
        return [f"{'  ' * indent}- 暂无数据"]
    lines: list[str] = []
    pad = "  " * indent
    for key, value in data.items():
        if value is None or (isinstance(value, str) and not value):
            lines.append(f"{pad}- **{key}**: 暂无数据")
            continue
        if isinstance(value, Mapping):
            # 区间特殊处理
            if "low" in value and "high" in value and len(value) <= 4:
                hint = _infer_hint(key)
                lines.append(f"{pad}- **{key}**: {_format_value(value, hint)}")
                continue
            lines.append(f"{pad}- **{key}**:")
            lines.extend(_render_dict(value, indent + 1))
        elif isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{pad}- **{key}**: 暂无数据")
                continue
            lines.append(f"{pad}- **{key}**:")
            lines.extend(_render_list(value, indent + 1))
        else:
            lines.append(f"{pad}{_render_scalar(key, value)}")
    return lines


def _render_list(
    data: list[Any] | tuple[Any, ...],
    indent: int = 0,
) -> list[str]:
    """递归渲染 list。

    list of dict 用表格（最多 5 列）；list of scalar 用项目符号。

    Args:
        data: 待渲染列表
        indent: 当前缩进层级

    Returns:
        Markdown 行列表
    """
    if not data:
        return [f"{'  ' * indent}- 暂无数据"]
    pad = "  " * indent
    # 列表元素为标量
    if all(not isinstance(item, (Mapping, list, tuple)) for item in data):
        return [f"{pad}- {_format_value(item)}" for item in data]
    # 列表元素为 dict
    if all(isinstance(item, Mapping) for item in data):
        return _render_dict_list_as_table(
            [dict(item) for item in data], indent
        )
    # 混合：项目符号
    lines: list[str] = []
    for item in data:
        if isinstance(item, Mapping):
            lines.append(f"{pad}-")
            lines.extend(_render_dict(item, indent + 1))
        elif isinstance(item, (list, tuple)):
            lines.append(f"{pad}-")
            lines.extend(_render_list(item, indent + 1))
        else:
            lines.append(f"{pad}- {_format_value(item)}")
    return lines


def _render_dict_list_as_table(
    data: list[dict[str, Any]],
    indent: int = 0,
) -> list[str]:
    """将 dict 列表渲染为 Markdown 表格。

    最多显示 6 列；超出字段省略。空值统一显示「-」。

    Args:
        data: dict 列表
        indent: 表格缩进（Markdown 表格本身不支持缩进，此处用于前置空行）

    Returns:
        Markdown 行列表
    """
    data = [item for item in data if isinstance(item, Mapping)]
    if not data:
        return [f"{'  ' * indent}- 暂无数据"]
    # 收集字段并按出现频次排序
    field_count: dict[str, int] = {}
    for item in data:
        for k in item.keys():
            if isinstance(k, str):
                field_count[k] = field_count.get(k, 0) + 1
    fields = sorted(field_count.keys(), key=lambda k: -field_count[k])[:6]
    if not fields:
        return [f"{'  ' * indent}- 暂无数据"]
    pad = "  " * indent
    header = f"{pad}| " + " | ".join(fields) + " |"
    separator = f"{pad}| " + " | ".join(["---"] * len(fields)) + " |"
    lines = [header, separator]
    for item in data:
        row_cells = []
        for field in fields:
            v = item.get(field)
            if v is None:
                row_cells.append("-")
            elif isinstance(v, Mapping):
                if "low" in v and "high" in v:
                    row_cells.append(_format_interval(v))
                else:
                    # 截断显示
                    s = str(v)
                    row_cells.append(s if len(s) <= 30 else s[:27] + "...")
            elif isinstance(v, list):
                row_cells.append(f"[{len(v)} 项]" if v else "-")
            elif isinstance(v, str):
                row_cells.append(v if len(v) <= 30 else v[:27] + "...")
            elif isinstance(v, bool):
                row_cells.append("是" if v else "否")
            else:
                hint = _infer_hint(field)
                row_cells.append(_format_value(v, hint))
        lines.append(f"{pad}| " + " | ".join(row_cells) + " |")
    return lines


# ---------------------------------------------------------------------------
# 章节渲染
# ---------------------------------------------------------------------------

def render_cfa_report_section_to_markdown(
    section: ReportSectionContent,
    level: int = 2,
) -> str:
    """渲染单章节为 Markdown。

    Args:
        section: ReportSectionContent 对象
        level: 标题层级，默认 2（##）

    Returns:
        Markdown 字符串
    """
    if section is None or not isinstance(section, ReportSectionContent):
        return ""
    icon = _STATUS_ICONS.get(section.section_status, "?")
    title = section.title or SECTION_TITLES.get(section.section, section.section)
    header = "#" * level
    lines: list[str] = [f"{header} {title} [{icon} {section.section_status}]", ""]

    content = section.content if isinstance(section.content, Mapping) else {}
    if content:
        lines.extend(_render_dict(content, indent=0))
        lines.append("")
    else:
        lines.append("暂无数据")
        lines.append("")

    limitations = (
        section.limitations
        if isinstance(section.limitations, (list, tuple))
        else []
    )
    if limitations:
        lines.append("**限制说明：**")
        for lim in limitations:
            lines.append(f"- {lim}")
        lines.append("")
    else:
        lines.append("**限制说明：** 无")
        lines.append("")

    source_refs = (
        section.source_refs
        if isinstance(section.source_refs, (list, tuple))
        else []
    )
    if source_refs:
        lines.append("**来源引用（source_refs）：**")
        for ref in source_refs:
            lines.append(f"- {ref}")
        lines.append("")
    else:
        lines.append("**来源引用（source_refs）：** 暂无")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 字段速查 & Checklist
# ---------------------------------------------------------------------------


def _section_content(section: ReportSectionContent | None) -> Mapping[str, Any]:
    if section is None or not isinstance(section.content, Mapping):
        return {}
    return section.content


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]

def _build_field_lookup_table(report: CfaReport) -> list[str]:
    """构建字段位置速查表（业务人员定位 5 项复核字段）。

    Args:
        report: CfaReport 对象

    Returns:
        Markdown 行列表
    """
    lines: list[str] = [
        "## 11. 字段位置速查表",
        "",
        "| 复核项 | 字段位置 | 章节 |",
        "| --- | --- | --- |",
        "| 1. 三表勾稽 | `balance_check` / `key_metrics` / 章节 4 `financial_quality.statement` | 4 |",
        "| 2. 分部收入加总 | 章节 2 `strategy_business_model.segments` / 章节 4 `statement.income_statement[revenue]` | 2, 4 |",
        "| 3. 前瞻假设 | 章节 5 `drivers_forecast.forward_assumptions` / 章节 7 `valuation_cases[*].assumptions[*]` | 5, 7 |",
        "| 4. 估值关键假设 | 章节 7 `valuation_cases[*].assumptions[*].source_ref` | 7 |",
        "| 5. 同业可比口径 | 章节 7 `valuation_cases[*].comparable_companies` | 7 |",
        "",
    ]
    return lines


def _build_review_checklist_hints(report: CfaReport) -> list[str]:
    """构建 5 项人工复核 Checklist 提示。

    从 CfaReport 各章节内容中提取统计信息（不编造数据），告诉业务人员
    每项该看哪个字段、通过标准、当前数据可用性。

    Args:
        report: CfaReport 对象

    Returns:
        Markdown 行列表
    """
    # 章节 4：取 statement
    sec4 = report.get_section(ReportSection.FINANCIAL_QUALITY.value)
    sec4_content = _section_content(sec4)
    statement = sec4_content.get("statement")
    statement = statement if isinstance(statement, Mapping) else {}
    has_statement = bool(statement)
    # 关键指标
    revenue = None
    total_assets = None
    total_liabilities = None
    equity = None
    if has_statement:
        for line in _mapping_rows(statement.get("income_statement")):
            if str(line.get("metric_name", "")) in ("revenue", "operating_revenue"):
                revenue = line.get("value")
                break
        for line in _mapping_rows(statement.get("balance_sheet")):
            mn = str(line.get("metric_name", ""))
            if mn in ("total_assets",):
                total_assets = line.get("value")
            elif mn in ("total_liabilities", "total_debt"):
                total_liabilities = line.get("value")
            elif mn in ("total_equity", "equity", "shareholders_equity"):
                equity = line.get("value")

    ale_balanced: bool | None = None
    ale_diff: float | None = None
    if total_assets is not None and total_liabilities is not None and equity is not None:
        try:
            ale_diff = float(total_assets) - float(total_liabilities) - float(equity)
            ale_balanced = abs(ale_diff) < 1.0
        except (TypeError, ValueError):
            pass

    # 章节 2：取 segments
    sec2 = report.get_section(ReportSection.STRATEGY_BUSINESS_MODEL.value)
    sec2_content = _section_content(sec2)
    segments = sec2_content.get("segments", [])
    segments = segments if isinstance(segments, (list, tuple)) else []
    segments_count = len(segments)

    # 章节 5：取 forward_assumptions
    sec5 = report.get_section(ReportSection.DRIVERS_AND_FORECAST.value)
    sec5_content = _section_content(sec5)
    forward_assumptions = sec5_content.get("forward_assumptions", [])
    forward_assumptions = (
        forward_assumptions if isinstance(forward_assumptions, (list, tuple)) else []
    )
    fa_count = len(forward_assumptions)
    fa_with_source = sum(
        1 for asm in forward_assumptions
        if isinstance(asm, Mapping) and str(asm.get("source_ref", "")).strip()
    )

    # 章节 7：取 valuation_cases
    sec7 = report.get_section(ReportSection.VALUATION_SENSITIVITY.value)
    sec7_content = _section_content(sec7)
    val_cases = sec7_content.get("valuation_cases", [])
    val_cases = val_cases if isinstance(val_cases, (list, tuple)) else []
    val_assumptions_count = 0
    val_assumptions_with_source = 0
    comparable_count = 0
    for case in val_cases:
        if not isinstance(case, Mapping):
            continue
        for asm in _mapping_rows(case.get("assumptions")):
            val_assumptions_count += 1
            if str(asm.get("source_ref", "")).strip():
                val_assumptions_with_source += 1
        # comparable_companies 在 inputs_snapshot 中
        inputs_snapshot = case.get("inputs_snapshot")
        if isinstance(inputs_snapshot, Mapping):
            comp = inputs_snapshot.get("comparable_companies", [])
            comparable_count += len(comp) if isinstance(comp, (list, tuple)) else 0

    # 计算结果文案
    if ale_balanced is True:
        ale_result = f"通过（diff={ale_diff}，< 1.0）"
    elif ale_balanced is False:
        ale_result = f"不通过（diff={ale_diff}）"
    else:
        ale_result = "无法计算（缺字段）"

    fa_result = (
        f"{fa_with_source}/{fa_count} 个前瞻假设有 source_ref"
        if fa_count > 0 else "暂无前瞻假设"
    )
    val_asm_result = (
        f"{val_assumptions_with_source}/{val_assumptions_count} 个估值假设有 source_ref"
        if val_assumptions_count > 0 else "暂无估值假设"
    )
    comp_result = f"共 {comparable_count} 家可比公司"

    lines: list[str] = [
        "## 12. 人工复核 Checklist 提示",
        "",
        "> 本章节告诉业务人员该看哪些字段做 5 项复核；",
        "> 实际复核结论请在末尾「复核结论表格」填写，签名 + 日期后归档。",
        "",
        "### 12.1 三表勾稽",
        "",
        f"- **字段位置**：章节 4 `financial_quality.statement.{'{income_statement,balance_sheet,cash_flow_statement}'}`",
        "- **通过标准**：资产 = 负债 + 所有者权益（diff < 1.0）；现金流间接法勾稽平衡",
        f"- **已计算结果**：{ale_result}",
        f"- **数据可用性**：{'available' if has_statement else 'missing'}",
        "",
        "### 12.2 分部收入加总",
        "",
        "- **字段位置**：章节 2 `strategy_business_model.segments[*].product_revenue` 与章节 4 `statement.income_statement[metric_name=revenue]`",
        "- **通过标准**：分部收入加总 = 利润表营业收入（误差 < 1%）；划分口径可追溯",
        f"- **当前数据**：共 {segments_count} 个分部；营业收入 = {_format_amount(revenue)}",
        f"- **数据可用性**：{'available' if segments_count > 0 else 'missing'}",
        "",
        "### 12.3 前瞻假设",
        "",
        "- **字段位置**：章节 5 `drivers_forecast.forward_assumptions[*].source_ref` 与章节 7 `valuation_cases[*].assumptions[*].source_ref`",
        "- **通过标准**：100% 前瞻假设有 `source_ref`，且来源可追溯",
        f"- **已统计结果**：{fa_result}",
        f"- **数据可用性**：{'available' if fa_count > 0 else 'missing'}",
        "",
        "### 12.4 估值关键假设",
        "",
        "- **字段位置**：章节 7 `valuation_cases[*].assumptions[*].source_ref`（WACC / 永续增长率 / Beta / 风险溢价 / 折现率 / 倍数中位数）",
        "- **通过标准**：100% 估值关键假设有 `source_ref`，且来源可追溯",
        f"- **已统计结果**：{val_asm_result}",
        f"- **数据可用性**：{'available' if val_assumptions_with_source > 0 else 'missing'}",
        "",
        "### 12.5 同业可比口径",
        "",
        "- **字段位置**：章节 7 `valuation_cases[*].inputs_snapshot.comparable_companies`",
        "- **通过标准**：可比公司同行业、相似规模、相似业务结构；财务指标口径统一（PE TTM/Forward、PB LTM/MRQ）",
        f"- **已统计结果**：{comp_result}",
        f"- **数据可用性**：{'available' if comparable_count > 0 else 'missing'}",
        "",
    ]
    return lines


def _build_review_conclusion_table() -> list[str]:
    """构建业务人员填写的复核结论表格。"""
    return [
        "## 13. 复核结论（业务人员填写）",
        "",
        "| 复核项 | 通过 | 不通过 | 待补充 | 不适用 | 证据/备注 |",
        "| --- | :---: | :---: | :---: | :---: | --- |",
        "| 1. 三表勾稽 | [ ] | [ ] | [ ] | [ ] |  |",
        "| 2. 分部收入加总 | [ ] | [ ] | [ ] | [ ] |  |",
        "| 3. 前瞻假设对齐 | [ ] | [ ] | [ ] | [ ] |  |",
        "| 4. 估值关键假设来源 | [ ] | [ ] | [ ] | [ ] |  |",
        "| 5. 同业可比口径 | [ ] | [ ] | [ ] | [ ] |  |",
        "",
        "**复核人签名**：____________  **复核日期**：____________",
        "",
        "**总体结论**：[ ] 全部通过  [ ] 部分不通过（需整改）  [ ] 待补充材料",
        "",
    ]


# ---------------------------------------------------------------------------
# 主渲染函数
# ---------------------------------------------------------------------------

def render_cfa_report_to_markdown(report: CfaReport) -> str:
    """把 10 章节 CFA 报告渲染为业务人员易读的 Markdown。

    Args:
        report: CfaReport 对象（CfaReport dataclass 或 dict）

    Returns:
        Markdown 字符串，包含：
        - 顶部公司元数据 + notice
        - 10 章节按顺序渲染（标题 + 状态图标 + 内容 + 限制 + 来源）
        - 字段位置速查表
        - 人工复核 Checklist 提示（5 项）
        - 复核结论表格（业务人员填写）
    """
    if report is None:
        return "# 暂无数据\n\nCfaReport 为空，无法渲染。\n"
    # 兼容 dict 输入
    if isinstance(report, dict):
        from .contracts import CfaReport as _CfaReport
        report = _CfaReport.from_dict(report)
    if not isinstance(report, CfaReport):
        return "# 暂无数据\n\nCfaReport 类型无效，无法渲染。\n"

    overall_label = _OVERALL_STATUS_LABELS.get(
        report.overall_status, report.overall_status,
    )

    lines: list[str] = [
        f"# {report.company_name or report.company_symbol} ({report.company_symbol}) - CFA 风格研究报告",
        "",
        "> **notice**：研究数据产物，不包含交易动作建议。",
        "",
        "## 0. 公司元数据",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        f"| symbol | {report.company_symbol} |",
        f"| name | {report.company_name or '暂无数据'} |",
        f"| as_of | {report.as_of} |",
        f"| lookback_days | {report.lookback_days} |",
        f"| overall_status | {report.overall_status} ({overall_label}) |",
        f"| model_version | {report.model_version} |",
        f"| report_id | {report.report_id} |",
        f"| created_at | {report.created_at} |",
        "",
    ]

    if report.degradation_reasons:
        lines.append("**降级原因：**")
        for reason in report.degradation_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    # 10 章节按顺序渲染
    for section_value in ReportSection.ordered_values():
        section = report.get_section(section_value)
        if section is None:
            # 缺章节，构造 missing 占位
            from .contracts import empty_section
            section = empty_section(section_value, reason="section_not_built")
        lines.append(render_cfa_report_section_to_markdown(section, level=2))
        lines.append("---", )
        lines.append("")

    # 11. 字段位置速查表
    lines.extend(_build_field_lookup_table(report))
    lines.append("---")
    lines.append("")

    # 12. 人工复核 Checklist 提示
    lines.extend(_build_review_checklist_hints(report))
    lines.append("---")
    lines.append("")

    # 13. 复核结论表格
    lines.extend(_build_review_conclusion_table())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Evidence Snapshot 渲染（附录：让业务人员直接在 .md 中看完整字段证据）
# ---------------------------------------------------------------------------

def render_evidence_snapshot_to_markdown(snapshot: dict[str, Any]) -> str:
    """把 evidence_snapshot dict 渲染为 Markdown 附录。

    与 ``render_cfa_report_to_markdown`` 配合使用：前者渲染 CfaReport 10 章节，
    本函数渲染 evidence_snapshot 中的关键字段（meta / key_metrics / balance_check /
    valuation_cases / consensus_estimates / data_gaps / review_checklist_hints），
    让业务人员打开 .md 即可看到完整字段证据，无需读 JSON。

    Args:
        snapshot: ``build_company_evidence_snapshot`` 返回的 dict

    Returns:
        Markdown 字符串
    """
    if not isinstance(snapshot, Mapping):
        return ""

    lines: list[str] = [
        "---",
        "",
        "# 附录 A. 证据快照（业务人员复核字段证据）",
        "",
        "> 本附录由 ``build_company_evidence_snapshot`` 自动生成，包含 5 项复核所需的完整字段与证据。",
        "> 数值保持原始精度；金额显示自动转换为 万/亿 单位。",
        "",
    ]

    meta = snapshot.get("meta")
    meta = meta if isinstance(meta, Mapping) else {}
    lines.extend([
        "## A.1 元数据",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        f"| symbol | {meta.get('symbol', '暂无数据')} |",
        f"| name | {meta.get('name', '暂无数据')} |",
        f"| industry | {meta.get('industry', '暂无数据')} |",
        f"| as_of | {meta.get('as_of', '暂无数据')} |",
        f"| generated_at | {meta.get('generated_at', '暂无数据')} |",
        f"| data_source | {meta.get('data_source', '暂无数据')} |",
        "",
        f"> notice: {meta.get('notice', '研究数据产物，不包含交易动作建议。')}",
        "",
    ])

    # A.2 关键指标
    key_metrics = snapshot.get("key_metrics")
    key_metrics = key_metrics if isinstance(key_metrics, Mapping) else {}
    lines.extend([
        "## A.2 关键指标 (key_metrics)",
        "",
        "| 指标 | 值 | 单位 | 币种 | 期间 | source_trace_id | data_gap |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    for metric_key in ("revenue", "net_profit", "total_assets",
                       "total_liabilities", "equity", "operating_cash_flow"):
        m = key_metrics.get(metric_key)
        m = m if isinstance(m, Mapping) else {}
        value = m.get("value")
        value_str = _format_amount(value, m.get("unit", "元")) if value is not None else "暂无数据"
        lines.append(
            f"| {metric_key} | {value_str} | {m.get('unit', '')} | "
            f"{m.get('currency', '')} | {m.get('period', '')} | "
            f"{m.get('source_trace_id', '')} | {m.get('data_gap', '')} |"
        )
    lines.append("")

    # A.3 三表勾稽
    balance_check = snapshot.get("balance_check")
    balance_check = balance_check if isinstance(balance_check, Mapping) else {}
    ale_balanced = balance_check.get("asset_liability_equity_balanced")
    ale_diff = balance_check.get("asset_liability_equity_diff")
    cash_explained = balance_check.get("cash_reconciliation_explained")
    cash_diff = balance_check.get("cash_reconciliation_diff")
    if ale_balanced is True:
        ale_str = f"✓ 通过（diff={ale_diff}）"
    elif ale_balanced is False:
        ale_str = f"✗ 不通过（diff={ale_diff}）"
    else:
        ale_str = "— 无法计算（缺字段）"
    if cash_explained is True:
        cash_str = f"✓ 通过（diff={cash_diff}）"
    elif cash_explained is False:
        cash_str = f"✗ 不通过（diff={cash_diff}）"
    else:
        cash_str = "— 无法计算（缺字段）"
    lines.extend([
        "## A.3 三表勾稽 (balance_check)",
        "",
        "| 检查项 | 结果 |",
        "| --- | --- |",
        f"| 资产 = 负债 + 权益 | {ale_str} |",
        f"| 现金流勾稽 | {cash_str} |",
        "",
    ])
    if balance_check.get("notes"):
        lines.append("**备注：**")
        for note in balance_check.get("notes", []):
            lines.append(f"- {note}")
        lines.append("")

    # A.4 估值案例
    valuation_cases = snapshot.get("valuation_cases")
    valuation_cases = (
        valuation_cases if isinstance(valuation_cases, (list, tuple)) else []
    )
    lines.extend([
        f"## A.4 估值案例 (valuation_cases, 共 {len(valuation_cases)} 个)",
        "",
    ])
    if not valuation_cases:
        lines.append("暂无估值案例。")
        lines.append("")
    else:
        case_number = 0
        for raw_case in valuation_cases:
            if not isinstance(raw_case, Mapping):
                continue
            case_number += 1
            case = raw_case
            assumptions = case.get("assumptions")
            assumptions = assumptions if isinstance(assumptions, (list, tuple)) else []
            source_refs = case.get("source_refs")
            source_refs = source_refs if isinstance(source_refs, (list, tuple)) else []
            comparable_companies = case.get("comparable_companies")
            comparable_companies = (
                comparable_companies
                if isinstance(comparable_companies, (list, tuple))
                else []
            )
            lines.extend([
                f"### A.4.{case_number} 案例 {case.get('case_id', '')}",
                "",
                "| 字段 | 值 |",
                "| --- | --- |",
                f"| method | {case.get('method', '')} |",
                f"| as_of | {case.get('as_of', '')} |",
                f"| scenario | {case.get('scenario', '')} |",
                f"| output_interval | {_format_interval(case.get('output_interval'))} |",
                f"| output_per_share | {_format_interval(case.get('output_per_share'))} |",
                f"| computability | {case.get('computability', '')} |",
                f"| model_version | {case.get('model_version', '')} |",
                f"| assumptions_count | {len(assumptions)} |",
                f"| source_refs_count | {len(source_refs)} |",
                f"| comparable_companies_count | {len(comparable_companies)} |",
                "",
            ])
            if assumptions:
                lines.append("**假设列表 (assumptions)：**")
                lines.append("")
                lines.append("| name | value | unit | source_ref | as_of |")
                lines.append("| --- | --- | --- | --- | --- |")
                for asm in assumptions:
                    if not isinstance(asm, Mapping):
                        continue
                    name = asm.get("name", "")
                    value = asm.get("value")
                    unit = asm.get("unit", "")
                    hint = _infer_hint(str(name))
                    value_str = _format_value(value, hint)
                    src = asm.get("source_ref", "") or "（无来源）"
                    asm_as_of = asm.get("as_of", "")
                    lines.append(f"| {name} | {value_str} | {unit} | {src} | {asm_as_of} |")
                lines.append("")
            if source_refs:
                lines.append("**来源引用 (source_refs)：**")
                for ref in source_refs:
                    lines.append(f"- {ref}")
                lines.append("")
            if comparable_companies:
                lines.append("**可比公司 (comparable_companies)：**")
                lines.append("")
                lines.extend(_render_dict_list_as_table(comparable_companies, indent=0))
                lines.append("")
            if case.get("limitations"):
                lines.append("**限制说明：**")
                for lim in case.get("limitations", []):
                    lines.append(f"- {lim}")
                lines.append("")
            if case.get("formula_trace"):
                lines.append("**公式轨迹 (formula_trace)：**")
                for trace in case.get("formula_trace", []):
                    lines.append(f"- {trace}")
                lines.append("")

    # A.5 一致预期
    consensus_estimates = snapshot.get("consensus_estimates")
    consensus_estimates = (
        consensus_estimates
        if isinstance(consensus_estimates, (list, tuple))
        else []
    )
    lines.extend([
        f"## A.5 一致预期 (consensus_estimates, 共 {len(consensus_estimates)} 条)",
        "",
    ])
    if not consensus_estimates:
        lines.append("暂无一致预期（贵州茅台 600519.SH 应有 226 条；其他公司可能因 tushare report_rc 接口未返回数据而缺失）。")
        lines.append("")
    else:
        lines.append("| consensus_id | provider | institution_id | analyst_id | values_count |")
        lines.append("| --- | --- | --- | --- | --- |")
        for est in consensus_estimates[:50]:  # 最多显示 50 条
            if not isinstance(est, Mapping):
                continue
            values = est.get("values")
            values = values if isinstance(values, (list, tuple)) else []
            lines.append(
                f"| {est.get('consensus_id', '')} | {est.get('provider', '')} | "
                f"{est.get('institution_id', '')} | {est.get('analyst_id', '')} | "
                f"{len(values)} |"
            )
        if len(consensus_estimates) > 50:
            lines.append("")
            lines.append(f"> 仅显示前 50 条，完整列表见 `<symbol>.json` 文件。")
        lines.append("")

    # A.6 数据缺口
    data_gaps = snapshot.get("data_gaps")
    data_gaps = data_gaps if isinstance(data_gaps, (list, tuple)) else []
    lines.extend([
        f"## A.6 数据缺口 (data_gaps, 共 {len(data_gaps)} 项)",
        "",
    ])
    if not data_gaps:
        lines.append("无数据缺口。")
        lines.append("")
    else:
        lines.append("| field | reason |")
        lines.append("| --- | --- |")
        for gap in data_gaps:
            if not isinstance(gap, Mapping):
                continue
            lines.append(
                f"| {gap.get('field', '')} | {gap.get('reason', '')} |"
            )
        lines.append("")

    # A.7 复核提示
    review_hints = snapshot.get("review_checklist_hints")
    review_hints = review_hints if isinstance(review_hints, (list, tuple)) else []
    lines.extend([
        "## A.7 人工复核 Checklist 提示 (review_checklist_hints)",
        "",
        "| 复核项 | 字段位置 | 通过标准 | 已计算结果 | 数据可用性 |",
        "| --- | --- | --- | --- | --- |",
    ])
    for hint in review_hints:
        if not isinstance(hint, dict):
            continue
        lines.append(
            f"| {hint.get('item', '')} | {hint.get('field_location', '')} | "
            f"{hint.get('pass_criteria', '')} | {hint.get('computed_result', '')} | "
            f"{hint.get('data_availability', '')} |"
        )
    lines.append("")

    return "\n".join(lines)
