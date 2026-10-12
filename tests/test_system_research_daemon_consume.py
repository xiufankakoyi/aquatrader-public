from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from server.routes import system_routes


def _app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(system_routes.system_bp)
    return app


def test_consume_worker_uses_project_root_and_records_success(tmp_path: Path) -> None:
    status_path = tmp_path / "consume.json"
    observed: dict = {}

    def runner(command, **kwargs):
        observed["command"] = command
        observed["cwd"] = kwargs["cwd"]
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = system_routes._execute_research_consume(
        task_id="consume_test",
        workers=4,
        batch=12,
        status_path=status_path,
        runner=runner,
    )

    assert result["status"] == "succeeded"
    assert Path(observed["cwd"]) == system_routes.PROJECT_ROOT
    assert Path(observed["command"][1]) == (
        system_routes.PROJECT_ROOT / "sandbox" / "drain_search_tasks.py"
    )
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "succeeded"


def test_consume_worker_records_nonzero_exit(tmp_path: Path) -> None:
    status_path = tmp_path / "consume.json"

    result = system_routes._execute_research_consume(
        task_id="consume_failed",
        workers=2,
        batch=5,
        status_path=status_path,
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=7, stdout="", stderr="worker failed"
        ),
    )

    assert result["status"] == "failed"
    assert result["return_code"] == 7
    assert "worker failed" in result["error"]
    assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_consume_route_rejects_invalid_limits() -> None:
    client = _app().test_client()

    response = client.post(
        "/api/research-daemon/consume",
        json={"workers": 0, "batch": 10000},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False

