"""P0 本地概念闭环一键运行脚本。

用法:
    python scripts/run_local_concept_loop.py
    python scripts/run_local_concept_loop.py --output-dir /tmp/test_perilla
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.perilla.discovery.knowledge_loop_engine import run_local_concept_loop


def main() -> int:
    """脚本入口。

    Returns:
        退出码，0 表示成功，1 表示失败
    """
    parser = argparse.ArgumentParser(description="运行 P0 本地概念闭环")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="输出目录，默认为 data/reports/perilla_discovery/",
    )
    parser.add_argument(
        "--execute-search",
        action="store_true",
        default=False,
        help="是否执行搜索任务（默认不执行）",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else None
    summary = run_local_concept_loop(
        output_dir=output_dir,
        project_root=_PROJECT_ROOT,
        execute_search=args.execute_search,
        use_llm_explanations=False,
    )

    print("=" * 60)
    print("P0 本地概念闭环运行结果")
    print("=" * 60)
    print(f"模块: {summary.get('module', '')}")
    print(f"成功: {summary.get('success', False)}")

    if not summary.get("success"):
        print(f"错误: {summary.get('error', '未知错误')}")
        return 1

    counts = summary.get("counts", {})
    print("\n生成条数:")
    for key, value in counts.items():
        print(f"  {key}: {value}")

    outputs = summary.get("outputs", [])
    if outputs:
        print(f"\n输出文件 ({len(outputs)} 个):")
        for path in outputs:
            print(f"  {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
