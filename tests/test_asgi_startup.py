import socketio


def test_asgi_socketio_handlers_register_core_events():
    from server.asgi_socketio_handlers import register_handlers

    server = socketio.AsyncServer(async_mode="asgi")
    register_handlers(server)

    handlers = server.handlers.get("/", {})
    assert {"connect", "disconnect", "run_streaming_backtest"} <= set(handlers)


def test_startup_data_manager_is_available():
    from data_svc.unified_data_manager import get_unified_manager

    assert callable(get_unified_manager)
