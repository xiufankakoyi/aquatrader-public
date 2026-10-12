"""P3.8 研究操作系统可观察性模块。

提供以下能力：
- ``trace_context``: 请求级 trace_id 上下文与 span 计时
- ``structured_logger``: 结构化 JSON 日志器，含密钥脱敏
- ``metrics``: 数据源/队列/模型指标收集器
- ``health_checks``: SQLite/文件/provider 健康检查
- ``backup_recovery``: 研究产物备份与恢复
- ``rate_limiter``: API 速率限制器

所有模块不依赖外部服务，可独立运行。
"""
from __future__ import annotations

from core.runtime.observability.backup_recovery import (
    BackupManager,
    BackupManifest,
    DEFAULT_EXCLUDE_PATTERNS,
)
from core.runtime.observability.health_checks import (
    HealthCheckResult,
    HealthChecker,
    check_file_checksums,
    check_provider_availability,
    check_sqlite_integrity,
    compute_file_checksums,
    write_checksum_manifest,
)
from core.runtime.observability.metrics import (
    MetricsCollector,
    ModelMetric,
    ProviderMetric,
    QueueMetric,
    get_global_collector,
)
from core.runtime.observability.rate_limiter import (
    RateLimitRule,
    RateLimiter,
    get_global_limiter,
)
from core.runtime.observability.structured_logger import (
    JsonFormatter,
    StructuredLogger,
    configure_structured_logging,
    get_structured_logger,
    is_sensitive_key,
    mask_sensitive,
    safe_log,
)
from core.runtime.observability.trace_context import (
    Span,
    TraceContext,
    get_trace_id,
    iter_spans,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
)

__all__ = [
    # trace_context
    "Span",
    "TraceContext",
    "get_trace_id",
    "iter_spans",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
    # structured_logger
    "JsonFormatter",
    "StructuredLogger",
    "configure_structured_logging",
    "get_structured_logger",
    "is_sensitive_key",
    "mask_sensitive",
    "safe_log",
    # metrics
    "MetricsCollector",
    "ModelMetric",
    "ProviderMetric",
    "QueueMetric",
    "get_global_collector",
    # health_checks
    "HealthCheckResult",
    "HealthChecker",
    "check_file_checksums",
    "check_provider_availability",
    "check_sqlite_integrity",
    "compute_file_checksums",
    "write_checksum_manifest",
    # backup_recovery
    "BackupManager",
    "BackupManifest",
    "DEFAULT_EXCLUDE_PATTERNS",
    # rate_limiter
    "RateLimitRule",
    "RateLimiter",
    "get_global_limiter",
]
