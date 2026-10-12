"""
DragonEye 任务状态管理器
管理爬虫、清洗、推送任务的执行状态和实时日志
"""
import json
import math
import os
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Callable, List
from dataclasses import dataclass, field, asdict
from uuid import uuid4
from config.config import Config
from config.logger import get_logger

logger = get_logger(__name__)


def _safe_progress(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(100.0, number))


def _parse_timestamp(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class JobStatus:
    """任务状态数据类"""
    job_id: str
    job_type: str  # 'crawl', 'clean', 'push', 'full_pipeline'
    status: str  # 'pending', 'running', 'completed', 'failed', 'cancelled'
    progress: float = 0.0  # 0-100
    message: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: Optional[str] = None
    error_info: Optional[str] = None
    logs: List[Dict] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at
        self.progress = _safe_progress(self.progress)
        self.logs = [item for item in self.logs if isinstance(item, dict)]
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    def add_log(self, level: str, message: str, progress: Optional[float] = None):
        """添加日志条目"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message
        }
        self.logs.append(log_entry)
        
        if progress is not None:
            self.progress = _safe_progress(progress, self.progress)
        
        self.message = message
        self.updated_at = datetime.now().isoformat()
        
        # 限制日志数量，防止内存溢出
        if len(self.logs) > 1000:
            self.logs = self.logs[-500:]
    
    def complete(self, success: bool = True, error: Optional[str] = None):
        """标记任务完成"""
        self.status = "completed" if success else "failed"
        self.completed_at = datetime.now().isoformat()
        self.error_info = error
        self.progress = 100.0 if success else self.progress
        self.updated_at = self.completed_at


class JobManager:
    """
    DragonEye 任务管理器
    
    功能：
    1. 管理任务生命周期（创建、更新、完成）
    2. 持久化任务状态到 JSON 文件
    3. 支持 SSE 实时日志推送
    4. 线程安全的任务操作
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """单例模式确保全局唯一实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._initialized = True
        self.jobs: Dict[str, JobStatus] = {}
        self.subscribers: Dict[str, List[Callable]] = {}  # job_id -> [callback]
        self.state_file = Path(Config.BASE_DIR) / "data" / "dragon_eye_jobs.json"
        self._file_lock = threading.Lock()
        
        # 加载历史任务状态
        self._load_state()
        
        logger.info(f"JobManager initialized, state file: {self.state_file}")
    
    def _load_state(self):
        """从文件加载任务状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        return
                    for job_id, job_data in data.items():
                        if not isinstance(job_data, dict):
                            continue
                        try:
                            job = JobStatus(**job_data)
                        except (TypeError, ValueError):
                            continue
                        self.jobs[str(job_id)] = job
                logger.info(f"Loaded {len(self.jobs)} jobs from state file")
            except Exception as e:
                logger.error(f"Failed to load job state: {e}")
    
    def _save_state(self):
        """保存任务状态到文件"""
        with self._file_lock:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                data = {job_id: job.to_dict() for job_id, job in self.jobs.items()}
                # 只保留最近7天的任务
                cutoff = time.time() - 7 * 24 * 3600
                filtered_data = {}
                for key, value in data.items():
                    created_at = _parse_timestamp(value.get('created_at'))
                    if created_at is not None and created_at.timestamp() > cutoff:
                        filtered_data[key] = value
                temp_path = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(
                        filtered_data,
                        f,
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.state_file)
            except Exception as e:
                logger.error(f"Failed to save job state: {e}")
    
    def create_job(self, job_type: str, job_id: Optional[str] = None) -> JobStatus:
        """
        创建新任务
        
        Args:
            job_type: 任务类型 ('crawl', 'clean', 'push', 'full_pipeline')
            job_id: 可选的任务ID，默认自动生成
            
        Returns:
            JobStatus: 创建的任务状态对象
        """
        if job_id is None:
            job_id = f"{job_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        if job_id in self.jobs:
            raise ValueError(f"job_id already exists: {job_id}")
        
        job = JobStatus(
            job_id=job_id,
            job_type=job_type,
            status="pending"
        )
        
        self.jobs[job_id] = job
        self.subscribers[job_id] = []
        self._save_state()
        
        logger.info(f"Created job {job_id} of type {job_type}")
        return job
    
    def get_job(self, job_id: str) -> Optional[JobStatus]:
        """获取任务状态"""
        return self.jobs.get(job_id)
    
    def update_job(self, job_id: str, **kwargs) -> Optional[JobStatus]:
        """
        更新任务状态
        
        Args:
            job_id: 任务ID
            **kwargs: 要更新的字段
            
        Returns:
            更新后的 JobStatus 或 None
        """
        job = self.jobs.get(job_id)
        if not job:
            return None
        
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        
        job.updated_at = datetime.now().isoformat()
        self._save_state()
        
        # 通知订阅者
        self._notify_subscribers(job_id, job)
        
        return job
    
    def add_log(self, job_id: str, level: str, message: str, progress: Optional[float] = None):
        """
        添加任务日志
        
        Args:
            job_id: 任务ID
            level: 日志级别 ('info', 'success', 'warning', 'error')
            message: 日志消息
            progress: 可选的进度百分比 (0-100)
        """
        job = self.jobs.get(job_id)
        if not job:
            return
        
        job.add_log(level, message, progress)
        self._save_state()
        
        # 通知订阅者
        self._notify_subscribers(job_id, job)
        
        # 同时记录到系统日志
        log_func = getattr(logger, level, logger.info)
        log_func(f"[{job_id}] {message}")
    
    def complete_job(self, job_id: str, success: bool = True, error: Optional[str] = None):
        """完成任务"""
        job = self.jobs.get(job_id)
        if not job:
            return
        
        job.complete(success, error)
        self._save_state()
        self._notify_subscribers(job_id, job)
        
        logger.info(f"Job {job_id} completed with status: {job.status}")
    
    def subscribe(self, job_id: str, callback: Callable[[JobStatus], None]):
        """
        订阅任务状态更新
        
        Args:
            job_id: 任务ID
            callback: 回调函数，接收 JobStatus 参数
        """
        if job_id not in self.subscribers:
            self.subscribers[job_id] = []
        self.subscribers[job_id].append(callback)
    
    def unsubscribe(self, job_id: str, callback: Callable):
        """取消订阅"""
        if job_id in self.subscribers:
            self.subscribers[job_id] = [cb for cb in self.subscribers[job_id] if cb != callback]
    
    def _notify_subscribers(self, job_id: str, job: JobStatus):
        """通知所有订阅者"""
        callbacks = self.subscribers.get(job_id, [])
        for callback in callbacks:
            try:
                callback(job)
            except Exception as e:
                logger.error(f"Error notifying subscriber: {e}")
    
    def get_recent_jobs(self, job_type: Optional[str] = None, limit: int = 10) -> List[JobStatus]:
        """获取最近的任务列表"""
        try:
            safe_limit = max(0, int(limit))
        except (TypeError, ValueError, OverflowError):
            safe_limit = 10
        jobs = list(self.jobs.values())
        if job_type:
            jobs = [j for j in jobs if j.job_type == job_type]
        jobs.sort(key=lambda x: x.created_at, reverse=True)
        return jobs[:safe_limit]
    
    def cleanup_old_jobs(self, days: int = 7):
        """清理旧任务"""
        try:
            safe_days = max(0, int(days))
        except (TypeError, ValueError, OverflowError):
            safe_days = 7
        cutoff = time.time() - safe_days * 24 * 3600
        to_remove = [
            job_id for job_id, job in self.jobs.items()
            if (
                (created_at := _parse_timestamp(job.created_at)) is not None
                and created_at.timestamp() < cutoff
            )
        ]
        for job_id in to_remove:
            del self.jobs[job_id]
            if job_id in self.subscribers:
                del self.subscribers[job_id]
        
        if to_remove:
            self._save_state()
            logger.info(f"Cleaned up {len(to_remove)} old jobs")


# 全局任务管理器实例
job_manager = JobManager()
