import os
import re
import sys
import threading
import time
import uuid
import math
import warnings
import json
import logging

# ========== 最先加载环境变量 ==========
from dotenv import load_dotenv
load_dotenv()

# ========== 全局线程异常钩子配置 ==========
from core.error_handler import ErrorHandler, ErrorLevel
from config.logger import get_logger

def global_thread_excepthook(args):
    """
    全局后台线程异常钩子
    
    捕获所有未处理的后台线程异常，确保错误被正确记录和处理
    
    Args:
        args: (exc_type, exc_value, exc_traceback)
    """
    exc_type, exc_value, exc_traceback = args
    
    try:
        logger = get_logger(__name__)
        logger.error(
            f"[Global Thread Exception] 未捕获的后台线程异常: {exc_type.__name__}: {exc_value}",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        
        ErrorHandler.capture(
            exception=exc_value,
            level=ErrorLevel.CRITICAL,
            category="system",
            context={"thread": "background_thread"}
        )
        
    except Exception as hook_error:
        print(f"[ERROR] 全局线程异常钩子本身出错: {hook_error}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)

# 配置全局线程异常钩子（Python 3.8+）
if hasattr(threading, 'excepthook'):
    threading.excepthook = global_thread_excepthook
else:
    # 旧版本 Python 兼容性处理
    original_thread_init = threading.Thread.__init__
    
    def patched_thread_init(self, *args, **kwargs):
        original_thread_init(self, *args, **kwargs)
        original_run = self.run
        
        def run_with_error_handling():
            try:
                original_run()
            except Exception as e:
                import sys
                import traceback
                logger = get_logger(__name__)
                logger.error(
                    f"[Thread Exception] 未捕获的后台线程异常: {e}",
                    exc_info=True
                )
                ErrorHandler.capture(
                    exception=e,
                    level=ErrorLevel.CRITICAL,
                    category="system",
                    context={"thread": self.name}
                )
        
        self.run = run_with_error_handling
    
    threading.Thread.__init__ = patched_thread_init

# ========== 全局线程异常钩子配置结束 ==========
# 高性能序列化
try:
    import orjson
    ORJSON_AVAILABLE = True
except ImportError:
    ORJSON_AVAILABLE = False
    orjson = None

try:
    import msgpack
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False
    msgpack = None

from server.performance_utils import json_response, pack_backtest_data
from server.utils.system import _schedule_restart

from server.utils.binary_packer import pack_backtest_result, estimate_size
from datetime import datetime, timedelta
from threading import Event
from typing import Dict, Any, List
from urllib.parse import unquote
import pandas as pd
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

from core.profiles.profile_repository import (
    create_profile as create_strategy_profile,
    list_profiles as list_strategy_profiles,
    get_profile as get_strategy_profile,
)

# CHANGED: 延迟初始化 API，在后台线程中预热数据库
api = None

def get_api():
    """
    懒加载单例模式：获取 VisualizationAPI 实例
    只有在第一次调用时才创建对象，实现延迟初始化
    预热操作延迟到首次使用时执行，避免启动阻塞
    """
    global api
    if api is None:
        from server.visualization_api import BacktestVisualizationAPI
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.info("正在初始化 VisualizationAPI（懒加载）...")
        api = BacktestVisualizationAPI()
        api._ensure_initialized()
        logger.info("VisualizationAPI 初始化完成（预热延迟到首次查询）")
    return api

def _normalize_strategy_id(strategy_name: str) -> str:
    """
    生成 URL 安全的策略 ID（仅用于兼容旧前端/日志），保留原始策略名备用
    """
    safe = ''.join(char.lower() if char.isalnum() else '_' for char in strategy_name)
    safe = re.sub(r'_+', '_', safe).strip('_')
    return safe or strategy_name

def _init_api():
    """初始化 API 并预热数据库连接（已废弃，使用 get_api() 代替）"""
    # CHANGED: 直接调用 get_api() 实现懒加载
    get_api()

# Redis 订阅线程（用于接收 Worker 进程的进度消息）
_redis_subscriber_thread = None
_redis_subscriber_running = False

def redis_subscriber_worker():
    """Redis 订阅工作线程"""
    global _redis_subscriber_running
    from config.logger import get_logger
    logger = get_logger(__name__)
    
    try:
        redis_sub = redis.from_url(REDIS_URL, decode_responses=True)
        pubsub = redis_sub.pubsub()
        
        pubsub.psubscribe(f"{NOTIFICATION_CHANNEL_PREFIX}:*")
        
        logger.info(f"Redis 订阅线程启动，监听频道: {NOTIFICATION_CHANNEL_PREFIX}:*")
        _redis_subscriber_running = True
        
        while _redis_subscriber_running:
            try:
                message = pubsub.get_message(timeout=1.0)
                
                if message is None:
                    continue
                
                if message['type'] == 'pmessage':
                    channel = message['channel']
                    data_str = message['data']
                    
                    try:
                        data = json.loads(data_str)
                        event = data.get('event')
                        event_data = data.get('data', {})
                        sid = data.get('sid')
                        
                        if sid:
                            socketio.emit(event, event_data, to=sid)
                        else:
                            socketio.emit(event, event_data)
                            
                    except json.JSONDecodeError as e:
                        logger.warning(f"Redis 消息 JSON 解析失败: {e}")
                    except Exception as e:
                        logger.error(f"转发 Redis 消息失败: {e}", exc_info=True)
                        
            except redis.ConnectionError as e:
                logger.error(f"Redis 订阅连接错误: {e}")
                time.sleep(5)
                try:
                    redis_sub = redis.from_url(REDIS_URL, decode_responses=True)
                    pubsub = redis_sub.pubsub()
                    pubsub.psubscribe(f"{NOTIFICATION_CHANNEL_PREFIX}:*")
                except Exception:
                    logger.error("Redis 重新连接失败")
                    break
            except Exception as e:
                logger.error(f"Redis 订阅线程错误: {e}", exc_info=True)
                time.sleep(1)
        
        try:
            pubsub.close()
            redis_sub.close()
        except Exception:
            pass
        
        logger.info("Redis 订阅线程已停止")
        
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"Redis 订阅线程初始化失败: {e}", exc_info=True)

def start_redis_subscriber():
    """
    启动 Redis 订阅线程，监听 Worker 进程发布的进度消息
    并将消息转发给前端（通过 socketio）
    """
    global _redis_subscriber_thread, _redis_subscriber_running
    
    if not REDIS_AVAILABLE:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.warning("Redis 不可用，跳过订阅线程启动")
        return
    
    if _redis_subscriber_thread is not None and _redis_subscriber_thread.is_alive():
        return
    
    _redis_subscriber_thread = threading.Thread(target=redis_subscriber_worker, daemon=True)
    _redis_subscriber_thread.start()
    
    from config.logger import get_logger
    logger = get_logger(__name__)
    logger.info("Redis 订阅线程已启动")

app = Flask(__name__, static_folder='static')

# CORS 配置：覆盖所有路径，包括 Socket.IO
CORS(app, resources={
    r"/api/*": {
        "origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"],
        "expose_headers": ["Content-Type", "X-Total-Count"],
        "supports_credentials": True
    },
    r"/socket.io/*": {
        "origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"],
        "supports_credentials": True
    }
}, supports_credentials=True)
# 关键：开启 async_handlers，方便长任务
# 注意：如果使用 Granian ASGI，需要确保兼容性
# 检查是否使用 ASGI 模式
use_asgi = os.getenv("USE_GRANIAN", "false").lower() == "true"

# 根据运行模式选择正确的 async_mode
# Flask-SocketIO 在 ASGI 模式下：
# - 仍然使用 'threading' 模式初始化 SocketIO
# - run.py 中会使用 socketio.ASGIApp 来包装，它会正确处理 ASGI 请求
# - socketio.ASGIApp 会自动将 threading 模式的服务器转换为 ASGI 兼容的服务器
socketio = SocketIO(app,
                    cors_allowed_origins="*",
                    cors_credentials=True,
                    async_handlers=True,
                    async_mode='threading',
                    logger=False,
                    engineio_logger=False,
                    ping_interval=25000,
                    ping_timeout=60000,
                    compression=True)  # 启用消息压缩

# 设置全局 Socket.IO 实例，供其他模块使用
from server.socketio_manager import set_global_socketio
set_global_socketio(socketio)

active_backtests: Dict[str, Event] = {}

# GA 任务管理（已移动到 server.logic.optimization）
# 保留导入以保持向后兼容
from server.logic.optimization import ga_tasks, ga_worker

active_optimizations: Dict[str, Event] = {}  # 用于存储活跃的优化任务及其停止事件

# ========== 日志分层过滤配置 ==========
# 在 Flask 和 SocketIO 创建之后，注册路由之前配置日志级别
# 目标：过滤框架层噪音，保持业务层详细输出
# 
# 注意：Granian 的日志可能由 Rust 层控制，需要通过环境变量 GRANIAN_LOG_LEVEL=info 来控制
# 如果仍然看到大量 "Scope received" 日志，请检查环境变量设置

# 1. 屏蔽 Granian 和底层 ASGI 噪音
logging.getLogger("granian").setLevel(logging.INFO)
logging.getLogger("granian.runtime").setLevel(logging.INFO)
logging.getLogger("granian.log").setLevel(logging.INFO)

# 2. 屏蔽 Socket.IO 和 Engine.IO 的详细日志
logging.getLogger("socketio").setLevel(logging.WARNING)
logging.getLogger("engineio").setLevel(logging.WARNING)
logging.getLogger("engineio.server").setLevel(logging.WARNING)
logging.getLogger("engineio.client").setLevel(logging.WARNING)

# 3. 屏蔽 ASGI 相关库的详细日志
logging.getLogger("asgiref").setLevel(logging.WARNING)
logging.getLogger("asgiref.wsgi").setLevel(logging.WARNING)

# 4. 屏蔽 Flask 的详细日志（可选，根据需要调整）
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# 5. 保持业务逻辑的详细输出
# server 模块：DEBUG 级别（可以看到路由注册、回测启动等）
logging.getLogger("server").setLevel(logging.DEBUG)
# core.backtest 模块：DEBUG 级别（可以看到回测详情、性能监控等）
logging.getLogger("core.quant.backtest").setLevel(logging.DEBUG)
# data_svc 模块：DEBUG 级别（可以看到数据查询详情）
logging.getLogger("data_svc").setLevel(logging.DEBUG)
# tools 模块：DEBUG 级别（可以看到策略执行详情）
logging.getLogger("tools").setLevel(logging.DEBUG)
# config 模块：DEBUG 级别（可以看到配置加载详情）
logging.getLogger("config").setLevel(logging.DEBUG)

# 6. 其他第三方库：设置为 INFO 或 WARNING
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)  # Pillow 图像库
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("numba").setLevel(logging.WARNING)  # Numba JIT 编译日志

# ======================================

# ============================================================================
# Celery 集成
# ============================================================================

# 导入 Celery 应用实例
try:
    from config.celery_config import celery_app
    CELERY_AVAILABLE = True
    print("[INFO] Celery 集成已启用")
except Exception as e:
    CELERY_AVAILABLE = False
    print(f"[WARNING] Celery 集成不可用: {e}")

# 导入任务模块（确保 Celery 能发现任务）
if CELERY_AVAILABLE:
    try:
        from server.tasks import backtest_tasks
        print("[INFO] Celery 任务模块已加载")
    except Exception as e:
        print(f"[WARNING] 加载 Celery 任务模块失败: {e}")

# ============================================================================
# 注册路由和 Socket.IO 处理器
# ============================================================================

# 注意：必须在 socketio 和 app 创建之后调用，延迟导入避免循环依赖
# 关键：分离错误处理，确保路由注册和 Socket.IO 注册独立，互不影响

# 1. 注册路由（独立错误处理）
from config.logger import get_logger
logger = get_logger(__name__)

try:
    from server.routes import register_routes
    def init_db_async():
        try:
            from config.config import Config
            from data_svc.database.db_utils import ensure_tables
            import sqlite3
            import os
            
            db_dir = os.path.dirname(Config.DB_PATH)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
                logger.info(f"创建数据库目录: {db_dir}")
            
            conn = sqlite3.connect(Config.DB_PATH)
            ensure_tables(conn)
            conn.close()
            logger.info(f"数据库异步初始化成功: {Config.DB_PATH}")
        except Exception as db_err:
            logger.error(f"数据库异步初始化失败: {db_err}")

    import threading
    threading.Thread(target=init_db_async, daemon=True).start()
        
    register_routes(app)
    logger.info("路由注册成功")
except Exception as e:
    logger.error(f"注册路由失败: {e}", exc_info=True)
    # 路由注册失败是严重错误，但不阻止服务器启动

# 2. 注册 Socket.IO 处理器（独立错误处理）
try:
    from server.socketio_handlers import register_socketio_handlers
    register_socketio_handlers(socketio)
    logger.info("Socket.IO 处理器注册成功")
except Exception as e:
    logger.error(f"注册 Socket.IO 处理器失败: {e}", exc_info=True)
    # Socket.IO 注册失败不影响 HTTP 路由的正常工作

# 【健壮性加固】全局数据查询实例（统一数据访问入口）
_global_data_query = None

def get_global_data_query():
    """获取全局数据查询实例（单例模式）"""
    global _global_data_query
    if _global_data_query is None:
        from data_svc.database.optimized_data_query import OptimizedStockDataQuery
        _global_data_query = OptimizedStockDataQuery(warmup=True)
    return _global_data_query

# Redis 配置
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TASK_QUEUE = "aqua_tasks"  # Redis List 名称
NOTIFICATION_CHANNEL_PREFIX = "aqua_notifications"

# Redis 客户端（用于推送任务）
_redis_client = None

def get_redis_client():
    """获取 Redis 客户端（单例模式）"""
    global _redis_client
    if _redis_client is None and REDIS_AVAILABLE:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
            from config.logger import get_logger
            logger = get_logger(__name__)
            logger.info(f"Redis 客户端初始化成功: {REDIS_URL}")
        except Exception as e:
            from config.logger import get_logger
            logger = get_logger(__name__)
            logger.error(f"Redis 客户端初始化失败: {e}")
    return _redis_client

# 【健壮性加固】全局错误处理器
@app.errorhandler(Exception)
def handle_exception(e):
    """捕获所有未处理的异常，返回标准 JSON 格式"""
    from config.logger import get_logger
    logger = get_logger(__name__)
    
    # CHANGED: 特别处理连接中止错误，这是常见的网络问题，不应该被视为严重错误
    if isinstance(e, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
        # 客户端在服务器发送响应之前关闭了连接，这是正常的网络行为
        # 只记录警告，不记录错误堆栈
        logger.debug(f"客户端连接已关闭: {type(e).__name__} - {str(e)}")
        # 返回 None 让 Flask 正常处理（实际上连接已关闭，无法返回响应）
        return None
    
    # CHANGED: 特别处理 404 错误，这是正常的 HTTP 状态码，不应该被视为错误
    from werkzeug.exceptions import NotFound, MethodNotAllowed
    if isinstance(e, NotFound):
        # 404 是正常的 HTTP 状态码，只记录 DEBUG 级别
        logger.debug(f"404 Not Found: {request.path}")
        return jsonify({
            'success': False,
            'error': '资源未找到',
            'data': []
        }), 404
    
    if isinstance(e, MethodNotAllowed):
        # 405 也是正常的 HTTP 状态码
        logger.debug(f"405 Method Not Allowed: {request.method} {request.path}")
        return jsonify({
            'success': False,
            'error': '请求方法不允许',
            'data': []
        }), 405
    
    # 记录错误堆栈
    import traceback
    error_trace = traceback.format_exc()
    logger.error(f"未处理的异常: {e}\n{error_trace}")
    
    # 返回标准 JSON 格式，确保前端能优雅降级
    return jsonify({
        'success': False,
        'error': str(e),
        'data': []
    }), 500

@app.errorhandler(500)
def handle_500(e):
    """专门处理 500 错误"""
    from config.logger import get_logger
    logger = get_logger(__name__)
    
    # CHANGED: 特别处理连接中止错误
    if isinstance(e, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
        logger.debug(f"客户端连接已关闭 (500): {type(e).__name__} - {str(e)}")
        return None
    
    import traceback
    error_trace = traceback.format_exc()
    logger.error(f"500 错误: {e}\n{error_trace}")
    
    return jsonify({
        'success': False,
        'error': str(e),
        'traceback': error_trace,
        'data': []
    }), 500

# CHANGED: 添加 after_request 钩子，捕获响应写入时的连接错误
@app.after_request
def after_request_handler(response):
    """在响应发送后处理，捕获连接错误"""
    try:
        return response
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
        # 客户端在响应发送过程中关闭了连接，这是正常的网络行为
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.debug(f"响应发送时客户端连接已关闭: {type(e).__name__} - {str(e)}")
        # 返回原始响应，让 Flask 正常处理
        return response
    except Exception as e:
        # 其他异常正常处理
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.warning(f"after_request 钩子异常: {e}")
        return response

def _ensure_api_initialized() -> None:
    """确保 API 已初始化（兼容性函数）"""
    get_api()
    
    # 启动启动服务检查（包含自动数据更新）
    try:
        from server.services.startup_service import get_startup_service
        startup_service = get_startup_service()
        startup_service.start_async()
    except Exception as e:
        logger = get_logger(__name__)
        logger.warning(f"启动服务初始化失败: {e}")

    try:
        from server.industry_chain.auto_update_scheduler import get_industry_auto_update_scheduler

        get_industry_auto_update_scheduler().start()
    except Exception as e:
        logger = get_logger(__name__)
        logger.warning(f"IndustryChainRadar auto update scheduler startup failed: {e}")

def _sanitize_json_data(data):
    """
    【健壮性加固】清洗数据，将 NaN/Infinity 转换为 null，防止 JSON 序列化报错
    """
    import math
    import numpy as np
    
    if isinstance(data, dict):
        return {k: _sanitize_json_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_sanitize_json_data(item) for item in data]
    elif isinstance(data, (float, np.floating)):
        if math.isnan(data) or math.isinf(data):
            return None
        return float(data)
    elif isinstance(data, (int, np.integer)):
        return int(data)
    elif pd.isna(data):
        return None
    else:
        return data


# ---------------- REST API ---------------- #
# 注意：大部分路由已迁移到 server/routes/ 目录下的独立文件
# 以下路由保留在 app.py 中，因为它们需要直接访问 app 实例或特殊处理

# 以下路由已迁移到 server/routes/ 目录：
# - /api/run_backtest -> server/routes/backtest_routes.py
# - /api/direct_test -> server/routes/system_routes.py
# - /api/restart-backend -> server/routes/system_routes.py
# - /api/kline -> server/routes/data_routes.py
# - /api/latest_price -> server/routes/data_routes.py
# - /api/stock_sentiment -> server/routes/sentiment_routes.py
# - /api/stock_sentiment_words -> server/routes/sentiment_routes.py
# - /api/stock_sentiment_timeline -> server/routes/sentiment_routes.py
# - /api/sentiment_trends -> server/routes/sentiment_routes.py
# - /api/lda_topics -> server/routes/sentiment_routes.py
# - /api/scatter_data -> server/routes/scatter_routes.py
# - /api/ga_optimize/start -> server/routes/optimization_routes.py
# - /api/ga_optimize/status/<task_id> -> server/routes/optimization_routes.py
# - /api/strategies -> server/routes/strategy_routes.py
# - /api/test_strategies -> server/routes/strategy_routes.py
# - /api/strategies/<strategy_id>/params -> server/routes/strategy_routes.py
# - /api/strategies/<strategy_name>/profiles -> server/routes/strategy_routes.py
# - /api/strategy-profiles/<int:profile_id> -> server/routes/strategy_routes.py

# 以下路由尚未迁移，保留在 app.py 中

@app.route('/api/strategy/<version_id>', methods=['GET'])
def get_strategy_detail(version_id):
    """
    获取策略详情（包括回测结果和交易记录）
    从数据库中查询该策略的最新回测结果和对应的交易记录
    """
    try:
        import sqlite3
        import json
        
        # 数据库连接 - 使用配置中心的路径
        from config.config import Config
        db_path = Config.DB_PATH
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 添加调试信息
        print(f"[DEBUG] 查询策略: {version_id}")
        
        # 查询该策略的最新回测结果
        cursor.execute('''
            SELECT * FROM backtest_results 
            WHERE strategy_name = ? 
            ORDER BY created_at DESC 
            LIMIT 1
        ''', (version_id,))
        
        backtest_result = cursor.fetchone()
        
        if backtest_result:
            print(f"[DEBUG] 找到回测结果: ID={backtest_result['id']}, 交易次数={backtest_result['trade_count']}")
            
            # 查询对应的交易记录
            cursor.execute('''
                SELECT COUNT(*) FROM trade_records 
                WHERE backtest_id = ?
            ''', (backtest_result['id'],))
            
            trade_count = cursor.fetchone()[0]
            print(f"[DEBUG] 交易记录数量: {trade_count}")
            
            # 查询详细交易记录
            cursor.execute('''
                SELECT * FROM trade_records 
                WHERE backtest_id = ? 
                ORDER BY date ASC
            ''', (backtest_result['id'],))
            
            trade_records = cursor.fetchall()
            
            # 格式化交易记录
            trades = []
            for record in trade_records:
                trades.append({
                    "id": record['id'],
                    "date": record['date'],
                    "symbol": record['stock_code'],
                    "symbolCode": record['stock_code'],
                    "name": record['stock_name'],
                    "action": record['action'],
                    "price": record['price'],
                    "quantity": record['shares'],
                    "amount": record['amount'],
                    "commission": 0,  # 数据库中没有该字段，默认为0
                    "profitLoss": record['profit_loss'],
                    "roi": record['profit_loss'] / record['amount'] if record['amount'] != 0 else 0,
                    "cumulativePnL": record['profit_loss'],  # 简单处理，实际应该累积计算
                    "entryDate": record['date'] if record['action'] == 'buy' else None,
                    "exitDate": record['date'] if record['action'] == 'sell' else None
                })
            
            # 累积计算盈亏
            cumulative_pnl = 0
            for trade in trades:
                cumulative_pnl += trade['profitLoss']
                trade['cumulativePnL'] = cumulative_pnl
                
            print(f"[DEBUG] 格式化后的交易记录数量: {len(trades)}")
            
            # 构造返回数据
            return jsonify({
                "versionId": version_id,
                "metrics": {
                    "totalReturn": backtest_result['total_return'],
                    "annualizedReturn": backtest_result['annual_return'],
                    "maxDrawdown": backtest_result['max_drawdown'],
                    "sharpeRatio": backtest_result['sharpe_ratio'],
                    "winRate": backtest_result['win_rate'],
                    "tradesCount": backtest_result['trade_count'],
                    "profitFactor": backtest_result['profit_factor'],
                    "volatility": 0,  # 数据库中没有该字段，默认为0
                    "sortinoRatio": backtest_result['sortino_ratio'],
                    "avgTradeReturn": sum(trade['profitLoss'] for trade in trades if trade['action'] == 'sell') / backtest_result['trade_count'] if backtest_result['trade_count'] != 0 else 0,
                    "maxWinningStreak": 0,  # 数据库中没有该字段，默认为0
                    "maxLosingStreak": 0  # 数据库中没有该字段，默认为0
                },
                "equityCurve": [],  # 暂时不返回 equity curve
                "monthlyReturns": [],  # 暂时不返回 monthly returns
                "trades": trades
            })
        else:
            # 如果没有回测结果，返回空数据结构
            return jsonify({
                "versionId": version_id,
                "metrics": {
                    "totalReturn": 0,
                    "annualizedReturn": 0,
                    "maxDrawdown": 0,
                    "sharpeRatio": 0,
                    "winRate": 0,
                    "tradesCount": 0,
                    "profitFactor": 0,
                    "volatility": 0,
                    "sortinoRatio": 0,
                    "avgTradeReturn": 0,
                    "maxWinningStreak": 0,
                    "maxLosingStreak": 0
                },
                "equityCurve": [],
                "monthlyReturns": [],
                "trades": []
            })
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            conn.close()
        except:
            pass

# ---------------- Socket.IO 连接事件 ---------------- #

@socketio.on('connect')
def handle_connect():
    """【健壮性加固】SocketIO 连接事件，添加异常捕获"""
    try:
        # 只在 DEBUG 模式下输出连接信息
        debug_mode = os.getenv('LOG_LEVEL', '').upper() == 'DEBUG'
        if debug_mode:
            from config.logger import get_logger
            logger = get_logger(__name__)
            logger.debug(f"Socket.IO 客户端已连接: {request.sid}")
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"Socket.IO 连接处理失败: {e}")

@socketio.on('disconnect')
def handle_disconnect():
    """【健壮性加固】SocketIO 断开事件，添加异常捕获"""
    try:
        # 只在 DEBUG 模式下输出断开信息
        debug_mode = os.getenv('LOG_LEVEL', '').upper() == 'DEBUG'
        if debug_mode:
            from config.logger import get_logger
            logger = get_logger(__name__)
            logger.debug(f"Socket.IO 客户端已断开: {request.sid}")
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"Socket.IO 断开处理失败: {e}")

# ---------------- 前端触发回测的事件处理handler ---------------- #

@socketio.on('run_streaming_backtest')
def handle_streaming_backtest(data):
    """
    这里只负责解析前端传来的参数，然后启动后台任务
    """
    try:
        strategy_name = data.get('strategy_name')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        benchmark_code = data.get('benchmark_code')
        profile_id = data.get('profile_id')
        override_params = data.get('override_params') or {}

        if not all([strategy_name, start_date, end_date]):
            emit('backtest_error', {"message": "缺少必要参数"})
            return

        # 如果带 profile_id，则在这里加载并合并参数，传入 API
        effective_params = None
        if profile_id is not None:
            try:
                from core.profiles.profile_repository import get_profile as load_profile

                profile = load_profile(int(profile_id))
                if profile is None:
                    emit('backtest_error', {"message": f"Profile {profile_id} 不存在"})
                    return
                params_from_profile = profile.get("params") or {}
                if not isinstance(params_from_profile, dict):
                    params_from_profile = {}
                if not isinstance(override_params, dict):
                    override_params = {}
                effective_params = {**params_from_profile, **override_params}
            except Exception as e:
                emit('backtest_error', {"message": f"加载 Profile 失败: {e}"})
                return

        print(f"📨 收到流式回测请求: {strategy_name} | {start_date}~{end_date} | 基准: {benchmark_code or 'None'}")

        # 当前这位前端用户�?sid
        sid = request.sid

        # 给前端一个“我收到了”的反馈（你 App.vue 有监听）
        emit('request_received', {"message": "回测请求已收到"})

        # 启动后台任务，不阻塞当前事件 handler
        stop_event = Event()
        active_backtests[sid] = stop_event

        # 使用逻辑层的回测函数
        from server.logic.backtest import run_backtest_background
        socketio.start_background_task(
            run_backtest_background,
            socketio,  # socketio_instance
            sid,  # sid
            strategy_name,  # strategy_name
            start_date,  # start_date
            end_date,  # end_date
            benchmark_code,  # benchmark_code
            stop_event,  # stop_event
            effective_params,  # params
            None,  # backtest_config
            get_api,  # get_api_func
            active_backtests  # active_backtests_dict
        )

    except Exception as e:
        print("❌ 流式回测启动失败:", e)
        emit('backtest_error', {"message": str(e)})

# CHANGED: 将取消回测的处理函数移到模块级别，避免嵌套定义问题
@socketio.on('cancel_streaming_backtest')
def handle_cancel_backtest(data=None):
    """
    处理取消回测请求
    CHANGED: 接受 data 参数（即使不使用），避免 Socket.IO 参数不匹配错误
    """
    from config.logger import get_logger
    logger = get_logger(__name__)
    sid = request.sid
    stop_event = active_backtests.get(sid)
    if stop_event:
        stop_event.set()
        emit('backtest_cancel_ack', {"message": "正在停止回测..."})
        logger.info(f"收到取消回测请求，已设置停止标志 (sid: {sid})")
    else:
        emit('backtest_cancel_ack', {"message": "当前没有正在运行的回测"})
        logger.warning(f"收到取消回测请求，但当前没有运行的回测 (sid: {sid})")

@socketio.on('request_kline')
def handle_request_kline(data):
    symbol_code = data.get('symbol_code')
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    if not symbol_code:
        emit('kline_data', {"error": "缺少 symbol_code"}, to=request.sid)
        return

    print(f"📈 请求 K 线 {symbol_code} | {start_date} ~ {end_date}")
    api_instance = get_api()
    history = api_instance.get_symbol_kline(symbol_code, start_date, end_date)
    emit('kline_data', {
        "request_id": data.get('request_id'),
        "symbol_code": symbol_code,
        "symbolCode": symbol_code,
        "symbol_name": api_instance.stock_info_map.get(symbol_code, symbol_code),
        "data": history
    }, to=request.sid)

# ---------------- 参数优化相关事件处理 ---------------- #

def push_optimization_task_to_redis(sid, config):
    """
    将优化任务推送到 Redis 队列
    
    Args:
        sid: Socket.IO session ID
        config: 优化配置
    """
    from config.logger import get_logger
    logger = get_logger(__name__)
    
    redis_client = get_redis_client()
    if not redis_client:
        logger.error("Redis 不可用，无法推送任务")
        return False
    
    try:
        task_data = {
            "sid": sid,
            "config": config
        }
        
        # 推送到 Redis List
        redis_client.rpush(TASK_QUEUE, json.dumps(task_data))
        logger.info(f"优化任务已推送到 Redis 队列 (sid: {sid})")
        return True
    except Exception as e:
        logger.error(f"推送任务到 Redis 失败: {e}", exc_info=True)
        return False

@socketio.on('stop_optimization')
def handle_stop_optimization(data):
    """处理停止优化请求"""
    from config.logger import get_logger
    logger = get_logger(__name__)
    request_sid = request.sid
    # 优先使用前端显式传来的 sid（表示真正运行优化的会话）
    target_sid = None
    try:
        if isinstance(data, dict):
            target_sid = data.get("sid") or data.get("session_id")
    except Exception:
        target_sid = None
    if not target_sid:
        target_sid = request_sid

    if target_sid in active_optimizations:
        # 设置停止事件
        active_optimizations[target_sid].set()
        emit('optimization_cancel_ack', {"message": "正在停止优化..."})
        logger.info(f"收到取消优化请求，已设置停止标志 (sid: {target_sid})")
    else:
        emit('optimization_cancel_ack', {"message": "当前没有正在运行的优化"})
        logger.warning(f"收到取消优化请求，但当前没有运行的优化 (请求来自 sid: {request_sid}, 目标 sid: {target_sid})")

@socketio.on('run_optimization')
def handle_run_optimization(data):
    """处理参数优化请求（支持新旧两种格式）"""
    try:
        sid = request.sid
        
        # 检查是否是新格式（包含 optimization_engine 和 search_space）
        if 'optimization_engine' in data and 'search_space' in data:
            # 新格式处理
            strategy_name = data.get('strategy_name')
            optimization_engine = data.get('optimization_engine', {})
            search_space = data.get('search_space', [])
            objective = data.get('objective', {})
            backtest = data.get('backtest', {})
            validation = data.get('validation', {})

            # 全局优化模式（quick_explore / robust / aggressive）
            mode = data.get('mode') or validation.get('mode') or 'robust'
            
            method = optimization_engine.get('method', 'ga')
            # 统一方法名称（前端可能发送 cmaes，后端期望 cma_es）
            if method == 'cmaes':
                method = 'cma_es'
            algo_params = optimization_engine.get('params', {})
            
            start_date = backtest.get('start_date') or data.get('start_date')
            end_date = backtest.get('end_date') or data.get('end_date')
            
            if not all([strategy_name, start_date, end_date]):
                emit('optimization_error', {"message": "缺少必要参数: strategy_name, start_date, end_date"})
                return
            
            print(f"🔍 收到参数优化请求（新格式）: {strategy_name} | {start_date}~{end_date}")
            print(f"   算法: {method} | 参数: {algo_params}")
            print(f"   搜索空间: {len(search_space)} 个参数")
            
            # 构建 param_ranges（从 search_space 转换）
            param_ranges = []
            for param_spec in search_space:
                param_name = param_spec.get('name')
                param_type = param_spec.get('type', 'float')
                param_min = param_spec.get('min')
                param_max = param_spec.get('max')
                
                if not all([param_name, param_min is not None, param_max is not None]):
                    logger.warning(f"跳过无效参数: {param_spec}")
                    continue
                
                lower, upper = float(param_min), float(param_max)
                
                # 验证参数范围
                if lower >= upper:
                    logger.warning(f"参数范围验证失败: {param_name} 范围 [{lower}, {upper}] 无效，将使用安全范围")
                    lower, upper = min(lower, upper - 1), max(upper, lower + 1)
                
                param_range = {
                    "name": param_name,
                    "bounds": [lower, upper],
                    "type": param_type
                }
                
                # 网格搜索需要步长
                if method == 'grid' and 'step' in param_spec:
                    param_range['step'] = float(param_spec['step'])
                
                param_ranges.append(param_range)
                print(f"📊 参数范围设置: {param_name} = [{lower}, {upper}] ({param_type})")
            
            if not param_ranges:
                emit('optimization_error', {"message": "没有有效的参数范围定义"})
                return
            
            # 构建优化配置（转换为后端期望的格式）
            target_metric = objective.get('target', 'sharpe_ratio')
            # 转换指标名称
            metric_map = {
                'sharpeRatio': 'sharpe',
                'totalReturn': 'returns',
                'maxDrawdown': 'sharpe',  # 回撤需要特殊处理
                'calmarRatio': 'calmar',
                'sortinoRatio': 'sortino'
            }
            target = metric_map.get(target_metric, target_metric.replace('_ratio', '').lower())
            
            # 根据算法类型构建 options
            options = {
                "target": target,
                "target_metric": target_metric,
                "mode": mode,
            }
            
            # 根据算法类型添加特定参数
            if method == 'ga':
                options.update({
                    "iterations": algo_params.get('generations', 100),
                    "generations": algo_params.get('generations', 100),
                    "population": algo_params.get('pop_size', 50),
                    "mutation_rate": algo_params.get('mutation_rate', 0.1),
                    "crossover_rate": algo_params.get('crossover_rate', 0.8),
                })
            elif method == 'pso':
                options.update({
                    "iterations": algo_params.get('iterations', 100),
                    "swarm_size": algo_params.get('particle_count', 30),
                    "w": algo_params.get('w', 0.7),
                    "c1": algo_params.get('c1', 1.5),
                    "c2": algo_params.get('c2', 1.5),
                })
            elif method == 'cma_es':
                options.update({
                    "iterations": algo_params.get('max_evaluations', 100),
                    "population": algo_params.get('population', 20),
                    "sigma": algo_params.get('sigma', 0.5),
                })
            elif method == 'cmaes':  # 兼容前端可能发送的 cmaes 格式
                options.update({
                    "iterations": algo_params.get('max_evaluations', 100),
                    "population": algo_params.get('population', 20),
                    "sigma": algo_params.get('sigma', 0.5),
                })
            elif method == 'sa':
                options.update({
                    "iterations": 1000,  # 模拟退火的迭代次数由温度控制
                    "initial_temp": algo_params.get('initial_temp', 100.0),
                    "min_temp": algo_params.get('min_temp', 0.01),
                    "cooling_rate": algo_params.get('cooling_rate', 0.95),
                    "steps_per_temp": algo_params.get('steps_per_temp', 10),
                })
            elif method == 'bayesian':
                options.update({
                    "iterations": algo_params.get('iterations', 50),
                    "init_points": algo_params.get('random_init_points', 10),
                    "acquisition_function": algo_params.get('acquisition_function', 'EI'),
                })
            elif method == 'grid':
                options.update({
                    "iterations": 1000,  # 网格搜索的迭代次数由参数组合数决定
                    "grid_density": algo_params.get('grid_density', 10),
                    "parallel": algo_params.get('parallel', True),
                })
            
            # 添加约束条件
            constraints = objective.get('constraints', {})
            if constraints:
                options['constraints'] = constraints
            
            # 添加验证框架配置
            if validation.get('enabled'):
                options['validation'] = validation

            # 三段区间：前端通过 backtest 传入，如果缺失则退回主回测区间
            train_start_date = backtest.get('train_start_date') or start_date
            train_end_date = backtest.get('train_end_date') or end_date
            val_start_date = backtest.get('val_start_date') or None
            val_end_date = backtest.get('val_end_date') or None
            test_start_date = backtest.get('test_start_date') or None
            test_end_date = backtest.get('test_end_date') or None

            print("📌 参数优化区间:")
            print(f"   train: {train_start_date} ~ {train_end_date}")
            print(f"   val  : {val_start_date} ~ {val_end_date}")
            print(f"   test : {test_start_date} ~ {test_end_date}")
            
            optimization_config = {
                "strategy_name": strategy_name,
                "start_date": start_date,
                "end_date": end_date,
                "method": method,
                "param_ranges": param_ranges,
                "options": options,
                "selected_params": [p['name'] for p in param_ranges],
                "mode": mode,
                "train_start_date": train_start_date,
                "train_end_date": train_end_date,
                "val_start_date": val_start_date,
                "val_end_date": val_end_date,
                "test_start_date": test_start_date,
                "test_end_date": test_end_date,
            }
            
        else:
            # 旧格式处理（向后兼容）
            strategy_name = data.get('strategy_name')
            start_date = data.get('start_date')
            end_date = data.get('end_date')
            config_data = data.get('config', {})
            selected_params = data.get('selected_params', {})
            
            if not all([strategy_name, start_date, end_date]):
                emit('optimization_error', {"message": "缺少必要参数: strategy_name, start_date, end_date"})
                return
            
            from config.logger import get_logger
            logger = get_logger(__name__)
            debug_mode = os.getenv('LOG_LEVEL', '').upper() == 'DEBUG'
            if debug_mode:
                logger.debug(f"收到参数优化请求（旧格式）: {strategy_name} | {start_date}~{end_date}")
                logger.debug(f"   算法: {config_data.get('method', 'genetic')} | 迭代: {config_data.get('iterations', 50)}")
                logger.debug(f"   选中参数: {list(selected_params.keys())}")
            
            # 构建 param_ranges
            param_ranges = []
            for param_name, bounds in selected_params.items():
                if isinstance(bounds, list) and len(bounds) == 2:
                    lower, upper = float(bounds[0]), float(bounds[1])
                    
                    # 验证参数范围
                    if lower >= upper:
                        logger.warning(f"参数范围验证失败: {param_name} 范围 [{lower}, {upper}] 无效，将使用安全范围")
                        if lower > 1000:
                            lower, upper = 0, lower + 100
                        else:
                            lower, upper = min(lower, upper - 1), max(upper, lower + 1)
                    
                    # 判断参数类型
                    is_int_param = (
                        'days' in param_name or 
                        'candidates' in param_name or 
                        'stocks' in param_name or
                        (param_name.endswith('_ma') and 'ratio' not in param_name and 'threshold' not in param_name) or
                        param_name in ['market_cap_min', 'market_cap_max']
                    )
                    param_type = 'int' if is_int_param else 'float'
                    
                    param_ranges.append({
                        "name": param_name,
                        "bounds": [lower, upper],
                        "type": param_type
                    })
                    print(f"📊 参数范围设置: {param_name} = [{lower}, {upper}] ({param_type})")
            
            if not param_ranges:
                emit('optimization_error', {"message": "没有有效的参数范围定义"})
                return
            
            # 构建优化配置
            optimization_config = {
                "strategy_name": strategy_name,
                "start_date": start_date,
                "end_date": end_date,
                "method": config_data.get('method', 'genetic'),
                "param_ranges": param_ranges,
                "options": {
                    "target": config_data.get('target', 'sharpe'),
                    "iterations": config_data.get('iterations', 50),
                    "generations": config_data.get('iterations', 50),
                    "population": config_data.get('population', 20),
                    "mutation_rate": config_data.get('mutationRate', 0.1),
                    "crossover_rate": config_data.get('crossoverRate', 0.7),
                    "init_points": config_data.get('init_points', 5),
                },
                "selected_params": list(selected_params.keys()) if selected_params else None,
            }
        
        # 推送任务到 Redis 队列（异步架构）
        success = push_optimization_task_to_redis(sid, optimization_config)
        
        if success:
            emit('optimization_request_received', {"message": "优化请求已收到，已加入队列，等待 Worker 处理..."})
        else:
            emit('optimization_error', {"message": "Redis 不可用，无法启动优化任务"})
        
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"参数优化启动失败: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        emit('optimization_error', {"message": str(e)})

if __name__ == '__main__':
    print('🚀 启动量化回测数据可视化API服务...')
    # 在启动服务前预先初始化 API（替代 @app.before_first_request）
    _ensure_api_initialized()
    # 启动 Redis 订阅线程（用于接收 Worker 进程的进度消息）
    start_redis_subscriber()
    # 必须使用 socketio.run 才能启用 Socket.IO 事件
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=False, allow_unsafe_werkzeug=True)
