"""路由注册契约：模块故障不能被启动流程静默吞掉。"""

import builtins

import pytest
from flask import Flask


def test_route_registration_surfaces_module_import_error(monkeypatch):
    import server.routes as routes

    original_import = builtins.__import__

    def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "server.routes.strategy_routes":
            raise RuntimeError("route module unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(routes, "__import__", failing_import, raising=False)

    with pytest.raises(RuntimeError, match="route module unavailable"):
        routes.register_routes(Flask(__name__))
