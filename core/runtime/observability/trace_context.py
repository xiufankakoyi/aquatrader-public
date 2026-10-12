"""请求级 trace_id 上下文与 span 计时。

按 P3.8 生产可靠性要求：
- 使用 contextvars 实现请求级 trace_id 传递，异步安全
- 提供 span 计时能力，记录阶段耗时
- trace_id 缺失时自动生成，保证可观察性
- 不记录任何密钥或敏感输入
"""
from __future__ import annotations

import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator


# 请求级 trace_id 上下文变量；contextvars 保证协程/线程间隔离
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aquatrade_trace_id", default=""
)
# span 堆栈上下文变量；嵌套 span 按层级记录
# 注意：ContextVar 不支持 default_factory，用 None 占位并在获取时初始化
_span_stack_var: contextvars.ContextVar[list["Span"] | None] = contextvars.ContextVar(
    "aquatrade_span_stack", default=None
)


def _get_span_stack() -> list["Span"]:
    """获取当前上下文的 span 堆栈；若未初始化则设置为空列表。

    Returns:
        当前上下文的 span 堆栈（可变列表）。
    """
    stack = _span_stack_var.get()
    if stack is None:
        stack = []
        _span_stack_var.set(stack)
    return stack


def new_trace_id() -> str:
    """生成新的 trace_id。

    Returns:
        形如 ``trace_<hex16>`` 的唯一标识符。
    """
    return f"trace_{uuid.uuid4().hex[:16]}"


def get_trace_id() -> str:
    """获取当前上下文的 trace_id；缺失时返回空字符串。

    Returns:
        当前 trace_id；未设置时返回 ``""``。
    """
    return _trace_id_var.get()


def set_trace_id(trace_id: str | None = None) -> str:
    """设置当前上下文的 trace_id；为空时自动生成。

    Args:
        trace_id: 指定的 trace_id；``None`` 或空串时自动生成新值。

    Returns:
        实际生效的 trace_id。
    """
    value = trace_id or new_trace_id()
    _trace_id_var.set(value)
    return value


def reset_trace_id() -> None:
    """清空当前上下文的 trace_id 与 span 堆栈。

    主要用于测试隔离，避免上下文污染。
    """
    _trace_id_var.set("")
    _span_stack_var.set(None)


@dataclass
class Span:
    """单个 span 记录，描述一个阶段的执行情况。

    Attributes:
        name: 阶段名称（如 ``research.premarket``）。
        trace_id: 关联的 trace_id。
        started_at: 开始时间戳（秒）。
        finished_at: 结束时间戳（秒）；未结束时为 0。
        attributes: 附加属性；不记录密钥。
        error: 失败原因；成功时为空。
    """

    name: str
    trace_id: str
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def duration_seconds(self) -> float:
        """span 耗时（秒）；未结束时返回已运行时长。"""
        end = self.finished_at if self.finished_at else time.monotonic()
        return max(0.0, end - self.started_at)

    @property
    def is_finished(self) -> bool:
        """span 是否已结束。"""
        return self.finished_at > 0.0

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 6),
            "attributes": dict(self.attributes),
            "error": self.error,
            "is_finished": self.is_finished,
        }

    def set_attribute(self, key: str, value: Any) -> None:
        """设置 span 附加属性。

        Args:
            key: 属性名。
            value: 属性值；禁止传入密钥。
        """
        self.attributes[key] = value

    def record_error(self, message: str) -> None:
        """记录 span 失败原因。

        Args:
            message: 失败原因描述。
        """
        self.error = message


class TraceContext:
    """trace 上下文管理器，绑定 trace_id 并提供 span 计时。

    用法::

        with TraceContext("research.run") as ctx:
            ctx.set_attribute("mode", "premarket")
            with ctx.span("theme_scan") as span:
                ...
            ctx.record_error("theme scan timeout")

    Notes:
        - 上下文退出后 trace_id 自动恢复，避免污染外层
        - span 嵌套时按层级记录到 span 堆栈
    """

    def __init__(self, name: str = "root", trace_id: str | None = None) -> None:
        """初始化 trace 上下文。

        Args:
            name: 根 span 名称。
            trace_id: 指定的 trace_id；``None`` 时复用当前上下文或自动生成。
        """
        self._token_trace: contextvars.Token[str] | None = None
        self._token_spans: contextvars.Token[list[Span]] | None = None
        self._root_span = Span(name=name, trace_id="")
        self._trace_id = trace_id or ""

    @property
    def trace_id(self) -> str:
        """当前上下文绑定的 trace_id。"""
        return self._root_span.trace_id

    @property
    def root_span(self) -> Span:
        """根 span。"""
        return self._root_span

    @property
    def spans(self) -> list[Span]:
        """当前 span 堆栈的快照（浅拷贝）。"""
        return list(_get_span_stack())

    def __enter__(self) -> "TraceContext":
        """进入上下文；绑定 trace_id 并启动根 span。"""
        effective_id = self._trace_id or get_trace_id() or new_trace_id()
        self._token_trace = _trace_id_var.set(effective_id)
        self._token_spans = _span_stack_var.set([])
        self._root_span.trace_id = effective_id
        self._root_span.started_at = time.monotonic()
        _get_span_stack().append(self._root_span)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出上下文；关闭根 span 并恢复外层 trace_id。"""
        self._root_span.finished_at = time.monotonic()
        if exc is not None:
            self._root_span.error = f"{type(exc).__name__}: {exc}"
        # 弹出根 span
        stack = _get_span_stack()
        if stack and stack[-1] is self._root_span:
            stack.pop()
        if self._token_trace is not None:
            _trace_id_var.reset(self._token_trace)
        if self._token_spans is not None:
            _span_stack_var.reset(self._token_spans)

    def span(self, name: str, **attributes: Any) -> "_SpanScope":
        """创建子 span 上下文管理器。

        Args:
            name: span 名称。
            **attributes: 附加属性；禁止传入密钥。

        Returns:
            _SpanScope 上下文管理器。
        """
        return _SpanScope(name=name, trace_id=self.trace_id, attributes=attributes)

    def set_attribute(self, key: str, value: Any) -> None:
        """为根 span 设置附加属性。

        Args:
            key: 属性名。
            value: 属性值；禁止传入密钥。
        """
        self._root_span.attributes[key] = value

    def record_error(self, message: str) -> None:
        """记录根 span 的失败原因。

        Args:
            message: 失败原因描述。
        """
        self._root_span.error = message

    def to_dict(self) -> dict[str, Any]:
        """导出上下文快照。"""
        return {
            "trace_id": self.trace_id,
            "root_span": self._root_span.to_dict(),
            "span_count": len(self.spans),
        }


class _SpanScope:
    """子 span 上下文管理器；进入/退出时压栈/弹栈。"""

    def __init__(
        self,
        name: str,
        trace_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self._span = Span(
            name=name,
            trace_id=trace_id,
            attributes=dict(attributes or {}),
        )

    @property
    def span(self) -> Span:
        """当前 span。"""
        return self._span

    def __enter__(self) -> Span:
        """进入 span；压栈并返回 Span 对象。"""
        self._span.started_at = time.monotonic()
        _get_span_stack().append(self._span)
        return self._span

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出 span；弹栈并记录失败原因。"""
        self._span.finished_at = time.monotonic()
        if exc is not None:
            self._span.error = f"{type(exc).__name__}: {exc}"
        stack = _get_span_stack()
        if stack and stack[-1] is self._span:
            stack.pop()

    def set_attribute(self, key: str, value: Any) -> None:
        """设置 span 附加属性。"""
        self._span.attributes[key] = value

    def record_error(self, message: str) -> None:
        """记录 span 失败原因。"""
        self._span.error = message


def iter_spans() -> Iterator[Span]:
    """迭代当前上下文的 span 堆栈（从根到当前）。"""
    yield from _get_span_stack()


__all__ = [
    "Span",
    "TraceContext",
    "get_trace_id",
    "iter_spans",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
]
