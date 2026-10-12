from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_launchers_use_the_available_tushare_updater() -> None:
    """The opt-in production updater must point at the shipped module."""
    launcher_paths = (
        ROOT / "scripts" / "start" / "start_production.ps1",
        ROOT / "scripts" / "start" / "start_lancedb.ps1",
        ROOT / "server" / "start.bat",
    )
    missing_module = re.compile(r"(?:data_svc|database)\.tushare_updater(?:\s|['\"]|$)")

    assert importlib.util.find_spec("data_svc.tushare_updater_loop") is not None
    for path in launcher_paths:
        content = path.read_text(encoding="utf-8")
        assert "data_svc.tushare_updater_loop" in content
        assert not missing_module.search(content)
