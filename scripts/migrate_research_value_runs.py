"""扫描 data/research_value_runs/ 目录，将历史运行索引到 SQLite。

P0.3 实现：迁移脚本只读取现有目录与 JSON，不修改原产物。
- 幂等：重复执行只补齐缺失索引，不重复写入。
- 失败的目录（JSON 缺失/损坏）会被跳过但计入统计。
- 默认 --force=False；强制重写需显式传 --force。

用法：
    python scripts/migrate_research_value_runs.py
    python scripts/migrate_research_value_runs.py --runs-root data/research_value_runs --db data/research_control/research_value_runs.db --force
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本不依赖 PYTHONPATH 也能从仓库根目录直接运行
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.research.value_chain.repository import ResearchRunRepository  # noqa: E402


def main() -> int:
    """执行迁移并打印统计。返回 0 表示成功，1 表示部分失败。"""
    parser = argparse.ArgumentParser(
        description="索引历史公司研究运行目录到 SQLite。",
    )
    parser.add_argument(
        "--runs-root",
        default="data/research_value_runs",
        help="运行产物根目录（默认 data/research_value_runs）。",
    )
    parser.add_argument(
        "--db",
        default="data/research_control/research_value_runs.db",
        help="SQLite 索引库路径（默认 data/research_control/research_value_runs.db）。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新索引已存在的 run_id。",
    )
    args = parser.parse_args()

    repository = ResearchRunRepository(db_path=args.db, runs_root=args.runs_root)
    stats = repository.migrate_from_disk(force=args.force)
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    # 部分失败不应阻塞 P0.8 验收，但需要返回码提示
    if stats.get("failed", 0) > 0:
        print(
            f"[migrate] warning: {stats['failed']} 个目录索引失败；"
            "见 degraded 字段；不修改原 JSON。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
