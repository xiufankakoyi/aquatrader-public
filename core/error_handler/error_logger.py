"""
错误日志记录器
=============
将错误记录到 JSON 格式的日志文件中
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any
from config.logger import get_logger


class ErrorLogger:
    """错误日志记录器"""
    
    def __init__(self, log_dir: str = None):
        """
        初始化错误日志记录器
        
        Args:
            log_dir: 日志目录，默认为项目根目录下的 logs/errors
        """
        if log_dir is None:
            # 默认使用项目根目录下的 logs/errors
            from config.config import Config
            self.log_dir = Path(Config.BASE_DIR) / "logs" / "errors"
        else:
            self.log_dir = Path(log_dir)
        
        # 确保日志目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self._logger = get_logger(__name__)
        self._current_log_file = None
        self._update_log_file()
    
    def _update_log_file(self):
        """更新当前日志文件路径（按日期轮转）"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        self._current_log_file = self.log_dir / f"errors_{date_str}.jsonl"
    
    def log_error(self, error_record: Dict[str, Any]):
        """
        记录错误到 JSON Lines 文件
        
        Args:
            error_record: 错误记录字典
        """
        try:
            # 检查是否需要轮转日志文件
            self._update_log_file()
            
            # 写入 JSON Lines 格式（每行一个 JSON 对象）
            with open(self._current_log_file, 'a', encoding='utf-8') as f:
                json.dump(error_record, f, ensure_ascii=False)
                f.write('\n')
        
        except Exception as e:
            # 日志记录失败不应该影响主程序
            print(f"[ErrorLogger] 写入错误日志失败: {e}")
    
    def get_recent_errors(self, limit: int = 100) -> list:
        """
        获取最近的错误记录
        
        Args:
            limit: 返回的最大记录数
        
        Returns:
            错误记录列表
        """
        try:
            self._update_log_file()
            
            if not self._current_log_file.exists():
                return []
            
            errors = []
            with open(self._current_log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        error = json.loads(line.strip())
                        errors.append(error)
                    except json.JSONDecodeError:
                        continue
            
            # 返回最近的 N 条
            return errors[-limit:] if len(errors) > limit else errors
        
        except Exception as e:
            print(f"[ErrorLogger] 读取错误日志失败: {e}")
            return []
    
    def get_error_count_by_category(self, date: str = None) -> Dict[str, int]:
        """
        统计指定日期的错误数量（按类别）
        
        Args:
            date: 日期字符串 (YYYY-MM-DD)，默认为今天
        
        Returns:
            {category: count} 字典
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        log_file = self.log_dir / f"errors_{date}.jsonl"
        
        if not log_file.exists():
            return {}
        
        category_counts = {}
        
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        error = json.loads(line.strip())
                        category = error.get('category', 'unknown')
                        category_counts[category] = category_counts.get(category, 0) + 1
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"[ErrorLogger] 统计错误失败: {e}")
        
        return category_counts
