"""
错误捕获装饰器和上下文管理器
=========================
提供便捷的错误捕获方式
"""

import functools
from typing import Optional, Callable, Any
from .error_handler import ErrorHandler, ErrorLevel


def capture_error(
    category: str = "general",
    level: ErrorLevel = ErrorLevel.ERROR,
    reraise: bool = True,
    fallback_return: Any = None
):
    """
    装饰器：自动捕获函数中的异常
    
    Args:
        category: 错误类别
        level: 错误级别
        reraise: 是否重新抛出异常
        fallback_return: 如果不重新抛出异常，返回此值
    
    示例:
        @capture_error(category="database", level=ErrorLevel.ERROR)
        def query_database():
            # your code
            pass
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # 构建上下文
                context = {
                    'function': func.__name__,
                    'module': func.__module__,
                    'args': str(args)[:200],  # 限制长度
                    'kwargs': str(kwargs)[:200],
                }
                
                # 捕获错误
                ErrorHandler.capture(
                    exception=e,
                    level=level,
                    category=category,
                    context=context
                )
                
                # 决定是否重新抛出
                if reraise:
                    raise
                else:
                    return fallback_return
        
        return wrapper
    return decorator


class ErrorContext:
    """
    上下文管理器：在代码块中自动捕获异常
    
    示例:
        with ErrorContext("backtest_execution", level=ErrorLevel.CRITICAL):
            # your code
            run_backtest()
    """
    
    def __init__(
        self,
        operation_name: str,
        category: str = "general",
        level: ErrorLevel = ErrorLevel.ERROR,
        reraise: bool = True,
        context: Optional[dict] = None
    ):
        """
        Args:
            operation_name: 操作名称
            category: 错误类别
            level: 错误级别
            reraise: 是否重新抛出异常
            context: 额外的上下文信息
        """
        self.operation_name = operation_name
        self.category = category
        self.level = level
        self.reraise = reraise
        self.context = context or {}
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # 构建上下文
            full_context = {
                'operation': self.operation_name,
                **self.context
            }
            
            # 捕获错误
            ErrorHandler.capture(
                exception=exc_val,
                level=self.level,
                category=self.category,
                context=full_context
            )
            
            # 决定是否抑制异常
            if self.reraise:
                # Propagate exception
                return False
            else:
                # Suppress exception
                return True
