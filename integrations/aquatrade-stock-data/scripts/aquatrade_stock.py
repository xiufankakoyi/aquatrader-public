#!/usr/bin/env python
"""Forward skill commands to the AquaTrade research data CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def find_project_root() -> Path:
    configured = os.getenv("AQUATRADE_ROOT", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend([Path.cwd(), *Path.cwd().parents])

    for candidate in candidates:
        cli = candidate / "scripts" / "research_data.py"
        if cli.is_file():
            return candidate.resolve()
    raise SystemExit(
        "AquaTrade repository not found. Run from the repository or set "
        "AQUATRADE_ROOT."
    )


def main() -> int:
    project_root = find_project_root()
    cli = project_root / "scripts" / "research_data.py"
    command = [sys.executable, str(cli), *sys.argv[1:]]
    return subprocess.call(command, cwd=str(project_root))


if __name__ == "__main__":
    raise SystemExit(main())
