"""P4A 多智能体假设生成与反方审查运行脚本。

用法：
    python scripts/run_hypothesis_engine.py
    python scripts/run_hypothesis_engine.py --output-dir /tmp/test_p4a
    python scripts/run_hypothesis_engine.py --no-llm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.perilla.discovery.adversarial_research_agents import (
    run_adversarial_research_agents,
)
from core.research.perilla.discovery.hypothesis_engine import (
    run_hypothesis_engine,
)


def main() -> int:
    """运行 P4A 多智能体假设生成与反方审查。"""
    parser = argparse.ArgumentParser(description="P4A 多智能体假设生成与反方审查")
    parser.add_argument(
        "--input-dir", type=str,
        default="data/reports/perilla_discovery",
        help="输入目录",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="输出目录（默认同 input-dir）",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="禁用 LLM，使用纯规则模式",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else None
    use_llm = not args.no_llm

    print("=" * 60)
    print("P4A 多智能体假设生成与反方审查")
    print("=" * 60)
    print(f"LLM 模式: {'启用' if use_llm else '禁用（纯规则）'}")
    print()

    # 1. 假设生成
    hyp_summary = run_hypothesis_engine(
        input_dir=input_dir, output_dir=output_dir, use_llm=use_llm
    )
    if not hyp_summary.get("success"):
        print(f"[失败] hypothesis_engine: {hyp_summary.get('error')}")
        return 1

    counts = hyp_summary.get("counts", {})
    print(f"[成功] hypothesis_engine: hypotheses={counts.get('hypotheses', 0)}, "
          f"lifecycle={counts.get('lifecycle', 0)}")

    # 2. 对抗式审查
    adv_summary = run_adversarial_research_agents(
        input_dir=input_dir, output_dir=output_dir, use_llm=use_llm
    )
    if not adv_summary.get("success"):
        print(f"[失败] adversarial_research_agents: {adv_summary.get('error')}")
        return 1

    adv_counts = adv_summary.get("counts", {})
    print(f"[成功] adversarial_research_agents: "
          f"reviews={adv_counts.get('adversarial_reviews', 0)}, "
          f"skepticism_scores={adv_counts.get('skepticism_scores', 0)}, "
          f"portfolio_translation={adv_counts.get('portfolio_translation', 0)}")
    print()

    # 打印 top 10 hypotheses
    out_base = output_dir or input_dir
    hyp_path = out_base / "research_hypotheses_latest.json"
    if hyp_path.exists():
        hyps = json.loads(hyp_path.read_text(encoding="utf-8"))
        print("Top 10 hypotheses:")
        for item in hyps[:10]:
            print(f"  - {item.get('hypothesis_text')}｜"
                  f"novelty={item.get('novelty_score')}｜"
                  f"evidence={item.get('evidence_score')}｜"
                  f"status={item.get('status')}")
        print()

    # 打印 top 10 high skepticism
    scores_path = out_base / "skepticism_scores_latest.json"
    if scores_path.exists():
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
        sorted_scores = sorted(
            scores, key=lambda x: float(x.get("skepticism_score", 0)), reverse=True
        )
        print("Top 10 high skepticism:")
        for item in sorted_scores[:10]:
            print(f"  - {item.get('hypothesis_id')}｜"
                  f"skepticism={item.get('skepticism_score')}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
