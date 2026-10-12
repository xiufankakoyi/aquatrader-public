from __future__ import annotations

from pathlib import Path
from typing import Any


PROBLEM_DEFINITION = (
    "AquaTrader Research 的知识本体到底是什么？它如何生长、验证、修正，并最终支撑投研决策？"
)

HIGHEST_PRINCIPLE = (
    "禁止继续在旧 QuantFlow / research pipeline 上打补丁。\n"
    "旧 QuantFlow 只保留为市场状态面板，不再承担“新标的发现”职责。\n"
    "AquaTrader Research 的核心从现在开始切换为 HyperGraph Research Engine。"
)

ROLE_MAP = (
    "QuantFlow = 市场快照\n"
    "SignalEngine = 技术确认\n"
    "RAG = 证据挖掘工具\n"
    "LLM = 图生长与验证工具\n"
    "Report = 图的一个输出视图\n"
    "HyperGraph = 真正的大脑"
)

CORE_ARTIFACT = "主题-工艺-材料/设备-公司-证据-反证-时间-约束 的受益路径"

FORBIDDEN_OUTPUT = "推荐关注：A、B、C"

REQUIRED_OUTPUT_EXAMPLE = (
    "路径 1：\n"
    "HBM → TSV铜填充 → 电镀液 → 公司A/子公司B → 年报证据 → 研报交叉验证 → 反证检查 → 路径评分\n"
    "\n"
    "路径 2：\n"
    "HBM → 临时键合 → 键合材料 → 公司C → 公告证据 → 客户不明 → 降级为 LIKELY\n"
    "\n"
    "路径 3：\n"
    "HBM → 封装基板 → ABF材料 → 公司D → 无官方证据 → SPECULATIVE，不进入重点候选"
)

FIRST_BATTLE_BOUNDARY = (
    "任务名称：HyperGraph Kernel 第一战役\n"
    "\n"
    "不要修改 QuantFlow。\n"
    "不要修改旧 research_routes。\n"
    "不要做报告生成。\n"
    "不要做候选股筛选。\n"
    "不要做前端页面。\n"
    "不要做简化 MVP。"
)

FORBIDDEN_DRIFT_PHRASES = (
    "已完成一个简单版本",
    "已跑通基础流程",
    "已生成候选股报告",
    "已接入 RAG 总结",
    "已在 QuantFlow 中新增阶段",
    "已完成 AlphaDiscoveryPipeline",
)

COMPLETION_STATEMENT = (
    "已实现 HyperGraph Kernel。\n"
    "现在系统可以存储和查询带证据约束的超图关系。\n"
    "没有证据的关系无法进入 ACTIVE。\n"
    "粗粒度公司-主题关系会被拒绝。\n"
    "每条关系都能被证据升级、反证降级、冲突标记。\n"
    "查询股票时必须返回受益路径而不是裸标的。"
)

FIRST_PRINCIPLE = (
    "不是从数据里筛股票，而是从真实产业结构中生长出受益路径；"
    "不是让 LLM 写报告，而是让 LLM 维护、质疑、修正一张可验证的超图；"
    "不是输出候选股，而是输出有证据、有反证、有时间、有瓶颈、有股权穿透的可复核受益链路。"
)

FORBIDDEN_IMPORT_PREFIXES = (
    "core.research.pipeline",
    "server.routes.research_routes",
)


def validate_research_graph_boundaries(root: Path) -> dict[str, Any]:
    """Verify that the HyperGraph package stays independent from old research surfaces."""

    graph_dir = root / "core" / "research_graph"
    violations: list[str] = []
    for path in graph_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for prefix in FORBIDDEN_IMPORT_PREFIXES:
            if f"import {prefix}" in text or f"from {prefix}" in text:
                violations.append(f"{path.relative_to(root)} imports {prefix}")
    return {
        "ok": not violations,
        "violations": violations,
        "highest_principle": HIGHEST_PRINCIPLE,
        "core_artifact": CORE_ARTIFACT,
    }
