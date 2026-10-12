"""
RedisSocketIOPublisher: 替代原生 socketio，将进度/日志发布到 Redis Channel

用于 Worker 进程与 Web Server 之间的异步通信。
Worker 进程通过 Redis Pub/Sub 发布进度消息，Web Server 订阅并转发给前端。
"""
import json
import time
import redis
import os
from typing import Dict, Any, Optional
from config.logger import get_logger


class RedisSocketIOPublisher:
    """
    Redis 发布器，用于 Worker 进程发布进度消息到 Redis Channel
    
    替代原生 socketio，因为 Worker 进程是独立进程，无法直接使用 socketio。
    """
    
    def __init__(self, redis_url: Optional[str] = None, channel_prefix: str = "aqua_notifications"):
        """
        初始化 Redis 发布器
        
        Args:
            redis_url: Redis 连接 URL，默认从环境变量读取
            channel_prefix: Channel 前缀，默认为 "aqua_notifications"
        """
        self.logger = get_logger(__name__)
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.channel_prefix = channel_prefix
        
        try:
            self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
            # 测试连接
            self.redis_client.ping()
            self.logger.info(f"RedisSocketIOPublisher 初始化成功: {self.redis_url}")
        except Exception as e:
            self.logger.error(f"RedisSocketIOPublisher 初始化失败: {e}")
            raise
    
    def emit(self, event: str, data: Dict[str, Any], to: Optional[str] = None):
        """
        发布事件到 Redis Channel
        
        Args:
            event: 事件名称（如 "optimization_progress", "optimization_evaluation"）
            data: 事件数据
            to: 目标 session ID（可选，用于定向推送）
        """
        try:
            message = {
                "event": event,
                "data": data,
                "sid": to,  # session ID，用于 Web Server 定向转发
                "timestamp": time.time()
            }
            
            # 发布到 Redis Channel
            channel = f"{self.channel_prefix}:{event}"
            self.redis_client.publish(channel, json.dumps(message))
            
        except Exception as e:
            self.logger.error(f"RedisSocketIOPublisher.emit 失败: {e}")
    
    def emit_progress(self, progress: float, message: str = "", sid: Optional[str] = None):
        """发布优化进度"""
        self.emit("optimization_progress", {
            "progress": progress,
            "message": message
        }, to=sid)
    
    def emit_evaluation(self, evaluation: Dict[str, Any], sid: Optional[str] = None):
        """发布单次评估结果"""
        self.emit("optimization_evaluation", evaluation, to=sid)
    
    def emit_error(self, error: str, sid: Optional[str] = None):
        """发布错误消息"""
        self.emit("optimization_error", {
            "message": error,
            "error": error
        }, to=sid)
    
    def emit_complete(self, result: Dict[str, Any], sid: Optional[str] = None):
        """发布完成消息"""
        self.emit("optimization_complete", result, to=sid)
    
    def close(self):
        """关闭 Redis 连接"""
        try:
            if hasattr(self, 'redis_client'):
                self.redis_client.close()
        except Exception:
            pass

