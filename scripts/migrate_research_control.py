"""迁移旧研究 JSONL 到统一 SQLite 控制面。"""
from __future__ import annotations

import argparse
import json

from core.research.control_plane.migration import LazyResearchMigrator
from core.research.control_plane.repository import ResearchRepository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--kind", choices=("document", "evidence", "claim"), required=True)
    parser.add_argument("--claim-id", default="")
    parser.add_argument("--db", default="data/research_control/research_control.db")
    args = parser.parse_args()
    report = LazyResearchMigrator(ResearchRepository(args.db)).migrate_jsonl(args.path, args.kind, claim_id=args.claim_id)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
