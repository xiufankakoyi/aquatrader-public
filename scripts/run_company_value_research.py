from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.value_chain import CompanyValueResearchService


def main() -> int:
    parser = argparse.ArgumentParser(description="运行一次证据优先的公司价值研究档案")
    parser.add_argument("--symbol", required=True, help="证券代码，例如 300308.SZ")
    parser.add_argument("--as-of", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--output-root", default="data/research_value_runs")
    parser.add_argument("--enqueue-gaps", action="store_true", help="将验证后的研究缺口写入统一闲时任务队列")
    args = parser.parse_args()
    result = CompanyValueResearchService().run(
        args.symbol,
        as_of=args.as_of,
        lookback_days=args.lookback_days,
        output_root=args.output_root,
        enqueue_gaps=args.enqueue_gaps,
    )
    print(json.dumps({
        "run_id": result["run_id"],
        "run_dir": result["run_dir"],
        "acceptance": result["acceptance"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
