"""
回测任务模块（本地回退实现）

完整实现需接入 Celery 与消息队列。当前提供与 Celery Task 兼容的占位对象，
在 .delay() / .apply_async() 被调用时抛出清晰错误，避免 ImportError 导致服务启动失败。
"""
from typing import Any, Callable


class _NoCeleryTask:
    """模拟 Celery Task 的占位对象，调用异步方法时给出明确提示。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def delay(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            f"异步任务 '{self.name}' 需要 Celery 支持。"
            "请安装 celery 并配置 broker，或实现本地同步回退。"
        )

    def apply_async(self, *args: Any, **kwargs: Any) -> Any:
        return self.delay(*args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            f"同步调用 '{self.name}' 需要 Celery 支持。"
        )


def _task(name: str) -> Callable[..., Any]:
    """装饰器占位，忽略被装饰函数并返回 _NoCeleryTask 实例。"""
    def decorator(_func: Callable[..., Any]) -> _NoCeleryTask:
        return _NoCeleryTask(name)
    return decorator


@_task('backtest.run_backtest')
def run_backtest_task(*args: Any, **kwargs: Any) -> Any:
    """运行回测任务（需 Celery）"""
    raise RuntimeError('Celery 未安装，无法运行回测任务')


@_task('backtest.run_streaming_backtest')
def run_streaming_backtest_task(*args: Any, **kwargs: Any) -> Any:
    """运行流式回测任务（需 Celery）"""
    raise RuntimeError('Celery 未安装，无法运行流式回测任务')


@_task('backtest.run_optimization')
def run_optimization_task(*args: Any, **kwargs: Any) -> Any:
    """运行参数优化任务（需 Celery）"""
    raise RuntimeError('Celery 未安装，无法运行参数优化任务')


@_task('backtest.cancel_backtest')
def cancel_backtest_task(*args: Any, **kwargs: Any) -> Any:
    """取消回测任务（需 Celery）"""
    raise RuntimeError('Celery 未安装，无法取消回测任务')
