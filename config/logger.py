"""
统一日志系统 - 提供统一的日志配置和管理

支持 DEBUG / INFO / WARNING / ERROR 四级日志
支持文件输出和控制台输出
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional


class ColoredFormatter(logging.Formatter):
    """带颜色的日志格式化器"""
    
    # ANSI 颜色代码
    COLORS = {
        'DEBUG': '\033[36m',      # 青色
        'INFO': '\033[32m',      # 绿色
        'WARNING': '\033[33m',    # 黄色
        'ERROR': '\033[31m',      # 红色
        'CRITICAL': '\033[35m',   # 紫色
        'RESET': '\033[0m',       # 重置
    }
    
    def format(self, record):
        # 添加颜色
        if sys.stdout.isatty():  # 只在终端中显示颜色
            log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
            record.levelname = f"{log_color}{record.levelname}{self.COLORS['RESET']}"
        
        return super().format(record)


def setup_logger(
    name: str = "aquatrade",
    level: Optional[int] = None,
    log_file: Optional[Path] = None,
    format_string: Optional[str] = None,
    use_color: bool = True,
) -> logging.Logger:
    """
    设置并返回一个配置好的 logger
    
    Args:
        name: logger 名称
        level: 日志级别 (logging.DEBUG, INFO, WARNING, ERROR)
        log_file: 日志文件路径（可选）
        format_string: 自定义格式字符串（可选）
        use_color: 是否在控制台使用颜色（默认: True）
        
    Returns:
        配置好的 logger 实例
    """
    logger = logging.getLogger(name)
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    # 默认级别：从环境变量读取，如果没有则使用 WARNING（只显示警告和错误）
    if level is None:
        log_level_str = os.getenv('LOG_LEVEL', 'WARNING').upper()
        level_map = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL,
        }
        level = level_map.get(log_level_str, logging.WARNING)
    
    logger.setLevel(level)
    
    # 默认格式
    if format_string is None:
        format_string = (
            '[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s'
        )
    
    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    if use_color:
        console_formatter = ColoredFormatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    else:
        console_formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # 文件 handler（如果指定）
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(level)
        file_formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    获取 logger 实例（如果不存在则创建）
    
    Args:
        name: logger 名称，默认为 "aquatrade"
        
    Returns:
        logger 实例
    """
    logger_name = name or "aquatrade"
    logger = logging.getLogger(logger_name)
    
    # 如果 logger 还没有配置，使用默认配置
    if not logger.handlers:
        setup_logger(logger_name)
    
    return logger


# 创建默认 logger（默认级别为 WARNING，只显示警告和错误）
default_logger = setup_logger("aquatrade")

