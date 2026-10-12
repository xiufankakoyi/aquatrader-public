"""
回测相关路由
============

【架构说明】本模块使用两层架构：
- 存储层 (LanceDB): 极速写入，高效压缩，版本管理
- 分析层 (Polars): 复杂表达式计算，向量化执行

【两层架构优势】
- 存储层 (LanceDB): 极速写入，高效压缩，版本管理
- 分析层 (Polars): 复杂表达式计算，向量化执行

【使用建议】
- 新策略回测建议使用两层架构，性能更优
"""
from flask import Blueprint, request, Response
from server.performance_utils import json_response
import pandas as pd
import io
import json
import os
import threading
from typing import Any, Dict

backtest_bp = Blueprint('backtest', __name__, url_prefix='/api')


# ============================================================================
# 两层架构支持函数
# ============================================================================

def is_lancedb_backend():
    """检查是否使用 LanceDB 后端"""
    return os.getenv("DB_BACKEND", "lancedb").lower() == "lancedb"


def get_unified_data_interface():
    """获取统一数据接口实例"""
    try:
        from data_svc.unified_data_interface import get_unified_data_interface
        return get_unified_data_interface()
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.warning(f"数据接口初始化失败: {e}")
        return None


# ============================================================================
# 预加载回测数据
# ============================================================================

@backtest_bp.route('/preload', methods=['POST'])
def preload_backtest():
    """
    预加载回测数据
    
    当用户 hover "运行回测" 按钮时触发
    静默预加载策略代码和数据缓存，加速实际回测
    
    【架构支持】
    - LanceDB: 预加载数据到内存
    
    请求体:
        {
            "strategy_name": "策略名称",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31"
        }
    
    返回:
        {
            "success": true,
            "task_id": "abc123",
            "status": "loading" | "completed" | "error"
        }
    """
    from server.services.preload_service import get_preload_service
    from config.logger import get_logger
    
    logger = get_logger(__name__)
    
    try:
        data = request.get_json() or {}
        strategy_name = data.get('strategy_name')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if not all([strategy_name, start_date, end_date]):
            return json_response({
                "success": False,
                "error": "缺少必要参数: strategy_name, start_date, end_date"
            }, status_code=400)
        
        # 如果使用 LanceDB，预加载数据到内存
        if is_lancedb_backend():
            logger.debug(f"[LanceDB] 预加载回测数据: {strategy_name}")
            interface = get_unified_data_interface()
            if interface:
                pass
        
        preload_service = get_preload_service()
        task = preload_service.preload_strategy(strategy_name, start_date, end_date)
        
        logger.debug(f"[Preload] 任务创建: {task.task_id} - {task.status}")
        
        return json_response({
            "success": True,
            "task_id": task.task_id,
            "status": task.status,
            "strategy_name": strategy_name,
            "start_date": start_date,
            "end_date": end_date
        })
        
    except Exception as e:
        logger = get_logger(__name__)
        logger.error(f"预加载失败: {e}", exc_info=True)
        return json_response({"success": False, "error": str(e)}, status_code=500)


@backtest_bp.route('/preload/status/<task_id>', methods=['GET'])
def get_preload_status(task_id: str):
    """
    获取预加载任务状态
    
    Args:
        task_id: 预加载任务 ID
        
    Returns:
        {
            "success": true,
            "task_id": "abc123",
            "status": "completed",
            "cache_key": "preload:..."
        }
    """
    from server.services.preload_service import get_preload_service
    
    preload_service = get_preload_service()
    task = preload_service.get_task_status(task_id)
    
    if task is None:
        return json_response({
            "success": False,
            "error": "任务不存在"
        }, status_code=404)
    
    return json_response({
        "success": True,
        "task_id": task.task_id,
        "status": task.status,
        "strategy_name": task.strategy_name,
        "start_date": task.start_date,
        "end_date": task.end_date,
        "error": task.error,
        "cache_key": task.cache_key
    })


# ============================================================================
# 运行回测（同步 - 保持兼容）
# ============================================================================

@backtest_bp.route('/run_backtest', methods=['POST'])
def run_backtest():
    """
    运行回测（非流式接口 - 同步版本，保持兼容）
    
    【注意】此接口为同步调用，长时间回测会阻塞请求。
    建议使用新的异步接口 /run_backtest_async
    
    【架构支持】
    - LanceDB 两层架构：使用 Polars 进行向量化回测计算
    - 传统后端：使用原有回测引擎
    
    请求体:
        {
            "strategy_name": "策略名称",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "profile_id": 123,
            "override_params": {},
            "use_lancedb": false  // 可选，强制使用两层架构
        }
    """
    # 延迟导入避免循环依赖
    from server.app import get_api
    from config.logger import get_logger
    
    logger = get_logger(__name__)
    
    try:
        data = request.get_json() or {}
        strategy_name = data.get('strategy_name')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        profile_id = data.get('profile_id')
        override_params = data.get('override_params') or {}
        use_lancedb = data.get('use_lancedb', is_lancedb_backend())

        if not all([strategy_name, start_date, end_date]):
            return json_response({
                "success": False, 
                "error": "缺少必要参数: strategy_name, start_date, end_date"
            }, status_code=400)

        # 如果提供了 profile_id，则从 LanceDB 中加载对应的参数预设
        effective_params = override_params
        if profile_id is not None:
            from core.profiles.profile_repository import get_profile as load_profile

            profile = load_profile(int(profile_id))
            if profile is None:
                return json_response({
                    "success": False, 
                    "error": f"Profile {profile_id} 不存在"
                }, status_code=400)
            # 合并 profile 参数和本次请求的覆盖参数
            params_from_profile = profile.get("params") or {}
            if not isinstance(params_from_profile, dict):
                params_from_profile = {}
            effective_params = {**params_from_profile, **override_params}
        else:
            # 不使用 Profile，直接使用请求体中的参数
            effective_params = data.get('params') or {}

        # 【两层架构】如果使用 LanceDB，可以尝试使用 Polars 回测
        if use_lancedb and is_lancedb_backend():
            logger.info(f"[LanceDB] 使用两层架构运行回测: {strategy_name}")
            # 注意：这里可以添加针对两层架构的优化回测逻辑
            # 例如：使用 Polars 直接在 Arrow 数据上执行策略计算
            # 目前保持与原有 API 兼容
        
        result = get_api().run_backtest_and_get_data(
            strategy_name,
            start_date,
            end_date,
            params=effective_params,
        )
        # 使用 orjson 加速响应
        return json_response({"success": True, "data": result})

    except Exception as e:
        logger = get_logger(__name__)
        logger.error(f"回测失败: {e}", exc_info=True)
        return json_response({"success": False, "error": str(e)}, status_code=500)


# ============================================================================
# T2.5: 真实回测入口（统一契约）
# ============================================================================

# 同步执行时间上限：超过此值自动切换到 job 模式（线程后台执行）
SYNC_TIMEOUT_SECONDS = 30.0
# 异步 job 模式：前端轮询最长 5 分钟
ASYNC_JOB_POLL_MAX_SECONDS = 300.0


@backtest_bp.route('/backtest/run', methods=['POST'])
def run_backtest_v2():
    """
    【T2.5 真实回测入口】统一契约

    行为约定:
      - 同步模式: 请求 30s 内能完成则直接返回完整结果
      - 异步模式: 30s 未完成则提交后台线程, 返回 job_id; 前端轮询 /backtest/jobs/<id>
      - 失败原因归类为 8 类业务错误码 (BACKTEST_*)

    请求体:
        {
            "strategy_id":       "AI策略" 或 "user_main_wave_trend",
            "start_date":        "2024-01-01",
            "end_date":          "2024-03-31",
            "initial_capital":   1000000,        # 可选, 默认 1,000,000
            "params":            {...}            # 可选, 策略参数
        }

    响应 (同步 / 完成):
        {
            "success": true,
            "data": {
                "job_id":       "abc123",
                "status":       "success",
                "progress":     100,
                "stats":        {totalReturn, sharpeRatio, maxDrawdown, winRate, ...},
                "equity_curve": [{date, equity}, ...],
                "message":      "回测完成",
                "elapsed_ms":   1234,
                "endpoint":     "/api/backtest/run"
            },
            "meta": {"mode": "sync", "elapsed_ms": 1234}
        }

    响应 (异步 / 排队中):
        {
            "success": true,
            "data": {"job_id": "abc123", "status": "pending", "progress": 0, "endpoint": "/api/backtest/run"},
            "meta": {"mode": "async", "hint": "请轮询 GET /api/backtest/jobs/<id>"}
        }

    响应 (失败):
        {
            "success": false,
            "data": null,
            "error": {
                "code":        "BACKTEST_STRATEGY_NOT_REGISTERED",
                "message":     "...",
                "detail":      "...",
                "retryable":   false,
                "suggestion":  "...",
                "endpoint":    "/api/backtest/run"
            }
        }
    """
    import time as _time
    from server.services.backtest_runner_service import (
        BacktestErrorCode,
        ERROR_SUGGESTIONS,
        execute_backtest,
        get_job_registry,
        run_backtest_in_thread,
    )
    from config.logger import get_logger as _get_logger

    logger = _get_logger(__name__)
    registry = get_job_registry()

    try:
        payload = request.get_json(silent=True) or {}
        strategy_id = (payload.get("strategy_id") or payload.get("strategy_name") or "").strip()
        start_date = (payload.get("start_date") or "").strip()
        end_date = (payload.get("end_date") or "").strip()
        initial_capital = float(payload.get("initial_capital") or 1_000_000.0)
        params = payload.get("params") or payload.get("override_params") or {}
        if not isinstance(params, dict):
            params = {}

        # ---- 缺参前置拦截 ----
        if not strategy_id or not start_date or not end_date:
            return json_response({
                "success": False,
                "data": None,
                "error": {
                    "code": BacktestErrorCode.MISSING_PARAMETERS.value,
                    "message": "缺少必要参数: strategy_id / start_date / end_date",
                    "detail": f"strategy_id={strategy_id!r} start_date={start_date!r} end_date={end_date!r}",
                    "retryable": False,
                    "suggestion": ERROR_SUGGESTIONS[BacktestErrorCode.MISSING_PARAMETERS.value],
                    "endpoint": "/api/backtest/run",
                }
            }, status_code=400)

        job = registry.create(
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            params=params,
        )

        # ---- 同步尝试 (前台执行, 受 30s timeout 限制) ----
        started_at = _time.time()

        # 用线程 + join(timeout) 实现"软超时" — 超过 30s 立即返回 job_id
        result_holder: Dict[str, Any] = {}

        def _sync_runner() -> None:
            try:
                result_holder["result"] = execute_backtest(
                    strategy_id=strategy_id,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=initial_capital,
                    params=params,
                )
            except Exception as e:  # 防御性兜底
                result_holder["exception"] = e
                logger.error(f"[BacktestRunner] 同步任务异常: {e}", exc_info=True)

        t = threading.Thread(target=_sync_runner, daemon=True, name=f"backtest-sync-{job.job_id}")
        t.start()
        t.join(timeout=SYNC_TIMEOUT_SECONDS)

        elapsed = (_time.time() - started_at) * 1000

        if not result_holder:
            # 超时 → 切换到 job 模式 (后台继续跑, 注册表里已有 job)
            logger.info(
                f"[BacktestRunner] 同步超时 ({SYNC_TIMEOUT_SECONDS}s), 切到 job 模式: {job.job_id}"
            )
            # 启动真正的后台任务 (覆盖上面未启动的)
            t_bg = threading.Thread(
                target=run_backtest_in_thread,
                args=(job, registry),
                daemon=True,
                name=f"backtest-async-{job.job_id}",
            )
            t_bg.start()
            return json_response({
                "success": True,
                "data": {
                    "job_id": job.job_id,
                    "status": "pending",
                    "progress": 0,
                    "message": f"任务已提交, 超过 {SYNC_TIMEOUT_SECONDS:.0f}s 已切换异步模式",
                    "endpoint": "/api/backtest/run",
                },
                "meta": {
                    "mode": "async",
                    "elapsed_ms": int(elapsed),
                    "hint": f"请轮询 GET /api/backtest/jobs/{job.job_id}, 最长 {ASYNC_JOB_POLL_MAX_SECONDS/60:.0f} 分钟",
                },
            })

        # ---- 同步完成 ----
        result = result_holder.get("result") or {}
        if "exception" in result_holder:
            # 防御性: 上面的 execute_backtest 已经捕获, 走到这里说明二次异常
            err = {
                "code": BacktestErrorCode.RUNTIME_EXCEPTION.value,
                "message": "回测执行异常",
                "detail": str(result_holder["exception"]),
                "retryable": True,
                "suggestion": ERROR_SUGGESTIONS[BacktestErrorCode.RUNTIME_EXCEPTION.value],
                "endpoint": "/api/backtest/run",
            }
            return json_response({
                "success": False, "data": None, "error": err,
            }, status_code=500)

        if result.get("success"):
            data = result["data"] or {}
            return json_response({
                "success": True,
                "data": {
                    "job_id": job.job_id,
                    "status": "success",
                    "progress": 100,
                    "stats": data.get("stats") or {},
                    "equity_curve": data.get("equity_curve") or [],
                    "trades": data.get("trades") or [],
                    "message": "回测完成",
                    "elapsed_ms": int(elapsed),
                    "endpoint": "/api/backtest/run",
                },
                "meta": {"mode": "sync", "elapsed_ms": int(elapsed)},
            })

        # ---- 同步失败 (业务错误) ----
        err = result.get("error") or {}
        err.setdefault("endpoint", "/api/backtest/run")
        # 字段缺失降级: 业务错误用 200 + success=false, 前端按 code 处理
        return json_response({
            "success": False,
            "data": {
                "job_id": job.job_id,
                "status": "failed",
                "progress": 0,
                "message": err.get("message") or "回测失败",
                "elapsed_ms": int(elapsed),
                "endpoint": "/api/backtest/run",
            },
            "error": err,
            "meta": {"mode": "sync", "elapsed_ms": int(elapsed)},
        })

    except Exception as e:
        logger.error(f"[BacktestRunner] /backtest/run 顶层异常: {e}", exc_info=True)
        return json_response({
            "success": False,
            "data": None,
            "error": {
                "code": BacktestErrorCode.RUNTIME_EXCEPTION.value,
                "message": "回测入口服务异常",
                "detail": f"{type(e).__name__}: {e}",
                "retryable": True,
                "suggestion": ERROR_SUGGESTIONS[BacktestErrorCode.RUNTIME_EXCEPTION.value],
                "endpoint": "/api/backtest/run",
            }
        }, status_code=500)


@backtest_bp.route('/backtest/jobs/<job_id>', methods=['GET'])
def get_backtest_job(job_id: str):
    """
    【T2.5 轮询】获取回测任务状态

    Path:
        job_id: POST /api/backtest/run 返回的 job_id

    响应:
        {
            "success": true,
            "data": {
                "job_id": "abc123",
                "status": "pending|running|success|failed",
                "progress": 0-100,
                "message": "...",
                "stats":     {...} (success 时存在, 字段缺失降级为 null),
                "equity_curve": [...] (success 时存在),
                "error":     null | {code, message, detail, retryable, suggestion, endpoint},
                "endpoint":  "/api/backtest/run",
                "elapsed_ms": 1234
            }
        }
    """
    from server.services.backtest_runner_service import get_job_registry

    registry = get_job_registry()
    job = registry.get(job_id)

    if job is None:
        return json_response({
            "success": False,
            "data": None,
            "error": {
                "code": "BACKTEST_JOB_NOT_FOUND",
                "message": f"任务不存在或已过期: {job_id}",
                "detail": "任务完成后 1 小时会被清理, 请重新提交回测",
                "retryable": False,
                "suggestion": "请回到 Step 7 重新点击\"运行回测\"",
                "endpoint": f"/api/backtest/jobs/{job_id}",
            }
        }, status_code=404)

    return json_response({
        "success": True,
        "data": job.to_public_dict(),
    })


# ============================================================================
# 运行回测（异步 - 推荐）
# ============================================================================

@backtest_bp.route('/run_backtest_async', methods=['POST'])
def run_backtest_async():
    """
    【异步】运行回测（推荐）
    
    提交回测任务到 Celery 队列，立即返回 Task ID。
    前端可通过轮询 /backtest/status/<task_id> 获取进度和结果。
    
    【架构说明】
    - 任务提交后立即返回，不阻塞请求
    - 回测在独立 Worker 进程中执行
    - 支持任务取消和进度查询
    
    请求体:
        {
            "strategy_name": "策略名称",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "profile_id": 123,
            "override_params": {},
            "use_lancedb": false
        }
    
    返回:
        {
            "success": true,
            "task_id": "abc-123-def",
            "status": "pending",
            "message": "回测任务已提交"
        }
    """
    from server.tasks.backtest_tasks import run_backtest_task
    from server.services.task_status_service import get_task_status_service
    from config.logger import get_logger
    
    logger = get_logger(__name__)
    
    try:
        data = request.get_json() or {}
        strategy_name = data.get('strategy_name')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        profile_id = data.get('profile_id')
        override_params = data.get('override_params') or {}
        use_lancedb = data.get('use_lancedb', is_lancedb_backend())

        if not all([strategy_name, start_date, end_date]):
            return json_response({
                "success": False, 
                "error": "缺少必要参数: strategy_name, start_date, end_date"
            }, status_code=400)

        # 提交异步任务
        task = run_backtest_task.delay(
            strategy_name=strategy_name,
            start_date=start_date,
            end_date=end_date,
            params=override_params or None,
            profile_id=profile_id,
            use_lancedb=use_lancedb
        )
        
        logger.info(f"[Async] 回测任务已提交: {task.id} - {strategy_name}")
        
        return json_response({
            "success": True,
            "task_id": task.id,
            "status": "pending",
            "message": "回测任务已提交",
            "strategy_name": strategy_name,
            "start_date": start_date,
            "end_date": end_date
        })
        
    except Exception as e:
        logger.error(f"提交回测任务失败: {e}", exc_info=True)
        return json_response({
            "success": False, 
            "error": str(e)
        }, status_code=500)


@backtest_bp.route('/backtest/status/<task_id>', methods=['GET'])
def get_backtest_status(task_id: str):
    """
    获取回测任务状态
    
    Args:
        task_id: Celery 任务 ID
        
    返回:
        {
            "success": true,
            "task_id": "abc-123",
            "state": "pending|started|progress|success|failure",
            "progress": {"current": 50, "total": 100, "percent": 50, "message": "..."},
            "result": {...},  // 仅在 success 时存在
            "error": "..."     // 仅在 failure 时存在
        }
    """
    from server.services.task_status_service import get_task_status_service, TaskState
    from config.logger import get_logger
    
    logger = get_logger(__name__)
    
    try:
        task_service = get_task_status_service()
        task_info = task_service.get_task_status(task_id)
        
        if task_info is None:
            # 尝试从 Celery 获取
            from config.celery_config import get_task_status
            celery_status = get_task_status(task_id)
            
            return json_response({
                "success": True,
                "task_id": task_id,
                "state": celery_status.get('state', 'unknown').lower(),
                "ready": celery_status.get('ready', False),
                "result": celery_status.get('result'),
                "error": celery_status.get('error')
            })
        
        return json_response({
            "success": True,
            "task_id": task_info.task_id,
            "state": task_info.state.value,
            "progress": task_info.progress,
            "result": task_info.result,
            "error": task_info.error,
            "meta": task_info.meta
        })
        
    except Exception as e:
        logger.error(f"获取任务状态失败: {e}", exc_info=True)
        return json_response({
            "success": False,
            "error": str(e)
        }, status_code=500)


@backtest_bp.route('/backtest/result/<task_id>', methods=['GET'])
def get_backtest_result(task_id: str):
    """
    获取回测任务结果
    
    仅在任务成功完成后返回完整结果。
    
    Args:
        task_id: Celery 任务 ID
    """
    from server.services.task_status_service import get_task_status_service, TaskState
    from config.logger import get_logger
    
    logger = get_logger(__name__)
    
    try:
        task_service = get_task_status_service()
        task_info = task_service.get_task_status(task_id)
        
        if task_info is None:
            return json_response({
                "success": False,
                "error": "任务不存在"
            }, status_code=404)
        
        if task_info.state != TaskState.SUCCESS:
            return json_response({
                "success": False,
                "error": f"任务尚未完成，当前状态: {task_info.state.value}",
                "state": task_info.state.value
            }, status_code=400)
        
        # 获取完整结果
        result = task_service.get_result(task_id)
        
        return json_response({
            "success": True,
            "task_id": task_id,
            "data": result or task_info.result
        })
        
    except Exception as e:
        logger.error(f"获取任务结果失败: {e}", exc_info=True)
        return json_response({
            "success": False,
            "error": str(e)
        }, status_code=500)


@backtest_bp.route('/backtest/cancel/<task_id>', methods=['POST'])
def cancel_backtest_task(task_id: str):
    """
    取消回测任务
    
    Args:
        task_id: Celery 任务 ID
        
    返回:
        {
            "success": true,
            "message": "任务已取消"
        }
    """
    from server.tasks.backtest_tasks import cancel_backtest_task as do_cancel
    from config.logger import get_logger
    
    logger = get_logger(__name__)
    
    try:
        success = do_cancel(task_id)
        
        if success:
            return json_response({
                "success": True,
                "message": "任务已取消"
            })
        else:
            return json_response({
                "success": False,
                "error": "取消任务失败，任务可能已完成或不存在"
            }, status_code=400)
            
    except Exception as e:
        logger.error(f"取消任务失败: {e}", exc_info=True)
        return json_response({
            "success": False,
            "error": str(e)
        }, status_code=500)


# ============================================================================
# 回测报告分析
# ============================================================================

@backtest_bp.route('/analyze_report', methods=['POST'])
def analyze_report():
    """
    接收前端发来的回测结果，让 AI 进行分析
    
    请求体:
        {
            "strategy_id": "ai_gen_12345",
            "backtest_result": { ...完整的回测结果... }
        }
    
    返回:
        流式响应，包含进度更新和最终报告
    """
    try:
        from server.services.analysis_service import AnalysisService
        from server.services.strategy_service import StrategyService
        from config.logger import get_logger
        
        logger = get_logger(__name__)
        
        data = request.get_json() or {}
        strategy_id = data.get('strategy_id', '')
        backtest_result = data.get('backtest_result')
        
        if not backtest_result:
            return json_response(
                {"success": False, "error": "回测结果不能为空"}, 
                status_code=400
            )
        
        def generate_report_stream():
            """生成带有进度更新的流式响应"""
            # 发送初始进度
            yield f"progress:{json.dumps({'progress': 0, 'stage': '准备分析数据...'})}\n"
            
            # 1. 获取策略源代码 (为了让 AI 结合逻辑看数据)
            strategy_code = ""
            if strategy_id:
                try:
                    strategy_service = StrategyService()
                    yield f"progress:{json.dumps({'progress': 10, 'stage': '获取策略源代码...'})}\n"
                    strategy_code = strategy_service.get_strategy_code(strategy_id)
                    if not strategy_code:
                        logger.warning(f"无法获取策略 {strategy_id} 的源代码，将仅基于回测数据进行分析")
                except Exception as e:
                    logger.warning(f"获取策略源代码失败: {e}，将仅基于回测数据进行分析")
            
            yield f"progress:{json.dumps({'progress': 25, 'stage': '数据预处理...'})}\n"
            
            # 2. 生成分析报告
            analysis_service = AnalysisService()
            
            # 从回测结果中获取策略名称
            strategy_name = (
                backtest_result.get('strategyInfo', {}).get('name') or 
                strategy_id or 
                '未知策略'
            )
            
            logger.info(f"开始生成策略分析报告: {strategy_name} (ID: {strategy_id})")
            
            yield f"progress:{json.dumps({'progress': 50, 'stage': 'AI 深度分析中...'})}\n"
            
            # 开启真正的流式文本生成
            for chunk in analysis_service.generate_review_stream(
                strategy_name=strategy_name,
                backtest_result=backtest_result,
                strategy_code=strategy_code
            ):
                yield f"stream:{json.dumps({'content': chunk})}\n"
            
            yield f"progress:{json.dumps({'progress': 100, 'stage': '分析完成'})}\n"
        
        # 返回流式响应
        return Response(
            generate_report_stream(),
            mimetype='text/plain',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'
            }
        )
        
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"生成分析报告失败: {e}", exc_info=True)
        return json_response(
            {"success": False, "error": f"生成分析报告失败: {str(e)}"}, 
            status_code=500
        )


# ============================================================================
# 三层架构专用回测 API
# ============================================================================

@backtest_bp.route('/run_backtest_sql', methods=['POST'])
def run_backtest_sql():
    """
    【两层架构】使用 Polars 运行回测
    
    这是两层架构的核心优势：直接在 Arrow 数据上使用 Polars 执行向量化回测
    
    请求体:
        {
            "symbols": ["000001.SZ", "000002.SZ"],
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "strategy_expr": "close > MA20",
            "library": "daily"
        }
    
    Returns:
        {
            "success": True,
            "trades": [...],
            "metrics": {...},
            "query_time_ms": 150
        }
    """
    try:
        if not is_lancedb_backend():
            return json_response({
                "success": False,
                "error": "当前未使用 LanceDB 后端，无法使用 Polars 回测"
            }, status_code=400)
        
        data = request.get_json() or {}
        symbols = data.get('symbols', [])
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        strategy_sql = data.get('strategy_sql')
        library = data.get('library', 'daily')
        
        if not symbols:
            return json_response({
                "success": False,
                "error": "缺少 symbols 参数"
            }, status_code=400)
        
        if not strategy_sql:
            return json_response({
                "success": False,
                "error": "缺少 strategy_sql 参数"
            }, status_code=400)
        
        import time
        start_time = time.time()
        
        # 使用统一接口进行回测
        interface = get_unified_data_interface()
        if not interface:
            return json_response({
                "success": False,
                "error": "数据接口未初始化"
            }, status_code=500)
        
        # 运行 SQL 回测
        result = interface.run_backtest(
            strategy_sql=strategy_sql,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
            library=library
        )
        
        query_time_ms = int((time.time() - start_time) * 1000)
        
        return json_response({
            "success": True,
            "data": result,
            "query_time_ms": query_time_ms
        })
        
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"SQL 回测失败: {e}", exc_info=True)
        return json_response({
            "success": False,
            "error": str(e)
        }, status_code=500)


@backtest_bp.route('/calculate_factor', methods=['POST'])
def calculate_factor():
    """
    【两层架构】计算技术指标/因子
    
    使用 Polars 的窗口函数高效计算技术指标
    
    请求体:
        {
            "symbols": ["000001.SZ"],
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "factors": ["ma20", "rsi14", "macd"],
            "library": "daily"
        }
    
    Returns:
        {
            "success": True,
            "data": [...],
            "query_time_ms": 150
        }
    """
    try:
        if not is_lancedb_backend():
            return json_response({
                "success": False,
                "error": "当前未使用 LanceDB 后端"
            }, status_code=400)
        
        data = request.get_json() or {}
        symbols = data.get('symbols', [])
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        factors = data.get('factors', ['ma20'])
        library = data.get('library', 'daily')
        
        if not symbols:
            return json_response({
                "success": False,
                "error": "缺少 symbols 参数"
            }, status_code=400)
        
        import time
        start_time = time.time()
        
        interface = get_unified_data_interface()
        if not interface:
            return json_response({
                "success": False,
                "error": "数据接口未初始化"
            }, status_code=500)
        
        # 获取数据
        df = interface.get_multiple_stocks(
            symbols=symbols,
            start=start_date,
            end=end_date,
            library=library
        )
        
        if df is None or df.empty:
            return json_response({
                "success": True,
                "data": [],
                "message": "未找到数据"
            })
        
        # 转换为 Arrow
        arrow_table = interface.bridge.from_pandas(df)
        
        # 计算因子
        result_table = arrow_table
        for factor in factors:
            if factor.startswith('ma'):
                window = int(factor[2:]) if len(factor) > 2 else 20
                result_table = interface.analytics.calculate_moving_average(
                    result_table, column="close", window=window
                )
            elif factor == 'rsi14':
                # RSI 计算可以通过 Polars 实现
                pass
            elif factor == 'macd':
                # MACD 计算可以通过 Polars 实现
                pass
        
        # 转换回 Pandas
        result_df = interface.bridge.to_pandas(result_table)
        
        query_time_ms = int((time.time() - start_time) * 1000)
        
        return json_response({
            "success": True,
            "data": result_df.to_dict('records'),
            "query_time_ms": query_time_ms,
            "columns": list(result_df.columns)
        })
        
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"因子计算失败: {e}", exc_info=True)
        return json_response({
            "success": False,
            "error": str(e)
        }, status_code=500)
