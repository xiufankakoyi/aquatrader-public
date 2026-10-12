"""Virtual Research Desk Pipeline — 先运行 QuantFlow，再运行虚拟投研台。"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def run_pipeline(write_files: bool = True) -> dict:
    """执行 Virtual Research Desk Pipeline。

    流程：
    1. 先运行 QuantFlow Pipeline 确保数据最新
    2. 运行 VirtualResearchDesk 生成投研台报告

    Args:
        write_files: 是否写入报告文件

    Returns:
        虚拟投研台完整报告字典
    """
    # 1. 先运行 QuantFlow Pipeline
    print("[VirtualResearchDesk] Step 1: 运行 QuantFlow Pipeline...")
    from core.research.pipeline.quant_flow_pipeline import QuantFlowPipeline

    try:
        qf = QuantFlowPipeline()
        qf_report = qf.run(write_files=write_files)
    except Exception as exc:
        return {
            "success": False,
            "final_output_level": "DATA_MISSING",
            "candidate_stock_count": 0,
            "error": f"quant_flow_failed: {exc}",
        }
    qf_report = qf_report if isinstance(qf_report, Mapping) else {}
    trade_date = str(qf_report.get("global_latest_trade_date", "") or "")
    final_brief = qf_report.get("final_brief", {})
    final_brief = final_brief if isinstance(final_brief, Mapping) else {}
    final_data = final_brief.get("data", {})
    final_data = final_data if isinstance(final_data, Mapping) else {}
    qf_level = final_data.get("research_output_level", "UNKNOWN")
    print(
        f"[VirtualResearchDesk] QuantFlow 完成: "
        f"trade_date={trade_date}, level={qf_level}"
    )

    # Step 1.5: Evidence Center 扩容采集
    print("[VirtualResearchDesk] Step 1.5: Evidence Center 扩容采集...")
    from core.research.evidence import EvidenceCenter

    ec = EvidenceCenter()
    # 从 QuantFlow 报告中提取各阶段数据
    stages = qf_report.get("stages", [])
    stages = stages if isinstance(stages, (list, tuple)) else []
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        stage_name = stage.get("stage", "")
        stage_data = stage.get("data", {})
        if not isinstance(stage_data, Mapping):
            stage_data = {}
        try:
            if stage_name == "stock_screener_candidates":
                ec.collect_from_stock_screener(stage_data, trade_date)
            elif stage_name == "strategy_signal_scan":
                ec.collect_from_strategy_signals(stage_data, trade_date)
            elif stage_name == "market_regime_detect":
                ec.collect_from_market_regime(stage_data, trade_date)
            elif stage_name == "portfolio_risk_check":
                ec.collect_from_portfolio_risk(stage_data, trade_date)
        except Exception:
            continue

    # 尝试从本地数据源采集（数据不可用时静默跳过）
    for collector in (
        ec.collect_from_industry_chain_if_available,
        ec.collect_from_finance_radar_if_available,
    ):
        try:
            collector(trade_date)
        except Exception:
            continue

    # 标记 stale 证据并导出
    ec.mark_stale(trade_date)
    ec_export = ec.export()
    ec_export = ec_export if isinstance(ec_export, Mapping) else {}
    print(
        f"[VirtualResearchDesk] Evidence Center 扩容完成: "
        f"total={ec_export.get('total', 0)}, stale={ec_export.get('stale', 0)}, "
        f"by_source={ec_export.get('by_source', {})}"
    )

    # 2. 运行 VirtualResearchDesk
    print("[VirtualResearchDesk] Step 2: 运行虚拟投研台...")
    from core.research.desk.virtual_research_desk import VirtualResearchDesk

    try:
        desk = VirtualResearchDesk()
        report = desk.run(trade_date=trade_date)
    except Exception as exc:
        return {
            "success": False,
            "final_output_level": "DATA_MISSING",
            "candidate_stock_count": 0,
            "error": f"virtual_research_desk_failed: {exc}",
        }
    report = (
        dict(report)
        if isinstance(report, Mapping)
        else {
            "success": False,
            "final_output_level": "DATA_MISSING",
            "candidate_stock_count": 0,
            "error": "invalid virtual research desk report",
        }
    )

    if write_files:
        desk.write_reports(report)
        print(
            f"[VirtualResearchDesk] 报告已写入: "
            f"data/reports/virtual_research_desk_latest.json / .md"
        )

    # 3. 输出摘要
    final_level = report.get("final_output_level", "UNKNOWN")
    candidate_count = report.get("candidate_stock_count", 0)
    print(
        f"[VirtualResearchDesk] 完成: "
        f"final_output_level={final_level}, "
        f"candidate_stocks={candidate_count}"
    )

    return report


if __name__ == "__main__":
    result = run_pipeline(write_files=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
