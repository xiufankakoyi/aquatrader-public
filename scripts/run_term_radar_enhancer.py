"""P2 新词雷达增强运行脚本。

用法：
    python scripts/run_term_radar_enhancer.py
    python scripts/run_term_radar_enhancer.py --input-dir data/reports/perilla_discovery
    python scripts/run_term_radar_enhancer.py --output-dir /tmp/test_p2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.perilla.discovery.term_radar_enhancer import (
    run_term_radar_enhancer,
)


def main() -> int:
    """运行 P2 新词雷达增强。"""
    parser = argparse.ArgumentParser(description="P2 新词雷达与概念发现增强")
    parser.add_argument(
        "--input-dir",
        type=str,
        default="data/reports/perilla_discovery",
        help="输入目录（默认 data/reports/perilla_discovery）",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="输出目录（默认同 input-dir）",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else None

    print("=" * 60)
    print("P2 新词雷达与概念发现增强")
    print("=" * 60)
    print(f"输入目录: {input_dir}")
    print(f"输出目录: {output_dir or input_dir}")
    print()

    summary = run_term_radar_enhancer(input_dir=input_dir, output_dir=output_dir)

    if not summary.get("success"):
        print(f"[失败] {summary.get('module')}: {summary.get('error')}")
        return 1

    counts = summary.get("counts", {})
    print("[成功] 生成文件:")
    for path in summary.get("outputs", []):
        print(f"  - {path}")
    print()
    print("统计:")
    print(f"  raw_terms: {counts.get('raw_terms', 0)}")
    print(f"  candidate_concepts: {counts.get('candidate_concepts', 0)}")
    print(f"  statistical_terms: {counts.get('statistical_terms', 0)}")
    print(f"  concept_scores: {counts.get('concept_scores', 0)}")
    print(f"  concept_aliases: {counts.get('concept_aliases', 0)}")
    print()

    # 打印 top 20 statistical terms
    output_base = output_dir or input_dir
    scores_path = output_base / "concept_scores_latest.json"
    if scores_path.exists():
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
        print("Top 20 concept_scores:")
        for item in scores[:20]:
            print(
                f"  - {item.get('concept_name')}｜{item.get('term_type')}｜"
                f"{item.get('root_theme')}｜final={item.get('final_score')}｜"
                f"burst={item.get('burst_score')}｜cooc={item.get('cooccurrence_score')}"
            )
        print()

    aliases_path = output_base / "concept_aliases_latest.json"
    if aliases_path.exists():
        aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
        print("别名分组:")
        for group in aliases:
            print(
                f"  - {group.get('alias_group_id')}｜{group.get('canonical_name')}｜"
                f"aliases={group.get('aliases')}"
            )
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
