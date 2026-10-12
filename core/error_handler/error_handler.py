"""
错误处理器核心模块
================
提供统一的错误捕获、分类和处理接口。
"""

import sys
import traceback
from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path


class ErrorLevel(Enum):
    """错误级别"""
    CRITICAL = "critical"  # 系统无法继续运行
    ERROR = "error"        # 功能失败但系统可继续
    WARNING = "warning"    # 潜在问题
    INFO = "info"          # 信息性错误


class ErrorHandler:
    """
    全栈错误处理器
    
    负责捕获、分类、记录错误，并提供错误恢复机制
    """
    
    _instance: Optional['ErrorHandler'] = None
    _logger = None
    _stats_collector = None
    
    def __init__(self):
        """初始化错误处理器"""
        from .error_logger import ErrorLogger
        self.error_logger = ErrorLogger()
        self.error_count = 0
        self.last_errors = []  # 保留最近的 10 个错误
        self.max_recent_errors = 10
    
    @classmethod
    def get_instance(cls) -> 'ErrorHandler':
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def capture(
        cls,
        exception: Exception,
        level: ErrorLevel = ErrorLevel.ERROR,
        category: str = "general",
        context: Optional[Dict[str, Any]] = None,
        notify_user: bool = False
    ) -> Dict[str, Any]:
        """
        捕获并记录错误
        
        Args:
            exception: 捕获的异常对象
            level: 错误级别
            category: 错误类别（如 database, network, business等）
            context: 额外的上下文信息
            notify_user: 是否通知用户
        
        Returns:
            错误记录字典
        """
        instance = cls.get_instance()
        
        # 构建错误记录
        error_record = {
            'timestamp': datetime.now().isoformat(),
            'level': level.value,
            'category': category,
            'exception_type': type(exception).__name__,
            'message': str(exception),
            'traceback': traceback.format_exc(),
            'context': context or {},
        }
        
        # 记录到日志
        instance.error_logger.log_error(error_record)
        
        # 更新统计
        instance.error_count += 1
        instance.last_errors.append(error_record)
        if len(instance.last_errors) > instance.max_recent_errors:
            instance.last_errors.pop(0)
        
        # 控制台输出
        cls._print_error(error_record, level)
        
        # 如果是 CRITICAL，可能需要特殊处理
        if level == ErrorLevel.CRITICAL:
            cls._handle_critical_error(error_record)
        
        return error_record
    
    @classmethod
    def _print_error(cls, error_record: Dict[str, Any], level: ErrorLevel):
        """打印错误到控制台"""
        prefix_map = {
            ErrorLevel.CRITICAL: "[CRITICAL ERROR]",
            ErrorLevel.ERROR: "[ERROR]",
            ErrorLevel.WARNING: "[WARNING]",
            ErrorLevel.INFO: "[INFO]"
        }
        
        prefix = prefix_map.get(level, "[ERROR]")
        category = error_record.get('category', 'general')
        message = error_record.get('message', '')
        
        print(f"\n{prefix} [{category}] {message}")
        
        # CRITICAL 和 ERROR 级别打印堆栈
        if level in (ErrorLevel.CRITICAL, ErrorLevel.ERROR):
            print(f"异常类型: {error_record.get('exception_type')}")
            if error_record.get('context'):
                print(f"上下文: {error_record.get('context')}")
    
    @classmethod
    def _handle_critical_error(cls, error_record: Dict[str, Any]):
        """处理致命错误"""
        print("\n" + "="*80)
        print("检测到致命错误！系统可能无法继续运行。")
        print("="*80)
        print(f"错误信息: {error_record.get('message')}")
        print(f"发生时间: {error_record.get('timestamp')}")
        print("\n请检查日志文件获取详细信息。")
        print("="*80 + "\n")
    
    @classmethod
    def get_stats(cls) -> Dict[str, Any]:
        """获取错误统计信息"""
        instance = cls.get_instance()
        return {
            'total_errors': instance.error_count,
            'recent_errors': instance.last_errors,
        }
    
    @classmethod
    def create_user_friendly_message(cls, exception: Exception, category: str = "") -> str:
        """
        创建用户友好的错误消息
        
        Args:
            exception: 异常对象
            category: 错误类别
        
        Returns:
            用户友好的错误消息
        """
        error_type = type(exception).__name__
        
        # 根据错误类型提供友好消息
        friendly_messages = {
            'FileNotFoundError': '找不到指定的文件或目录',
            'PermissionError': '没有足够的权限执行此操作',
            'ConnectionError': '网络连接失败，请检查网络设置',
            'TimeoutError': '操作超时，请稍后重试',
            'ValueError': '参数值不正确',
            'KeyError': '找不到指定的键',
            'AttributeError': '对象不支持该操作',
        }
        
        base_message = friendly_messages.get(error_type, '发生了一个错误')
        
        if category:
            return f"[{category}] {base_message}: {str(exception)}"
        return f"{base_message}: {str(exception)}"
