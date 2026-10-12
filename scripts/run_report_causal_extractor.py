"""P1 研报因果链抽取一键运行脚本。

用法：
    python scripts/run_report_causal_extractor.py
    python scripts/run_report_causal_extractor.py --input-dir <path> --output-dir <path>
    python scripts/run_report_causal_extractor.py --no-llm  # 纯规则模式
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.perilla.discovery.report_causal_extractor import (
    run_report_causal_extractor,
)


def main() -> int:
    """主入口。

    Returns:
        退出码：0 成功，1 失败
    """
    parser = argparse.ArgumentParser(
        description="P1 研报因果链抽取（hybrid：LLM 优先 + 规则降级）"
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default="",
        help="输入目录（默认 data/reports/perilla_discovery/）",
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
    args = parser.parse_args()

    input_dir = Path(args.input_dir) if args.input_dir else None
    output_dir = Path(args.output_dir) if args.output_dir else None
    use_llm = not args.no_llm

    print("=" * 60)
    print("P1 研报因果链抽取（hybrid：LLM 优先 + 规则降级）")
    print("=" * 60)
    print(f"输入目录: {input_dir or 'data/reports/perilla_discovery/'}")
    print(f"输出目录: {output_dir or 'data/reports/perilla_discovery/'}")
    print(f"LLM 模式: {'hybrid（LLM 优先 + 规则降级）' if use_llm else '纯规则'}")
    print("-" * 60)

    summary = run_report_causal_extractor(
        input_dir=input_dir,
        output_dir=output_dir,
        use_llm=use_llm,
    )

    if not summary.get("success"):
        print(f"[失败] {summary.get('error', '未知错误')}")
        return 1

    counts = summary.get("counts", {})
    print(f"[成功] raw_documents: {counts.get('raw_documents', 0)}")
    print(f"       report_claims: {counts.get('report_claims', 0)}")
    print(f"       causal_chains: {counts.get('causal_chains', 0)}")
    print(f"       evidence_items: {counts.get('evidence_items', 0)}")
    print(f"       failure_conditions: {counts.get('failure_conditions', 0)}")

    # 打印 LLM 调用统计
    llm_stats = summary.get("llm_stats", {})
    if llm_stats:
        print("-" * 60)
        print("LLM 调用统计:")
        client_stats = llm_stats.get("client_stats", {})
        if client_stats:
            print(f"  总调用次数: {client_stats.get('llm_call_count', 0)}")
            print(f"  成功: {client_stats.get('success', 0)}")
            print(f"  失败: {client_stats.get('failure', 0)}")
            print(f"  降级到规则: {client_stats.get('fallback_rule_based', 0)}")
            print(f"  缓存命中: {client_stats.get('cache_hit', 0)}")
            print(f"  合规丢弃: {client_stats.get('compliance_drop', 0)}")
            print(f"  锚定丢弃: {client_stats.get('grounding_drop', 0)}")
            print(f"  粗略输入 tokens: {client_stats.get('prompt_tokens_est', 0)}")
            print(f"  粗略输出 tokens: {client_stats.get('completion_tokens_est', 0)}")
            print(f"  粗略总 tokens: {client_stats.get('total_tokens_est', 0)}")
        else:
            print("  （纯规则模式，无 LLM 调用）")

    # 打印 top 10 claims
    print("-" * 60)
    print("Top 10 claims:")
    try:
        import json
        out = Path(output_dir) if output_dir else _PROJECT_ROOT / "data" / "reports" / "perilla_discovery"
        claims_path = out / "report_claims_latest.json"
        if claims_path.exists():
            claims = json.loads(claims_path.read_text(encoding="utf-8"))
            for i, c in enumerate(claims[:10], 1):
                text = str(c.get("claim_text", ""))[:60]
                conf = c.get("confidence", "")
                signals = ",".join(c.get("signal_words", [])[:3])
                print(f"  {i}. [{conf}] {text} | 信号词: {signals}")
    except Exception as exc:
        print(f"  读取 claims 失败: {exc}")

    # 打印 top 10 causal_chains
    print("-" * 60)
    print("Top 10 causal_chains:")
    try:
        chains_path = out / "causal_chains_latest.json"
        if chains_path.exists():
            chains = json.loads(chains_path.read_text(encoding="utf-8"))
            for i, c in enumerate(chains[:10], 1):
                cause = str(c.get("cause", ""))[:30]
                effect = str(c.get("effect", ""))[:30]
                conf = c.get("confidence", "")
                print(f"  {i}. [{conf}] {cause} -> {effect}")
    except Exception as exc:
        print(f"  读取 chains 失败: {exc}")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
