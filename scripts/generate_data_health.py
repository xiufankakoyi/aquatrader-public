#!/usr/bin/env python
"""Generate the canonical AquaTrade data-health JSON and Markdown reports."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.services.data_health_service import build_data_health_report


def main() -> int:
    report = build_data_health_report(write_files=True)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if report.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
