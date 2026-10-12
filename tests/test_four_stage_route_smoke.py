"""Full app registration smoke for legacy and four-stage research surfaces."""


def test_full_app_registers_legacy_and_new_research_routes():
    from server.app import app, socketio
    rules = {str(rule) for rule in app.url_map.iter_rules()}
    required = {
        "/api/backtest/run", "/api/portfolio/demo", "/api/similarity/status", "/api/task/health",
        "/api/research/earnings/health", "/api/research/expectations/health",
        "/api/research/adaptation/health",
    }
    assert required.issubset(rules)
    assert socketio is not None


def test_legacy_and_research_health_endpoints_coexist():
    from server.app import app
    client = app.test_client()
    for path in (
        "/api/task/health", "/api/similarity/status", "/api/research/earnings/health",
        "/api/research/expectations/health", "/api/research/adaptation/health",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.get_data(as_text=True))
