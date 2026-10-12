"""P8 AquaResearch 总调度脚本。

直接 import 各模块 run_xxx() 函数（禁止 subprocess），顺序调用：
    run_local_concept_loop → run_report_causal_extractor → run_term_radar_enhancer
    → run_industry_chain_graph → run_search_task_compiler
    → run_hypothesis_engine → run_adversarial_research_agents
    → run_evidence_evolution → run_market_resonance

任一步 success=False 立即停止并打印失败模块。
最后聚合 search_tasks_from_missing_edges_latest.json 为 search_tasks_latest.json。

用法：
    python scripts/run_aquaresearch_pipeline.py
    python scripts/run_aquaresearch_pipeline.py --output-dir /tmp/test_pipeline
    python scripts/run_aquaresearch_pipeline.py --no-llm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 直接 import 各模块 run_xxx() 函数（禁止 subprocess）
from core.research.perilla.discovery.knowledge_loop_engine import (
    run_local_concept_loop,
)
from core.research.perilla.discovery.report_causal_extractor import (
    run_report_causal_extractor,
)
from core.research.perilla.discovery.term_radar_enhancer import (
    run_term_radar_enhancer,
)
from core.research.perilla.discovery.industry_chain_graph_builder import (
    run_industry_chain_graph,
)
from core.research.perilla.discovery.search_task_compiler import (
    run_search_task_compiler,
)
from core.research.perilla.discovery.hypothesis_engine import (
    run_hypothesis_engine,
)
from core.research.perilla.discovery.adversarial_research_agents import (
    run_adversarial_research_agents,
)
from core.research.perilla.evidence.evidence_evolution_tracker import (
    run_evidence_evolution,
)
from core.research.perilla.discovery.market_resonance_engine import (
    run_market_resonance,
)


def _print_step_result(step_name: str, summary: Dict[str, Any]) -> None:
    """打印单步执行结果。

    Args:
        step_name: 步骤名称
        summary: 模块返回的 dict summary
    """
    if not summary.get("success"):
        print(f"[失败] {step_name}: {summary.get('error', '未知错误')}")
        return

    counts = summary.get("counts", {})
    counts_str = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"[成功] {step_name}: {counts_str}")


def _aggregate_search_tasks(
    input_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    """聚合 search_tasks_from_missing_edges_latest.json 为 search_tasks_latest.json。

    第一阶段直接拷贝；为未来多来源任务（hypothesis / skepticism / evidence_evolution /
    user manual / SearXNG 重试）预留合并点。

    Args:
        input_dir: 输入目录
        output_dir: 输出目录

    Returns:
        dict summary，含 success / aggregated_count
    """
    source_path = input_dir / "search_tasks_from_missing_edges_latest.json"
    target_path = output_dir / "search_tasks_latest.json"

    try:
        if not source_path.exists():
            # 源文件不存在，写入空列表保持兼容
            target_path.write_text("[]", encoding="utf-8")
            return {
                "module": "search_tasks_aggregator",
                "success": True,
                "outputs": [str(target_path)],
                "counts": {"search_tasks": 0},
            }

        tasks: List[Dict[str, Any]] = json.loads(
            source_path.read_text(encoding="utf-8")
        )
        target_path.write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "module": "search_tasks_aggregator",
            "success": True,
            "outputs": [str(target_path)],
            "counts": {"search_tasks": len(tasks)},
        }
    except Exception as exc:
        return {
            "module": "search_tasks_aggregator",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }


def _print_total_summary(output_dir: Path) -> None:
    """打印总摘要。

    Args:
        output_dir: 输出目录
    """
    print()
    print("=" * 60)
    print("AquaResearch Pipeline 总摘要")
    print("=" * 60)

    # 核心文件条数统计
    files_to_count: List[tuple[str, str]] = [
        ("raw_documents_latest.json", "raw_documents"),
        ("raw_terms_latest.json", "raw_terms"),
        ("candidate_concepts_latest.json", "candidate_concepts"),
        ("report_claims_latest.json", "report_claims"),
        ("causal_chains_latest.json", "causal_chains"),
        ("evidence_items_latest.json", "evidence_items"),
        ("failure_conditions_latest.json", "failure_conditions"),
        ("evidence_edges_latest.json", "evidence_edges"),
        ("concept_scores_latest.json", "concept_scores"),
        ("concept_aliases_latest.json", "concept_aliases"),
        ("industry_chain_graph_latest.json", "industry_chain_graph"),
        ("missing_edges_latest.json", "missing_edges"),
        ("search_tasks_from_missing_edges_latest.json", "search_tasks_from_missing_edges"),
        ("search_tasks_latest.json", "search_tasks"),
        ("research_hypotheses_latest.json", "research_hypotheses"),
        ("hypothesis_lifecycle_latest.json", "hypothesis_lifecycle"),
        ("adversarial_reviews_latest.json", "adversarial_reviews"),
        ("skepticism_scores_latest.json", "skepticism_scores"),
        ("portfolio_translation_latest.json", "portfolio_translation"),
        ("evidence_timeline_latest.json", "evidence_timeline"),
        ("belief_state_latest.json", "belief_state"),
        ("post_mortem_latest.json", "post_mortem"),
        ("market_resonance_latest.json", "market_resonance"),
        ("trigger_signals_latest.json", "trigger_signals"),
    ]

    for filename, label in files_to_count:
        path = output_dir / filename
        if not path.exists():
            print(f"  {label}: 0 (文件不存在)")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                count = len(data)
            elif isinstance(data, dict):
                # industry_chain_graph 是 dict，含 nodes / edges
                if "nodes" in data and "edges" in data:
                    count = len(data.get("nodes", [])) + len(data.get("edges", []))
                else:
                    count = len(data)
            else:
                count = 0
            print(f"  {label}: {count}")
        except Exception as exc:
            print(f"  {label}: 读取失败 ({exc})")

    print("=" * 60)


def run_pipeline(
    output_dir: Path | None = None,
    project_root: Path | None = None,
    use_llm: bool = True,
    enable_search: bool = False,
) -> Dict[str, Any]:
    """运行 AquaResearch 全流水线。

    直接 import 调用各模块 run_xxx()，任一步失败立即停止。

    Args:
        output_dir: 输出目录，默认 data/reports/perilla_discovery/
        project_root: 项目根目录，默认当前工作目录
        use_llm: 是否启用 LLM（hybrid 模式），False 则纯规则
        enable_search: 是否启用外部搜索（ResearchEngine），False 则纯本地

    Returns:
        dict summary，含 success / steps / failed_step / error
    """
    root = Path(project_root) if project_root else _PROJECT_ROOT
    out = Path(output_dir) if output_dir else root / "data" / "reports" / "perilla_discovery"
    out.mkdir(parents=True, exist_ok=True)

    steps: List[Dict[str, Any]] = []

    # Step 1: P0 本地概念闭环
    print("-" * 60)
    print("Step 1/9: P0 本地概念闭环" + ("（启用外部搜索）" if enable_search else ""))
    s1 = run_local_concept_loop(
        output_dir=out,
        project_root=root,
        execute_search=enable_search,
        use_llm_explanations=False,
    )
    steps.append({"step": "P0_local_concept_loop", "summary": s1})
    _print_step_result("P0_local_concept_loop", s1)
    if not s1.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P0_local_concept_loop",
                "error": s1.get("error", "")}

    # Step 2: P1 研报因果链抽取
    print("-" * 60)
    print("Step 2/9: P1 研报因果链抽取")
    s2 = run_report_causal_extractor(input_dir=out, output_dir=out, use_llm=use_llm)
    steps.append({"step": "P1_report_causal_extractor", "summary": s2})
    _print_step_result("P1_report_causal_extractor", s2)
    if not s2.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P1_report_causal_extractor",
                "error": s2.get("error", "")}

    # Step 3: P2 新词雷达增强
    print("-" * 60)
    print("Step 3/9: P2 新词雷达增强")
    s3 = run_term_radar_enhancer(input_dir=out, output_dir=out)
    steps.append({"step": "P2_term_radar_enhancer", "summary": s3})
    _print_step_result("P2_term_radar_enhancer", s3)
    if not s3.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P2_term_radar_enhancer",
                "error": s3.get("error", "")}

    # Step 4: P3 产业链图谱构建
    print("-" * 60)
    print("Step 4/9: P3 产业链图谱构建")
    s4 = run_industry_chain_graph(input_dir=out, output_dir=out)
    steps.append({"step": "P3_industry_chain_graph", "summary": s4})
    _print_step_result("P3_industry_chain_graph", s4)
    if not s4.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P3_industry_chain_graph",
                "error": s4.get("error", "")}

    # Step 5: P3 搜索任务编译
    print("-" * 60)
    print("Step 5/9: P3 搜索任务编译")
    s5 = run_search_task_compiler(input_dir=out, output_dir=out)
    steps.append({"step": "P3_search_task_compiler", "summary": s5})
    _print_step_result("P3_search_task_compiler", s5)
    if not s5.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P3_search_task_compiler",
                "error": s5.get("error", "")}

    # Step 6: P4A 假设生成
    print("-" * 60)
    print("Step 6/9: P4A 假设生成")
    s6 = run_hypothesis_engine(input_dir=out, output_dir=out, use_llm=use_llm)
    steps.append({"step": "P4A_hypothesis_engine", "summary": s6})
    _print_step_result("P4A_hypothesis_engine", s6)
    if not s6.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P4A_hypothesis_engine",
                "error": s6.get("error", "")}

    # Step 7: P4A 对抗式审查
    print("-" * 60)
    print("Step 7/9: P4A 对抗式审查")
    s7 = run_adversarial_research_agents(input_dir=out, output_dir=out, use_llm=use_llm)
    steps.append({"step": "P4A_adversarial_research_agents", "summary": s7})
    _print_step_result("P4A_adversarial_research_agents", s7)
    if not s7.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P4A_adversarial_research_agents",
                "error": s7.get("error", "")}

    # Step 8: P4B 证据演化
    print("-" * 60)
    print("Step 8/9: P4B 证据演化")
    s8 = run_evidence_evolution(input_dir=out, output_dir=out, use_llm=use_llm)
    steps.append({"step": "P4B_evidence_evolution", "summary": s8})
    _print_step_result("P4B_evidence_evolution", s8)
    if not s8.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P4B_evidence_evolution",
                "error": s8.get("error", "")}

    # Step 9: P4B 市场共振
    print("-" * 60)
    print("Step 9/9: P4B 市场共振")
    s9 = run_market_resonance(input_dir=out, output_dir=out)
    steps.append({"step": "P4B_market_resonance", "summary": s9})
    _print_step_result("P4B_market_resonance", s9)
    if not s9.get("success"):
        return {"success": False, "steps": steps, "failed_step": "P4B_market_resonance",
                "error": s9.get("error", "")}

    # 聚合 search_tasks
    print("-" * 60)
    print("聚合 search_tasks_latest.json")
    agg = _aggregate_search_tasks(input_dir=out, output_dir=out)
    _print_step_result("search_tasks_aggregator", agg)
    if not agg.get("success"):
        return {"success": False, "steps": steps, "failed_step": "search_tasks_aggregator",
                "error": agg.get("error", "")}

    # 总摘要
    _print_total_summary(out)

    return {
        "success": True,
        "steps": steps,
        "failed_step": None,
        "output_dir": str(out),
    }


def main() -> int:
    """主入口。

    Returns:
        退出码：0 成功，1 失败
    """
    parser = argparse.ArgumentParser(
        description="AquaResearch 总调度（直接 import，禁止 subprocess）"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="输出目录（默认 data/reports/perilla_discovery/）",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="禁用 LLM，使用纯规则模式",
    )
    parser.add_argument(
        "--enable-search",
        action="store_true",
        help="启用外部搜索（ResearchEngine），默认关闭",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else None
    use_llm = not args.no_llm
    enable_search = args.enable_search

    print("=" * 60)
    print("AquaResearch Pipeline 总调度")
    print("=" * 60)
    print(f"输出目录: {output_dir or 'data/reports/perilla_discovery/'}")
    print(f"LLM 模式: {'hybrid（LLM 优先 + 规则降级）' if use_llm else '纯规则'}")
    print(f"外部搜索: {'启用（ResearchEngine）' if enable_search else '关闭'}")
    print("=" * 60)

    result = run_pipeline(
        output_dir=output_dir,
        project_root=_PROJECT_ROOT,
        use_llm=use_llm,
        enable_search=enable_search,
    )

    if not result.get("success"):
        print()
        print(f"[PIPELINE 失败] 失败步骤: {result.get('failed_step')}")
        print(f"错误: {result.get('error')}")
        return 1

    print()
    print("[PIPELINE 成功] 全部 9 步执行完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
