"""执行 P3-Engineering 正式认证前置检查（只读）。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.pit_certification.pit_runner import load_checkpoint_results
from core.research.pit_certification.production_preflight import evaluate_preflight


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _active_model_count(path: Path) -> int:
    payload = _read_json(path, {})
    models = payload.get("models", {}) if isinstance(payload, dict) else {}
    return sum(
        record.get("status") == "active"
        for versions in models.values()
        for record in versions
        if isinstance(record, dict)
    )


def _active_policy_count(path: Path) -> int:
    if not path.exists():
        return 0
    with sqlite3.connect(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='policy_versions'"
        ).fetchone()
        if not exists:
            return 0
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM policy_versions WHERE status='active'"
            ).fetchone()[0]
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="P3-Engineering 生产认证前置检查")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=PROJECT_ROOT / "data/research_control/p3_09_checkpoint/pit_certification_checkpoint.json",
    )
    parser.add_argument(
        "--provenance",
        type=Path,
        default=PROJECT_ROOT / "data/research_control/p3_engineering/provenance.json",
    )
    parser.add_argument(
        "--ai-review",
        type=Path,
        default=PROJECT_ROOT / "data/research_control/p3_engineering/ai_review.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data/research_control/p3_engineering/production_preflight.json",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    results = load_checkpoint_results(args.checkpoint) if args.checkpoint.exists() else []
    provenance_payload = _read_json(args.provenance, [])
    provenance = (
        provenance_payload.get("records", [])
        if isinstance(provenance_payload, dict)
        else provenance_payload
    )
    review = _read_json(args.ai_review, {})
    preflight = evaluate_preflight(
        results,
        provenance,
        review,
        active_model_count=_active_model_count(
            PROJECT_ROOT / "data/research_control/p3_model_registry.json"
        ),
        active_policy_count=_active_policy_count(
            PROJECT_ROOT / "data/research_control/research_control.db"
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": preflight["status"],
        "result_count": len(results),
        "blockers": preflight["blockers"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 2 if args.strict and preflight["status"] != "ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
