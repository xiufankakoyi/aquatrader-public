"""
Celery 任务队列配置
===================

【架构说明】
本模块配置 Celery 分布式任务队列，实现回测计算的异步化。

【核心概念】
- Broker: Redis，用于接收和分发任务
- Backend: Redis，用于存储任务结果和状态
- Worker: 独立进程，执行实际计算任务
- Task: 被装饰的函数，表示一个可异步执行的任务

【启动方式】
1. 确保 Redis 运行: docker run -d -p 6379:6379 redis:alpine
2. 启动 Worker: celery -A worker worker --loglevel=info --concurrency=4
3. 启动 Flower (可选): celery -A worker flower --port=5555

【任务路由】
- backtest.*: 回测相关任务 -> backtest_queue
- optimization.*: 优化相关任务 -> optimization_queue
- data.*: 数据处理任务 -> data_queue
"""
import os
from celery import Celery
from config.config import Config
from config.logger import get_logger

logger = get_logger(__name__)

# ============================================================================
# Celery 应用实例
# ============================================================================

def create_celery_app() -> Celery:
    """
    创建并配置 Celery 应用实例
    
    Returns:
        Celery: 配置好的 Celery 应用
    """
    # 使用 Redis 作为 Broker 和 Backend
    broker_url = Config.REDIS_URL
    backend_url = Config.REDIS_URL
    
    app = Celery(
        'aquatrade',
        broker=broker_url,
        backend=backend_url,
        include=[
            'server.tasks.backtest_tasks',
        ]
    )
    
    # 序列化配置 - 使用 msgpack 获得更好性能
    app.conf.update(
        # 任务序列化
        task_serializer='msgpack',
        accept_content=['msgpack', 'json'],
        result_serializer='msgpack',
        
        # 时区配置
        timezone='Asia/Shanghai',
        enable_utc=True,
        
        # 任务结果配置
        result_expires=3600 * 24,  # 结果保留24小时
        result_backend_max_retries=10,
        result_backend_always_retry=True,
        
        # 任务执行配置
        task_track_started=True,  # 跟踪任务开始状态
        task_time_limit=3600 * 2,  # 任务硬超时2小时
        task_soft_time_limit=3600,  # 任务软超时1小时（可捕获异常）
        worker_prefetch_multiplier=1,  # 每个worker只预取1个任务，避免长任务阻塞
        
        # 任务重试配置
        task_default_retry_delay=60,  # 默认重试间隔60秒
        task_max_retries=3,  # 最大重试次数
        
        # 队列配置
        task_default_queue='default',
        task_queues={
            'default': {
                'exchange': 'default',
                'routing_key': 'default',
            },
            'backtest': {
                'exchange': 'backtest',
                'routing_key': 'backtest',
            },
            'optimization': {
                'exchange': 'optimization',
                'routing_key': 'optimization',
            },
            'data': {
                'exchange': 'data',
                'routing_key': 'data',
            },
        },
        
        # 任务路由规则
        task_routes={
            'backtest.*': {'queue': 'backtest'},
            'optimization.*': {'queue': 'optimization'},
            'data.*': {'queue': 'data'},
        },
        
        # Worker 配置
        worker_concurrency=int(os.getenv('CELERY_WORKER_CONCURRENCY', '4')),
        worker_max_tasks_per_child=1000,  # 每个worker进程处理1000个任务后重启，防止内存泄漏
        
        # 监控配置
        worker_send_task_events=True,
        task_send_sent_event=True,
        
        # Redis 连接配置
        broker_connection_retry_on_startup=True,
        broker_connection_max_retries=10,
        broker_connection_timeout=30,
        
        # 结果后端配置
        result_backend=backend_url,
        result_extended=True,  # 存储更多结果元数据
    )
    
    logger.info(f"Celery 应用已配置 | Broker: {broker_url}")
    return app


# 全局 Celery 应用实例
celery_app = create_celery_app()


# ============================================================================
# 任务状态管理
# ============================================================================

class TaskStatus:
    """任务状态常量"""
    PENDING = 'PENDING'
    STARTED = 'STARTED'
    PROGRESS = 'PROGRESS'
    SUCCESS = 'SUCCESS'
    FAILURE = 'FAILURE'
    RETRY = 'RETRY'
    REVOKED = 'REVOKED'


class TaskProgressUpdater:
    """
    任务进度更新器
    
    用于在任务执行过程中更新进度，支持：
    1. Celery 内置的 update_state
    2. Redis Pub/Sub 实时推送
    """
    
    def __init__(self, task, redis_client=None, channel=None):
        """
        初始化进度更新器
        
        Args:
            task: Celery 任务实例 (self)
            redis_client: Redis 客户端（可选，用于 Pub/Sub）
            channel: Redis 频道名（可选）
        """
        self.task = task
        self.redis = redis_client
        self.channel = channel
        self._current = 0
        self._total = 100
        
    def update(self, current: int, total: int = None, message: str = None, **kwargs):
        """
        更新任务进度
        
        Args:
            current: 当前进度值
            total: 总进度值（可选，默认100）
            message: 进度消息
            **kwargs: 额外的元数据
        """
        self._current = current
        if total is not None:
            self._total = total
            
        meta = {
            'current': current,
            'total': self._total,
            'percent': int((current / self._total) * 100) if self._total > 0 else 0,
            'message': message or '',
        }
        meta.update(kwargs)
        
        # 更新 Celery 任务状态
        self.task.update_state(
            state=TaskStatus.PROGRESS,
            meta=meta
        )
        
        # 如果配置了 Redis Pub/Sub，同时发布到频道
        if self.redis and self.channel:
            try:
                import json
                self.redis.publish(self.channel, json.dumps({
                    'task_id': self.task.request.id,
                    'type': 'progress',
                    'data': meta
                }))
            except Exception as e:
                logger.warning(f"Redis Pub/Sub 发布失败: {e}")
    
    def info(self, message: str, **kwargs):
        """发送信息性更新"""
        self.update(self._current, self._total, message, **kwargs)
    
    def success(self, result: dict = None):
        """标记任务成功完成"""
        meta = {'status': 'completed', 'result': result or {}}
        self.task.update_state(
            state=TaskStatus.SUCCESS,
            meta=meta
        )
        
    def failure(self, error: str):
        """标记任务失败"""
        self.task.update_state(
            state=TaskStatus.FAILURE,
            meta={'status': 'failed', 'error': error}
        )


def get_task_status(task_id: str) -> dict:
    """
    获取任务状态
    
    Args:
        task_id: 任务ID
        
    Returns:
        dict: 包含任务状态、结果、进度等信息的字典
    """
    from celery.result import AsyncResult
    
    result = AsyncResult(task_id, app=celery_app)
    
    response = {
        'task_id': task_id,
        'state': result.state,
        'ready': result.ready(),
        'successful': result.successful() if result.ready() else None,
    }
    
    if result.state == TaskStatus.PROGRESS:
        response['progress'] = result.info
    elif result.ready():
        if result.successful():
            response['result'] = result.result
        else:
            response['error'] = str(result.result)
    
    return response


def revoke_task(task_id: str, terminate: bool = True) -> bool:
    """
    取消任务
    
    Args:
        task_id: 任务ID
        terminate: 是否强制终止正在运行的任务
        
    Returns:
        bool: 是否成功取消
    """
    try:
        celery_app.control.revoke(task_id, terminate=terminate, signal='SIGTERM')
        logger.info(f"任务已取消: {task_id}")
        return True
    except Exception as e:
        logger.error(f"取消任务失败: {e}")
        return False
