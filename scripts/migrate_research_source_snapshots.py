from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.value_chain.contracts import SourceSnapshot
from core.research.value_chain.source_gateway import ResearchSourceGateway


def migrate_dossier(path: Path, gateway: ResearchSourceGateway) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    symbol = str(payload.get("symbol") or "").upper()
    if not symbol:
        return 0
    migrated = 0
    for item in dict(payload.get("source_snapshots") or {}).values():
        if not isinstance(item, dict) or item.get("status") != "ok" or not item.get("records"):
            continue
        snapshot = SourceSnapshot(**{
            key: item[key]
            for key in SourceSnapshot.__dataclass_fields__
            if key in item
        })
        gateway._save_snapshot(symbol, snapshot)
        migrated += 1
    return migrated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将既有真实投研档案迁移到规范化本地来源快照缓存"
    )
    parser.add_argument("dossiers", nargs="+", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args()
    gateway = ResearchSourceGateway(cache_dir=args.cache_dir)
    total = sum(migrate_dossier(path, gateway) for path in args.dossiers)
    print(json.dumps({"dossiers": len(args.dossiers), "snapshots_migrated": total}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
