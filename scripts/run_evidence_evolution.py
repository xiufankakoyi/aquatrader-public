"""P4B 证据演化与市场共振运行脚本。

用法：
    python scripts/run_evidence_evolution.py
    python scripts/run_evidence_evolution.py --output-dir /tmp/test_p4b
    python scripts/run_evidence_evolution.py --no-llm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.perilla.evidence.evidence_evolution_tracker import (
    run_evidence_evolution,
)
from core.research.perilla.discovery.market_resonance_engine import (
    run_market_resonance,
)


def main() -> int:
    """运行 P4B 证据演化与市场共振。"""
    parser = argparse.ArgumentParser(description="P4B 证据演化与市场共振")
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
    print("P4B 证据演化与市场共振")
    print("=" * 60)
    print(f"LLM 模式: {'启用' if use_llm else '禁用（纯规则）'}")
    print()

    # 1. 证据演化
    evo_summary = run_evidence_evolution(
        input_dir=input_dir, output_dir=output_dir, use_llm=use_llm
    )
    if not evo_summary.get("success"):
        print(f"[失败] evidence_evolution: {evo_summary.get('error')}")
        return 1

    counts = evo_summary.get("counts", {})
    print(f"[成功] evidence_evolution: "
          f"timeline={counts.get('evidence_timeline', 0)}, "
          f"belief_state={counts.get('belief_state', 0)}, "
          f"post_mortem={counts.get('post_mortem', 0)}")

    # 2. 市场共振
    res_summary = run_market_resonance(input_dir=input_dir, output_dir=output_dir)
    if not res_summary.get("success"):
        print(f"[失败] market_resonance: {res_summary.get('error')}")
        return 1

    res_counts = res_summary.get("counts", {})
    print(f"[成功] market_resonance: "
          f"resonance={res_counts.get('market_resonance', 0)}, "
          f"trigger_signals={res_counts.get('trigger_signals', 0)}")
    print()

    # 打印 top 10 posterior_probability
    out_base = output_dir or input_dir
    belief_path = out_base / "belief_state_latest.json"
    if belief_path.exists():
        beliefs = json.loads(belief_path.read_text(encoding="utf-8"))
        sorted_beliefs = sorted(
            beliefs, key=lambda x: float(x.get("posterior_probability", 0)), reverse=True
        )
        print("Top 10 posterior_probability:")
        for item in sorted_beliefs[:10]:
            print(f"  - {item.get('hypothesis_id')}｜"
                  f"posterior={item.get('posterior_probability')}｜"
                  f"state={item.get('state')}")
        print()

    # 打印 top 10 trigger_signals
    signals_path = out_base / "trigger_signals_latest.json"
    if signals_path.exists():
        signals = json.loads(signals_path.read_text(encoding="utf-8"))
        print("Top 10 trigger_signals:")
        for item in signals[:10]:
            print(f"  - {item.get('hypothesis_id')}｜"
                  f"type={item.get('trigger_type')}｜"
                  f"action={item.get('next_action')}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
