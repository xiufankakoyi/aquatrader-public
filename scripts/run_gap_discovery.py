from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.runtime_factory import ResearchRuntimeFactory
from core.research.value_chain.gap_discovery import (
    FocusedRelationExtractor, GapDiscoveryExecutor, GapDiscoveryRetriever,
)
from core.research.value_chain.profit_discovery import ProfitBridgeDiscoveryExecutor


def main() -> int:
    parser = argparse.ArgumentParser(description="执行一个认知缺口发现任务")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    repository = ResearchRepository()
    task = repository.get_task(args.task_id)
    if task is None:
        raise SystemExit(f"任务不存在: {args.task_id}")
    runtime = ResearchRuntimeFactory(repository).build(
        as_of=str(task.params.get("as_of") or ""), task_context=task.params,
    )
    llm = FocusedRelationExtractor()
    discovery_retriever = GapDiscoveryRetriever(
        runtime.first_party_retriever,
        runtime.web_retriever,
        {
            **task.params,
            "symbol": task.params.get("target_entity", ""),
            "discovery_terms": ["主营业务", "产品服务于", "受益于", "算力基础设施", "产能", "研发"],
            "periodic_only": True,
        },
    )
    if task.params.get("gap") in {
        "forward_profit_bridge_missing", "company_forward_driver_evidence_missing",
    }:
        result = ProfitBridgeDiscoveryExecutor(repository, discovery_retriever).execute(task)
    else:
        result = GapDiscoveryExecutor(repository, discovery_retriever, llm).execute(task)
    result["llm_available"] = llm.available
    result["llm_stats"] = llm.stats.__dict__
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"succeeded", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
