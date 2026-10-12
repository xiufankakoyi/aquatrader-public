from __future__ import annotations

from flask import Flask

import server.routes.data_health_routes as routes


def test_data_health_route_smoke(monkeypatch) -> None:
    monkeypatch.setattr(
        routes,
        "build_data_health_report",
        lambda write_files=False: {"status": "ok", "datasets": [], "write": write_files},
    )
    app = Flask(__name__)
    app.register_blueprint(routes.data_health_bp)

    response = app.test_client().get("/api/data/health")

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "data": {"status": "ok", "datasets": [], "write": False},
    }
