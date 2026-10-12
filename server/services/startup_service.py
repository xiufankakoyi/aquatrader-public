"""
启动服务
======

管理后端启动自检流程，通过后台线程异步执行各阶段检查，
并通过 SSE（/api/startup-logs）向前端实时推送进度。

设计原则：
- 线程安全：状态变更受锁保护
- 幂等启动：start_async() 只会启动一次检查线程
- 延迟导入：所有重型依赖在检查线程内部导入，避免循环依赖与启动阻塞
- 优雅降级：任一检查失败记录错误但不中断后续步骤（除非致命错误）
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from config.logger import get_logger

logger = get_logger(__name__)


class StartupService:
    """后端启动自检服务"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._status: Dict[str, Any] = {
            "phase": "idle",
            "step": "initializing",
            "message": "Initializing AquaTrade...",
            "progress": 0,
            "ready": False,
            "error_code": None,
            "error_message": None,
        }

    # ------------------------------------------------------------------
    # 公共状态接口
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        """获取当前启动状态（线程安全拷贝）"""
        with self._lock:
            return self._status.copy()

    def is_ready(self) -> bool:
        """启动自检是否已完成且成功"""
        with self._lock:
            return bool(self._status.get("ready"))

    def has_error(self) -> bool:
        """启动自检是否遇到错误"""
        with self._lock:
            return self._status.get("error_code") is not None

    def start_async(self) -> None:
        """异步启动自检流程，幂等调用"""
        with self._lock:
            if self._started:
                logger.debug("启动自检已在运行，忽略重复调用")
                return
            self._started = True

        logger.info("启动后端自检流程")
        self._thread = threading.Thread(target=self._run_checks, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # 内部进度更新
    # ------------------------------------------------------------------
    def _update(
        self,
        phase: str,
        step: str,
        message: str,
        progress: int,
        ready: bool = False,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """原子更新状态"""
        with self._lock:
            self._status = {
                "phase": phase,
                "step": step,
                "message": message,
                "progress": progress,
                "ready": ready,
                "error_code": error_code,
                "error_message": error_message,
            }
        logger.info(f"[启动检查] {progress}% - {message}")

    # ------------------------------------------------------------------
    # 自检流程
    # ------------------------------------------------------------------
    def _run_checks(self) -> None:
        """在后台线程中执行启动检查"""
        try:
            self._check_redis()
            self._check_database()
            self._check_data_storage()
            self._check_kernel()
            self._warmup_cache()
            self._finalize()
        except Exception as e:
            logger.error(f"启动自检未捕获异常: {e}", exc_info=True)
            self._update(
                phase="error",
                step="fatal_error",
                message=f"Startup failed: {e}",
                progress=0,
                error_code="STARTUP_FATAL",
                error_message=str(e),
            )

    def _check_redis(self) -> None:
        """检查 Redis 连接"""
        self._update(
            phase="environment",
            step="checking_redis",
            message="Connecting to Redis...",
            progress=15,
        )
        try:
            # 延迟导入避免模块加载时触发 Redis 连接
            from server.app import get_redis_client, REDIS_AVAILABLE

            if not REDIS_AVAILABLE:
                logger.warning("Redis 模块未安装，跳过 Redis 检查")
                return

            client = get_redis_client()
            if client is not None:
                client.ping()
                logger.info("Redis 连接正常")
            else:
                logger.warning("Redis 客户端初始化失败，将在无缓存模式下继续")
        except Exception as e:
            logger.warning(f"Redis 连接检查失败: {e}")

    def _check_database(self) -> None:
        """检查 SQLite 数据库与表结构"""
        self._update(
            phase="environment",
            step="checking_db",
            message="Verifying database permissions...",
            progress=30,
        )
        try:
            import os
            import sqlite3

            from config.config import Config

            db_dir = os.path.dirname(Config.DB_PATH)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)

            conn = sqlite3.connect(Config.DB_PATH)
            try:
                # 如果存在 db_utils 则自动建表，否则仅验证连接
                try:
                    from data_svc.database.db_utils import ensure_tables

                    ensure_tables(conn)
                except ImportError:
                    logger.warning("data_svc.database.db_utils 不可用，跳过自动建表")
                logger.info(f"数据库检查通过: {Config.DB_PATH}")
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"数据库检查失败: {e}", exc_info=True)
            # 数据库检查失败降级处理，不阻塞整体启动

    def _check_data_storage(self) -> None:
        """检查 LanceDB 数据存储可访问性"""
        self._update(
            phase="integrity",
            step="checking_parquet",
            message="Validating data files...",
            progress=45,
        )
        try:
            from pathlib import Path

            import lancedb

            from config.config import Config

            lancedb_path = Path(Config.LANCEDB_PATH)
            if not lancedb_path.exists():
                logger.warning(f"LanceDB 目录不存在: {lancedb_path}")
                return

            db = lancedb.connect(str(lancedb_path))
            result = db.list_tables()
            tables = result.tables if hasattr(result, "tables") else list(result)
            logger.info(f"LanceDB 可访问，共 {len(tables)} 张表")
        except Exception as e:
            logger.warning(f"LanceDB 检查失败: {e}")

    def _check_kernel(self) -> None:
        """初始化核心 API 并加载策略模块"""
        self._update(
            phase="kernel",
            step="loading_strategies",
            message="Loading strategy modules...",
            progress=60,
        )
        try:
            from server.app import get_api

            api = get_api()
            logger.info(f"VisualizationAPI 初始化完成: {type(api).__name__}")
        except Exception as e:
            logger.error(f"VisualizationAPI 初始化失败: {e}", exc_info=True)

        self._update(
            phase="kernel",
            step="loading_strategies",
            message="Loading strategy modules...",
            progress=75,
        )
        try:
            from core.quant.strategies.strategy_factory import StrategyFactory

            factory = StrategyFactory()
            factory._ensure_initialized()
            logger.info(f"策略模块加载完成，共 {len(factory.registry)} 个策略")
        except Exception as e:
            logger.error(f"策略模块加载失败: {e}", exc_info=True)

    def _warmup_cache(self) -> None:
        """预热全局数据查询缓存"""
        self._update(
            phase="kernel",
            step="warming_cache",
            message="Warming up data cache...",
            progress=90,
        )
        try:
            from server.app import get_global_data_query

            query = get_global_data_query()
            logger.info(f"全局数据查询实例预热完成: {type(query).__name__}")
        except Exception as e:
            logger.warning(f"数据缓存预热失败: {e}")

    def _finalize(self) -> None:
        """标记启动完成"""
        self._update(
            phase="ready",
            step="complete",
            message="System ready",
            progress=100,
            ready=True,
        )


# 模块级单例
_startup_service_instance: Optional[StartupService] = None
_instance_lock = threading.Lock()


def get_startup_service() -> StartupService:
    """获取 StartupService 单例"""
    global _startup_service_instance
    if _startup_service_instance is None:
        with _instance_lock:
            if _startup_service_instance is None:
                _startup_service_instance = StartupService()
    return _startup_service_instance
