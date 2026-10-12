"""P3.8 可观察性 API 路由。

提供：
- GET /api/observability/health：综合健康检查（SQLite、文件校验、provider）
- GET /api/observability/metrics：指标快照（provider/queue/model）

设计原则：
- 降级处理：失败返回 503 + 错误信息
- 不写入敏感信息到响应
- 速率限制：默认每 IP 每分钟 30 次
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.runtime.observability import (
    HealthChecker,
    get_global_collector,
    get_global_limiter,
    check_provider_availability,
)
from server.services.data_health_service import PROJECT_ROOT

logger = logging.getLogger(__name__)

observability_bp = Blueprint(
    "observability",
    __name__,
    url_prefix="/api/observability",
)

# 默认配置的 provider 列表（用于健康检查）
_DEFAULT_PROVIDERS: list[str] = ["tushare", "akshare", "eastmoney"]


def _check_rate_limit(endpoint: str) -> tuple[bool, float]:
    """检查当前请求是否超过速率限制。

    Args:
        endpoint: 端点路径。

    Returns:
        (allowed, retry_after) 元组。
    """
    limiter = get_global_limiter()
    # 确保规则存在
    if limiter.get_rule(endpoint) is None:
        limiter.add_rule(endpoint, max_requests=30, window_seconds=60)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    # X-Forwarded-For 可能包含多个 IP，取第一个
    if "," in ip:
        ip = ip.split(",")[0].strip()
    return limiter.check(ip, endpoint)


@observability_bp.get("/health")
def get_observability_health() -> Any:
    """综合健康检查端点。

    返回 SQLite 一致性、文件校验和、provider 可用性的综合结果。
    失败时返回 503。
    """
    allowed, retry_after = _check_rate_limit("/api/observability/health")
    if not allowed:
        return jsonify({
            "success": False,
            "error": "rate limited",
            "retry_after": retry_after,
        }), 429
    try:
        checker = HealthChecker()
        # SQLite 一致性检查
        db_path = PROJECT_ROOT / "data" / "research_control" / "research_control.db"
        if db_path.exists():
            from core.runtime.observability import check_sqlite_integrity
            checker.add_check(
                "sqlite_integrity",
                lambda: check_sqlite_integrity(db_path),
            )
        # provider 可用性检查
        checker.add_check(
            "provider_availability",
            lambda: check_provider_availability(_DEFAULT_PROVIDERS),
        )
        report = checker.run_all()
        # 降级：error 状态返回 503
        status_code = 200 if report["status"] != "error" else 503
        return jsonify({"success": True, "data": report}), status_code
    except Exception as exc:  # 防御性 API 降级
        logger.exception("observability health check failed")
        return jsonify({
            "success": False,
            "data": {},
            "error": f"observability health unavailable: {exc}",
        }), 503


@observability_bp.get("/metrics")
def get_observability_metrics() -> Any:
    """指标快照端点。

    返回 provider/queue/model 指标的当前快照。
    失败时返回 503。
    """
    allowed, retry_after = _check_rate_limit("/api/observability/metrics")
    if not allowed:
        return jsonify({
            "success": False,
            "error": "rate limited",
            "retry_after": retry_after,
        }), 429
    try:
        collector = get_global_collector()
        snapshot = collector.snapshot()
        return jsonify({"success": True, "data": snapshot})
    except Exception as exc:  # 防御性 API 降级
        logger.exception("observability metrics snapshot failed")
        return jsonify({
            "success": False,
            "data": {},
            "error": f"observability metrics unavailable: {exc}",
        }), 503


@observability_bp.get("/rate_limiter")
def get_rate_limiter_snapshot() -> Any:
    """速率限制器快照端点（运维用）。

    返回当前速率限制规则与计数器状态。
    """
    allowed, retry_after = _check_rate_limit("/api/observability/rate_limiter")
    if not allowed:
        return jsonify({
            "success": False,
            "error": "rate limited",
            "retry_after": retry_after,
        }), 429
    try:
        limiter = get_global_limiter()
        snapshot = limiter.snapshot()
        return jsonify({"success": True, "data": snapshot})
    except Exception as exc:  # 防御性 API 降级
        logger.exception("rate limiter snapshot failed")
        return jsonify({
            "success": False,
            "data": {},
            "error": f"rate limiter snapshot unavailable: {exc}",
        }), 503
