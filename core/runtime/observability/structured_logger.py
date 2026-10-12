"""结构化 JSON 日志器。

按 P3.8 生产可靠性要求：
- 输出 JSON 格式日志，包含 trace_id、阶段、耗时、失败原因、模型版本、降级状态
- 密钥脱敏：对常见敏感字段名进行遮蔽，防止密钥泄露到日志
- 兼容标准 logging 模块，可作为 handler 或独立 logger 使用
- 不记录任何原始密钥、token 或密码
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from core.runtime.observability.trace_context import get_trace_id


# 敏感字段名正则：匹配 key/password/secret/token/api_key 等模式（不区分大小写）
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|bearer|authorization|credential)"
)
# 脱敏后的占位值
_MASKED_VALUE = "***REDACTED***"
# 默认敏感字段的 key 集合（用于精确匹配）
_DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_key", "private_key", "client_secret", "bearer",
    "authorization", "credential", "llm_api_key", "tushare_token",
    "openai_api_key", "deepseek_api_key",
})


def is_sensitive_key(key: str) -> bool:
    """判断字段名是否敏感。

    Args:
        key: 字段名。

    Returns:
        True 表示该字段名属于敏感字段。
    """
    if not key:
        return False
    lower_key = str(key).lower()
    if lower_key in _DEFAULT_SENSITIVE_KEYS:
        return True
    return bool(_SENSITIVE_KEY_PATTERN.search(lower_key))


def mask_sensitive(value: Any, sensitive_keys: Iterable[str] | None = None) -> Any:
    """递归脱敏字典中的敏感字段。

    Args:
        value: 待脱敏的值；通常是 dict，也支持 list/str/其他。
        sensitive_keys: 额外的敏感字段名集合；与默认集合合并。

    Returns:
        脱敏后的值；非 dict/list 原样返回。
    """
    extra = {str(k).lower() for k in (sensitive_keys or [])}
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, val in value.items():
            lower_key = str(key).lower()
            if lower_key in _DEFAULT_SENSITIVE_KEYS or lower_key in extra or is_sensitive_key(key):
                masked[key] = _MASKED_VALUE
            else:
                masked[key] = mask_sensitive(val, sensitive_keys)
        return masked
    if isinstance(value, list):
        return [mask_sensitive(item, sensitive_keys) for item in value]
    return value


class StructuredLogRecord(logging.LogRecord):
    """结构化日志记录，附加 trace_id 和业务字段。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 自动注入当前 trace_id
        self.trace_id = get_trace_id()


class StructuredLogger(logging.Logger):
    """结构化 JSON 日志器。

    通过 ``log``/``info``/``warning``/``error`` 方法的 ``extra`` 参数
    传递业务字段；输出时合并 trace_id、阶段、耗时、模型版本、降级状态等。
    """

    def makeRecord(  # type: ignore[override]
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: str,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: dict[str, Any] | None = None,
        sinfo: str | None = None,
    ) -> StructuredLogRecord:
        """创建结构化日志记录；自动注入 trace_id。"""
        record = StructuredLogRecord(
            name, level, fn, lno, msg, args, exc_info, func, sinfo
        )
        if extra is not None:
            for key, value in extra.items():
                if key in {"message", "asctime"}:
                    continue
                # 脱敏后写入 record
                record.__dict__[key] = mask_sensitive(value)
        return record


class JsonFormatter(logging.Formatter):
    """JSON 格式化器；输出单行 JSON 日志。

    输出字段：
    - ``ts``: ISO8601 时间戳
    - ``level``: 日志级别
    - ``logger``: logger 名称
    - ``trace_id``: 当前上下文 trace_id
    - ``message``: 日志消息
    - ``stage``: 阶段名（来自 extra）
    - ``duration_ms``: 阶段耗时（毫秒，来自 extra）
    - ``error``: 失败原因（来自 extra）
    - ``model_version``: 模型版本（来自 extra）
    - ``degraded``: 降级状态（来自 extra）
    - 其他 extra 字段原样输出（已脱敏）
    """

    # 结构化字段白名单（保证关键字段始终存在）
    STRUCTURED_FIELDS: tuple[str, ...] = (
        "stage", "duration_ms", "error", "model_version",
        "degraded", "provider", "task_id", "run_id",
    )

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为 JSON 字符串。"""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", "") or get_trace_id(),
            "message": record.getMessage(),
        }
        # 合并结构化字段
        for field_name in self.STRUCTURED_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None and value != "":
                payload[field_name] = mask_sensitive(value)
        # 合并其他 extra 字段（排除 logging 内部字段）
        reserved = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "asctime", "trace_id",
        } | set(self.STRUCTURED_FIELDS)
        for key, value in record.__dict__.items():
            if key not in reserved and not key.startswith("_"):
                payload[key] = mask_sensitive(value)
        # 异常信息
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_structured_logging(
    logger_name: str = "aquatrade",
    level: int = logging.INFO,
    stream: Any = None,
) -> StructuredLogger:
    """配置结构化 JSON 日志。

    Args:
        logger_name: logger 名称。
        level: 日志级别。
        stream: 输出流；默认 ``sys.stdout``。

    Returns:
        配置好的 StructuredLogger 实例。
    """
    # 临时替换 logger 类以注入 trace_id
    original_cls = logging.getLoggerClass()
    logging.setLoggerClass(StructuredLogger)
    try:
        logger = logging.getLogger(logger_name)
    finally:
        logging.setLoggerClass(original_cls)
    # 强制转换类型（保证类型检查通过）
    if not isinstance(logger, StructuredLogger):
        # 已经存在同名的非 StructuredLogger 实例；手动替换 __class__
        logger.__class__ = StructuredLogger
    logger.setLevel(level)
    # 避免重复添加 handler
    has_json_handler = any(
        isinstance(h, logging.StreamHandler)
        and isinstance(h.formatter, JsonFormatter)
        for h in logger.handlers
    )
    if not has_json_handler:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_structured_logger(name: str = "aquatrade") -> StructuredLogger:
    """获取结构化日志器；若未配置则惰性配置。

    Args:
        name: logger 名称。

    Returns:
        StructuredLogger 实例。
    """
    logger = logging.getLogger(name)
    if not isinstance(logger, StructuredLogger):
        logger.__class__ = StructuredLogger
    if not logger.handlers:
        configure_structured_logging(name)
    return logger  # type: ignore[return-value]


# 模块级锁，保证 configure_structured_logging 线程安全
_configure_lock = threading.Lock()


def safe_log(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    stage: str = "",
    duration_ms: float | None = None,
    error: str = "",
    model_version: str = "",
    degraded: bool | None = None,
    provider: str = "",
    **extra: Any,
) -> None:
    """安全记录结构化日志；自动脱敏。

    Args:
        logger: 日志器实例。
        level: 日志级别。
        message: 日志消息。
        stage: 阶段名。
        duration_ms: 阶段耗时（毫秒）。
        error: 失败原因。
        model_version: 模型版本。
        degraded: 降级状态。
        provider: 数据源名称。
        **extra: 其他业务字段；自动脱敏。
    """
    extra_payload: dict[str, Any] = dict(extra)
    if stage:
        extra_payload["stage"] = stage
    if duration_ms is not None:
        extra_payload["duration_ms"] = round(duration_ms, 3)
    if error:
        extra_payload["error"] = error
    if model_version:
        extra_payload["model_version"] = model_version
    if degraded is not None:
        extra_payload["degraded"] = degraded
    if provider:
        extra_payload["provider"] = provider
    # 脱敏 extra 后传入
    masked_extra = mask_sensitive(extra_payload)
    with _configure_lock:
        logger.log(level, message, extra=masked_extra)


__all__ = [
    "JsonFormatter",
    "StructuredLogger",
    "StructuredLogRecord",
    "configure_structured_logging",
    "get_structured_logger",
    "is_sensitive_key",
    "mask_sensitive",
    "safe_log",
]
