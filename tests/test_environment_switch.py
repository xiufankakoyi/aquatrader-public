from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.research.adaptation.repository import AdaptationRepository
from core.research.candidate.repository import CandidateRepository
from core.research.expectations.repository import ExpectationRepository


def test_switch_env_accepts_absolute_experiment_directory(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "switch-env.ps1"
    command = (
        f". '{script}' -TargetEnv experiment -ExperimentDir '{tmp_path}'; "
        "@{app_env=$env:APP_ENV; db_path=$env:RESEARCH_CONTROL_DB_PATH} "
        "| ConvertTo-Json -Compress"
    )

    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["app_env"] == "experiment"
    assert Path(payload["db_path"]) == tmp_path / "research_control.db"
    assert (tmp_path / "reports").is_dir()
    assert (tmp_path / "lancedb").is_dir()
    assert (tmp_path / "logs").is_dir()


def test_research_repositories_use_isolated_environment_paths(tmp_path: Path, monkeypatch) -> None:
    expected = {
        "EXPECTATIONS_DB_PATH": tmp_path / "expectations.db",
        "CANDIDATES_DB_PATH": tmp_path / "candidates.db",
        "ADAPTATION_DB_PATH": tmp_path / "adaptation.db",
    }
    for key, path in expected.items():
        monkeypatch.setenv(key, str(path))

    repositories = (ExpectationRepository(), CandidateRepository(), AdaptationRepository())

    assert [repo.db_path for repo in repositories] == list(expected.values())
