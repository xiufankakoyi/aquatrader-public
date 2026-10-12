from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research_graph.evidence_corpus import validate_run_directory
from core.research_graph.evidence_miner import (
    CorpusEvidenceMiner,
    load_candidate_claims,
    summarize_mining_results,
    write_mining_results,
)


DEFAULT_RUN_DIR = PROJECT_ROOT / "data" / "research_runs" / "overnight_evidence_20260629_231522"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "research_runs"


def run_smoke(run_dir: Path, output_dir: Path, *, limit: int = 4) -> dict:
    corpus_summary = validate_run_directory(run_dir)
    claims = load_candidate_claims(run_dir / "candidate_claims.jsonl")
    miner = CorpusEvidenceMiner.from_overnight_run(run_dir)
    results = miner.mine_many(claims, limit=limit)
    summary = summarize_mining_results(results)
    summary.update({
        "source_run_dir": str(run_dir),
        "corpus_summary": corpus_summary,
        "retrieval_limit": limit,
        "status": "passed" if _passes(summary, len(claims)) else "failed",
    })

    output_dir.mkdir(parents=True, exist_ok=True)
    write_mining_results(output_dir / "evidence_mining_results.jsonl", results)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        "\n".join([
            "# Evidence Miner Smoke Run",
            "",
            "该目录由第三战役 Evidence Miner smoke 脚本生成。",
            "输出只包含候选证据、反证和检索追溯信息，不包含交易动作、资金安排或价格判断。",
            "",
            f"- source_run_dir: {run_dir}",
            f"- status: {summary['status']}",
            f"- claims: {summary['claims']}",
            f"- support_evidence: {summary['support_evidence']}",
            f"- counter_evidence: {summary['counter_evidence']}",
        ]),
        encoding="utf-8",
    )
    return summary


def _passes(summary: dict, expected_claims: int) -> bool:
    return (
        summary.get("claims") == expected_claims
        and summary.get("claims_with_retrieved_documents", 0) >= expected_claims
        and summary.get("claims_with_support_evidence", 0) >= max(1, expected_claims // 2)
        and summary.get("untraceable_evidence") == []
        and summary.get("non_candidate_updates") == []
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ResearchGraph Evidence Miner smoke validation.")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()

    output_dir = args.output_dir
    if output_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = DEFAULT_OUTPUT_ROOT / f"evidence_miner_smoke_{stamp}"
    summary = run_smoke(args.run_dir, output_dir, limit=args.limit)
    print(json.dumps({"output_dir": str(output_dir), **summary}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
