"""P3 产业链图谱与搜索任务运行脚本。

用法：
    python scripts/run_industry_chain_graph.py
    python scripts/run_industry_chain_graph.py --output-dir /tmp/test_p3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.perilla.discovery.industry_chain_graph_builder import (
    run_industry_chain_graph,
)
from core.research.perilla.discovery.search_task_compiler import (
    run_search_task_compiler,
)


def main() -> int:
    """运行 P3 产业链图谱与搜索任务。"""
    parser = argparse.ArgumentParser(description="P3 产业链图谱与搜索任务")
    parser.add_argument(
        "--input-dir", type=str,
        default="data/reports/perilla_discovery",
        help="输入目录",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="输出目录（默认同 input-dir）",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else None

    print("=" * 60)
    print("P3 产业链图谱与搜索任务")
    print("=" * 60)

    # 1. 构建产业链图谱
    graph_summary = run_industry_chain_graph(input_dir=input_dir, output_dir=output_dir)
    if not graph_summary.get("success"):
        print(f"[失败] industry_chain_graph: {graph_summary.get('error')}")
        return 1

    counts = graph_summary.get("counts", {})
    print(f"[成功] industry_chain_graph: nodes={counts.get('nodes', 0)}, "
          f"edges={counts.get('edges', 0)}, missing_edges={counts.get('missing_edges', 0)}")

    # 2. 编译搜索任务
    task_summary = run_search_task_compiler(input_dir=input_dir, output_dir=output_dir)
    if not task_summary.get("success"):
        print(f"[失败] search_task_compiler: {task_summary.get('error')}")
        return 1

    task_counts = task_summary.get("counts", {})
    print(f"[成功] search_task_compiler: "
          f"missing_edges={task_counts.get('missing_edges', 0)}, "
          f"search_tasks={task_counts.get('search_tasks', 0)}")
    print()

    # 打印 top 20 missing_edges
    out_base = output_dir or input_dir
    missing_path = out_base / "missing_edges_latest.json"
    if missing_path.exists():
        missing_edges = json.loads(missing_path.read_text(encoding="utf-8"))
        print("Top 20 missing_edges:")
        for item in missing_edges[:20]:
            print(f"  - {item.get('source_node')} → {item.get('target_hint')}｜"
                  f"{item.get('source_type')}｜priority={item.get('priority_score')}")
        print()

    # 打印 top 20 search_tasks
    tasks_path = out_base / "search_tasks_from_missing_edges_latest.json"
    if tasks_path.exists():
        tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
        print("Top 20 search_tasks:")
        for item in tasks[:20]:
            print(f"  - {item.get('query')}｜{item.get('query_type')}｜"
                  f"priority={item.get('priority_score')}｜"
                  f"source={item.get('task_source')}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
