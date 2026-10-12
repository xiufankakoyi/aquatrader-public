"""
全栈错误捕获器
================
系统化地捕获、记录和分析应用中的所有错误。

使用方式:
    from core.error_handler import ErrorHandler, capture_error, ErrorContext
    
    # 装饰器方式
    @capture_error(category="database")
    def my_function():
        pass
    
    # 上下文管理器
    with ErrorContext("operation_name"):
        risky_code()
    
    # 手动捕获
    try:
        risky_code()
    except Exception as e:
        ErrorHandler.capture(e, context={"key": "value"})
"""

from .error_handler import ErrorHandler, ErrorLevel
from .decorators import capture_error, ErrorContext
from .error_logger import ErrorLogger

__all__ = [
    'ErrorHandler',
    'ErrorLevel',
    'capture_error',
    'ErrorContext',
    'ErrorLogger',
]
