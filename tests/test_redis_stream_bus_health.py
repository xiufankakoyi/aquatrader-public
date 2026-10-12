from core.events.redis_stream_bus import RedisStreamBus


def test_redis_stream_health_exposes_transport_failure_state() -> None:
    bus = object.__new__(RedisStreamBus)
    bus._available = False
    bus._last_error = "redis_unavailable:ConnectionError"
    bus.stream_prefix = "aquatrade:events"

    assert bus.last_error == "redis_unavailable:ConnectionError"
    assert bus.health() == {
        "redis_available": False,
        "local_fallback": True,
        "last_error": "redis_unavailable:ConnectionError",
        "stream_prefix": "aquatrade:events",
    }
