"""
DragonEye 路由模块
提供爬虫、数据清洗、飞书推送的 API 端点
支持 SSE 实时日志流

所有接口统一响应结构：
- 成功：
  {
    "success": true,
    "data": <T>,
    "meta": {
        "endpoint": str,
        "target_date": "YYYY-MM-DD" | null,
        "latest_date": "YYYY-MM-DD" | null,
        "has_data": bool,
        "source": "lancedb|local_spider|arcticdb|mock",
        "missing_parts": [str, ...],
        "partial_success": bool,
        "elapsed_ms": int
    },
    "error": null
  }
- 失败：
  {
    "success": false,
    "data": null,
    "meta": { ... },
    "error": {"code": str, "message": str, "detail": str, "retryable": bool}
  }
- HTTP 状态码：retryable=True → 503；参数错误 → 400；任务不存在 → 404；其他 → 500
"""
import json
import time
import queue
from typing import Any, Optional
from flask import Blueprint, request, jsonify, Response, stream_with_context
from datetime import datetime
from config.logger import get_logger
from core.research.dragon_eye.job_manager import job_manager

logger = get_logger(__name__)
dragon_bp = Blueprint('dragon', __name__, url_prefix='/api/research/dragon')
_dragon_service = None


# ==========================================
# 服务单例
# ==========================================

def get_dragon_service():
    """
    惰性获取 DragonEyeService 单例。

    Returns:
        DragonEyeService 实例
    """
    global _dragon_service
    if _dragon_service is None:
        from core.research.dragon_eye.service import DragonEyeService
        _dragon_service = DragonEyeService()
    return _dragon_service


# ==========================================
# 错误码与统一响应 helper
# ==========================================

class DragonEyeErrorCode:
    """DragonEye 标准错误码常量集合。"""
    DATA_MISSING = "DRAGON_EYE_DATA_MISSING"
    NETWORK_ERROR = "DRAGON_EYE_NETWORK_ERROR"
    AUTH_ERROR = "DRAGON_EYE_AUTH_ERROR"
    TIMEOUT = "DRAGON_EYE_TIMEOUT"
    INTERNAL_ERROR = "DRAGON_EYE_INTERNAL_ERROR"
    JOB_NOT_FOUND = "DRAGON_EYE_JOB_NOT_FOUND"
    STREAM_ERROR = "DRAGON_EYE_STREAM_ERROR"
    BAD_REQUEST = "DRAGON_EYE_BAD_REQUEST"
    PUSH_FAILED = "DRAGON_EYE_PUSH_FAILED"


def _now_ms() -> int:
    """获取当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _elapsed_ms(start_ms: int) -> int:
    """计算从 start_ms 起的耗时（毫秒，防御性取 max(0, …)）。"""
    return max(0, _now_ms() - int(start_ms or 0))


def _default_meta(*, endpoint: Optional[str] = None,
                  target_date: Optional[str] = None,
                  latest_date: Optional[str] = None,
                  has_data: bool = False,
                  source: str = "lancedb",
                  missing_parts: Optional[list] = None,
                  partial_success: bool = False,
                  elapsed_ms: int = 0) -> dict:
    """构造默认 meta 字典，缺省字段填 None / 0 / []。"""
    return {
        "endpoint": endpoint,
        "target_date": target_date,
        "latest_date": latest_date,
        "has_data": has_data,
        "source": source,
        "missing_parts": list(missing_parts) if missing_parts else [],
        "partial_success": partial_success,
        "elapsed_ms": int(elapsed_ms or 0),
    }


def _success(data: Any, *, target_date: Optional[str] = None,
             latest_date: Optional[str] = None, has_data: bool = True,
             source: str = "lancedb", missing_parts: Optional[list] = None,
             partial_success: bool = False, elapsed_ms: int = 0,
             endpoint: Optional[str] = None) -> dict:
    """
    构造统一成功响应。

    Args:
        data: 业务数据（与原结构兼容）
        target_date: 目标日期 (YYYY-MM-DD)
        latest_date: 最新日期 (YYYY-MM-DD)
        has_data: 是否有数据
        source: 数据来源 (lancedb|arcticdb|local_spider|mock)
        missing_parts: 缺失的子部分列表
        partial_success: 是否部分成功
        elapsed_ms: 耗时（毫秒）
        endpoint: 端点路径

    Returns:
        统一响应 dict
    """
    meta = _default_meta(
        endpoint=endpoint, target_date=target_date, latest_date=latest_date,
        has_data=has_data, source=source, missing_parts=missing_parts,
        partial_success=partial_success, elapsed_ms=elapsed_ms,
    )
    return {
        "success": True,
        "data": data,
        "meta": meta,
        "error": None,
    }


def _failure(code: str, message: str, detail: str = "", *,
             retryable: bool = True, status_code: Optional[int] = None,
             endpoint: Optional[str] = None, target_date: Optional[str] = None,
             latest_date: Optional[str] = None, has_data: bool = False,
             source: str = "lancedb", missing_parts: Optional[list] = None,
             partial_success: bool = False, elapsed_ms: int = 0):
    """
    构造统一失败响应。

    Args:
        code: 错误码
        message: 错误消息
        detail: 错误详情
        retryable: 是否可重试
        status_code: HTTP 状态码，默认 retryable=True→503；其他→500
        其余字段：写入 meta

    Returns:
        (response_dict, http_status_code)
    """
    if status_code is None:
        status_code = 503 if retryable else 500
    meta = _default_meta(
        endpoint=endpoint, target_date=target_date, latest_date=latest_date,
        has_data=has_data, source=source, missing_parts=missing_parts,
        partial_success=partial_success, elapsed_ms=elapsed_ms,
    )
    return {
        "success": False,
        "data": None,
        "meta": meta,
        "error": {
            "code": code,
            "message": message,
            "detail": detail or "",
            "retryable": bool(retryable),
        },
    }, status_code


def _error_code_for(exc: Exception) -> tuple:
    """
    把异常分类为 DragonEye 标准错误码。

    分类规则（防御性匹配：异常名 + 消息双重判定）：
        - TimeoutError / 含 "timeout"       → TIMEOUT, retryable=True, 503
        - ConnectionError/NetworkError/HTTPError / 网络关键字 → NETWORK_ERROR, retryable=True, 503
        - Auth 关键字                         → AUTH_ERROR, retryable=False, 500
        - FileNotFoundError / KeyError        → DATA_MISSING, retryable=False, 200
        - 其他                                → INTERNAL_ERROR, retryable=False, 500

    Returns:
        (error_code, retryable, http_status_code)
    """
    name = (exc.__class__.__name__ or "").lower()
    msg = str(exc or "").lower()

    if "timeout" in name or "timeout" in msg:
        return DragonEyeErrorCode.TIMEOUT, True, 503
    if any(k in name for k in ("connection", "network", "httperror")) or \
       any(k in msg for k in ("connection refused", "connection reset",
                              "dns", "unreachable", "remote disconnect")):
        return DragonEyeErrorCode.NETWORK_ERROR, True, 503
    if any(k in name for k in ("auth", "permission", "unauthorized")) or \
       any(k in msg for k in ("unauthorized", "forbidden", "token", "webhook", "auth")):
        return DragonEyeErrorCode.AUTH_ERROR, False, 500
    if "filenotfound" in name or "keyerror" in name:
        return DragonEyeErrorCode.DATA_MISSING, False, 200
    return DragonEyeErrorCode.INTERNAL_ERROR, False, 500


def _detect_missing_parts(data: Any, *, expect: Optional[list] = None) -> list:
    """
    根据 service 返回的 data 检测缺失的子部分。

    约定优先级：
        1) 若 data 为 dict 且显式包含 ``has_ladder/has_limit_up/has_sentiment/has_theme_flow``
           中的任意 key，则按这些 flag 聚合缺失项；
        2) 否则当 data 为空（None / 空 dict / 空 list）时，认为所有 expect 项均缺失。

    Args:
        data: service 返回的业务数据
        expect: 期望的子部分列表

    Returns:
        缺失的子部分列表
    """
    if expect is None:
        expect = ["has_ladder", "has_limit_up", "has_sentiment", "has_theme_flow"]
    if data is None:
        return list(expect)
    if isinstance(data, dict):
        flags = [k for k in expect if k in data]
        if flags:
            return [k for k in flags if not data.get(k)]
        # 无显式 flag：检查数据体是否空
        for key in ("data", "items", "rows", "history", "bubbles",
                    "nodes", "links", "stocks", "content"):
            val = data.get(key)
            if isinstance(val, (list, str)) and len(val) == 0:
                return list(expect)
    elif isinstance(data, list) and len(data) == 0:
        return list(expect)
    return []


# ==========================================
# 爬虫任务相关接口
# ==========================================

@dragon_bp.route('/crawl', methods=['POST'])
def trigger_crawl():
    """
    启动爬虫任务（统一响应）

    Request Body:
        - date: 目标日期 (YYYY-MM-DD)，默认为今天
        - sync: 是否同步等待结果，默认 False

    Returns:
        success.data: {"job_id", "status", "progress", "message"}
    """
    start_ms = _now_ms()
    endpoint = request.path
    data = request.json or {}
    target_date = data.get('date') or datetime.now().strftime("%Y-%m-%d")
    sync = bool(data.get('sync', False))

    try:
        service = get_dragon_service()
        job_id = service.run_crawler(target_date)

        if sync:
            # 同步模式：等待任务完成
            max_wait = 300
            waited = 0
            while waited < max_wait:
                job = job_manager.get_job(job_id)
                if job and job.status in ['completed', 'failed']:
                    break
                time.sleep(1)
                waited += 1

            job = job_manager.get_job(job_id)
            payload = {
                "job_id": job_id,
                "status": job.status if job else "unknown",
                "progress": job.progress if job else 0,
                "message": job.message if job else "",
            }
            has_data = bool(job and job.status == "completed")
            return jsonify(_success(
                payload,
                target_date=target_date,
                has_data=has_data,
                source="local_spider",
                elapsed_ms=_elapsed_ms(start_ms),
                endpoint=endpoint,
            ))

        # 异步模式：立即返回任务ID
        payload = {
            "job_id": job_id,
            "status": "running",
            "message": f"爬虫任务已启动，目标日期: {target_date}",
        }
        return jsonify(_success(
            payload,
            target_date=target_date,
            has_data=True,
            source="local_spider",
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to start crawl: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"启动爬虫任务失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=target_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/clean', methods=['POST'])
def trigger_clean():
    """
    启动数据清洗任务（统一响应）

    Request Body:
        - date: 目标日期 (YYYY-MM-DD)

    Returns:
        success.data: {"job_id", "status", "message"}
    """
    start_ms = _now_ms()
    endpoint = request.path
    data = request.json or {}
    target_date = data.get('date')

    if not target_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少必要参数 date",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        job_id = service.process_and_persist(target_date)
        payload = {
            "job_id": job_id,
            "status": "running",
            "message": f"清洗任务已启动，目标日期: {target_date}",
        }
        return jsonify(_success(
            payload,
            target_date=target_date,
            has_data=True,
            source="local_spider",
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to start clean: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"启动清洗任务失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=target_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/pipeline', methods=['POST'])
def trigger_pipeline():
    """
    启动完整工作流：爬虫 -> 清洗 -> 推送（统一响应）

    Request Body:
        - date: 目标日期 (YYYY-MM-DD)
        - push_feishu: 是否推送到飞书，默认 True

    Returns:
        success.data: {"job_id", "status", "message"}
    """
    start_ms = _now_ms()
    endpoint = request.path
    data = request.json or {}
    target_date = data.get('date') or datetime.now().strftime("%Y-%m-%d")
    push_feishu = bool(data.get('push_feishu', True))

    try:
        service = get_dragon_service()
        job_id = service.run_full_pipeline(target_date, push_feishu)
        payload = {
            "job_id": job_id,
            "status": "running",
            "message": f"完整工作流已启动，目标日期: {target_date}",
        }
        return jsonify(_success(
            payload,
            target_date=target_date,
            has_data=True,
            source="local_spider",
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to start pipeline: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"启动工作流失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=target_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


# ==========================================
# 任务状态查询接口
# ==========================================

@dragon_bp.route('/jobs', methods=['GET'])
def list_jobs():
    """
    获取最近的任务列表（统一响应）

    Query Parameters:
        - type: 任务类型过滤 (crawl, clean, push, full_pipeline)
        - limit: 返回数量限制，默认 10

    Returns:
        success.data: {"jobs": [...], "total": int}
    """
    start_ms = _now_ms()
    endpoint = request.path
    job_type = request.args.get('type')
    try:
        limit = int(request.args.get('limit', 10))
    except (TypeError, ValueError):
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "limit 参数必须为整数",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        jobs = job_manager.get_recent_jobs(job_type, limit)
        payload = {
            "jobs": [job.to_dict() for job in jobs],
            "total": len(jobs),
        }
        return jsonify(_success(
            payload,
            has_data=len(jobs) > 0,
            source="local",
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to list jobs: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取任务列表失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/jobs/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """
    获取任务状态（统一响应）

    Path Parameters:
        - job_id: 任务ID

    Returns:
        success.data: 完整任务状态对象
    """
    start_ms = _now_ms()
    endpoint = request.path

    try:
        job = job_manager.get_job(job_id)
        if not job:
            body, status = _failure(
                DragonEyeErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}",
                retryable=False, status_code=404,
                endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
            )
            return jsonify(body), status
        payload = job.to_dict()
        return jsonify(_success(
            payload,
            has_data=True,
            source="local",
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get job status: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取任务状态失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


# ==========================================
# SSE 实时日志流接口
# ==========================================

@dragon_bp.route('/stream/<job_id>')
def stream_logs(job_id):
    """
    SSE 实时日志流（统一事件协议）

    事件类型：
      - connected: 初始连接确认
      - log: 日志条目
      - heartbeat: 心跳
      - complete: 任务完成
      - error: 任务不存在 / 内部异常
        data: {"code", "message", "detail", "retryable"}

    Path Parameters:
        - job_id: 任务ID
    """
    def generate():
        try:
            # 发送初始连接确认
            yield f"data: {json.dumps({'type': 'connected', 'job_id': job_id, 'data': None})}\n\n"

            # 获取任务信息
            job = job_manager.get_job(job_id)
            if not job:
                err_payload = {
                    "code": DragonEyeErrorCode.JOB_NOT_FOUND,
                    "message": f"任务不存在: {job_id}",
                    "detail": "",
                    "retryable": False,
                }
                yield f"data: {json.dumps({'type': 'error', 'data': err_payload})}\n\n"
                return

            # 发送历史日志
            for log in job.logs:
                yield f"data: {json.dumps({'type': 'log', 'data': log})}\n\n"

            # 如果任务已完成，发送完成标记
            if job.status in ['completed', 'failed']:
                yield f"data: {json.dumps({'type': 'complete', 'status': job.status, 'data': None})}\n\n"
                return

            # 创建队列接收实时日志
            log_queue = queue.Queue()

            def on_log(log_entry):
                log_queue.put(log_entry)

            # 订阅日志更新
            service = get_dragon_service()
            service.subscribe_job_logs(job_id, on_log)

            # 持续发送新日志
            last_heartbeat = time.time()
            while True:
                try:
                    log_entry = log_queue.get(timeout=1)
                    yield f"data: {json.dumps({'type': 'log', 'data': log_entry})}\n\n"
                except queue.Empty:
                    if time.time() - last_heartbeat > 15:
                        yield f"data: {json.dumps({'type': 'heartbeat', 'data': None})}\n\n"
                        last_heartbeat = time.time()

                    job = job_manager.get_job(job_id)
                    if job and job.status in ['completed', 'failed']:
                        yield f"data: {json.dumps({'type': 'complete', 'status': job.status, 'progress': job.progress, 'data': None})}\n\n"
                        break
        except Exception as e:
            logger.error(f"SSE stream error for {job_id}: {e}")
            err_payload = {
                "code": DragonEyeErrorCode.STREAM_ERROR,
                "message": f"SSE 流异常: {e}",
                "detail": str(e),
                "retryable": True,
            }
            try:
                yield f"data: {json.dumps({'type': 'error', 'data': err_payload})}\n\n"
            except Exception:
                logger.error("Failed to write SSE error event")

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


# ==========================================
# 数据查询接口
# ==========================================

@dragon_bp.route('/stocks', methods=['GET'])
def get_dragon_stocks():
    """
    获取龙头股数据（统一响应）

    Query Parameters:
        - start_date: 开始日期 (YYYY-MM-DD)
        - end_date: 结束日期 (YYYY-MM-DD)

    Returns:
        success.data: 龙头股列表（list[dict]）
    """
    start_ms = _now_ms()
    endpoint = request.path
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if not start_date or not end_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 start_date 或 end_date",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        import polars as pl
        service = get_dragon_service()
        df = service.manager.get_historical_dragon(start_date, end_date)

        # 日期格式归一：Date/Datetime → 'YYYY-MM-DD' 字符串
        if df is not None and not df.is_empty() and 'trade_date' in df.columns:
            dtype = df['trade_date'].dtype
            if dtype in (pl.Date, pl.Datetime):
                df = df.with_columns(
                    pl.col('trade_date').dt.strftime('%Y-%m-%d').alias('trade_date')
                )

        data = df.to_dicts() if df is not None else []
        has_data = isinstance(data, list) and len(data) > 0
        return jsonify(_success(
            data,
            target_date=end_date,
            has_data=has_data,
            source="lancedb",
            missing_parts=[] if has_data else ["has_limit_up"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get stocks: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取龙头股数据失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=end_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/sentiment', methods=['GET'])
def get_market_sentiment():
    """
    获取市场情绪数据（统一响应）

    Query Parameters:
        - start_date: 开始日期 (YYYY-MM-DD)
        - end_date: 结束日期 (YYYY-MM-DD)

    Returns:
        success.data: 情绪数据列表（list[dict]）
    """
    start_ms = _now_ms()
    endpoint = request.path
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if not start_date or not end_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 start_date 或 end_date",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        df = service.manager.get_market_sentiment(start_date, end_date)
        data = df.to_dicts() if df is not None else []
        has_data = isinstance(data, list) and len(data) > 0
        return jsonify(_success(
            data,
            target_date=end_date,
            has_data=has_data,
            source="lancedb",
            missing_parts=[] if has_data else ["has_sentiment"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get sentiment: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取市场情绪数据失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=end_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/sentiment/score', methods=['GET'])
def get_sentiment_score():
    """
    获取综合情绪评分（统一响应）

    Query Parameters:
        - date: 目标日期 (YYYY-MM-DD)

    Returns:
        success.data: 情绪评分 dict（含 trade_date, summary, cycle_phase, risk_level, ...）
    """
    start_ms = _now_ms()
    endpoint = request.path
    target_date = request.args.get('date')

    if not target_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 date 参数",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        data = service.get_sentiment_score(target_date) or {}
        has_data = bool(data.get("has_data")) if isinstance(data, dict) else False
        return jsonify(_success(
            data,
            target_date=target_date,
            has_data=has_data,
            source="lancedb",
            missing_parts=[] if has_data else ["has_sentiment"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get sentiment score: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取情绪评分失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=target_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/sentiment/score/history', methods=['GET'])
def get_sentiment_score_history():
    """
    获取综合情绪评分历史（统一响应）

    Query Parameters:
        - start_date: 开始日期 (YYYY-MM-DD)
        - end_date: 结束日期 (YYYY-MM-DD)

    Returns:
        success.data: {"history": [...], "count": int}
    """
    start_ms = _now_ms()
    endpoint = request.path
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if not start_date or not end_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 start_date 或 end_date",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        history = service.get_sentiment_score_history(start_date, end_date) or []
        payload = {"history": history, "count": len(history)}
        has_data = len(history) > 0
        return jsonify(_success(
            payload,
            target_date=end_date,
            has_data=has_data,
            source="lancedb",
            missing_parts=[] if has_data else ["has_sentiment"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get sentiment score history: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取情绪历史失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=end_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/brief', methods=['GET'])
def get_ai_brief():
    """
    获取 AI 简报（统一响应）

    Query Parameters:
        - date: 日期 (YYYY-MM-DD)

    Returns:
        success.data: {"date", "content", "has_data"}
    """
    start_ms = _now_ms()
    endpoint = request.path
    target_date = request.args.get('date')

    if not target_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 date 参数",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        brief = service.get_latest_brief(target_date) or ""
        payload = {
            "date": target_date,
            "content": brief,
            "has_data": bool(brief),
        }
        return jsonify(_success(
            payload,
            target_date=target_date,
            has_data=bool(brief),
            source="local_spider",
            missing_parts=[] if brief else ["has_brief"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get brief: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取 AI 简报失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=target_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


# ==========================================
# 飞书推送接口
# ==========================================

@dragon_bp.route('/push', methods=['POST'])
def push_to_feishu():
    """
    推送到飞书（统一响应）

    Request Body:
        - date: 日期 (YYYY-MM-DD)

    Returns:
        success.data: {"status", "message"}
    """
    start_ms = _now_ms()
    endpoint = request.path
    data = request.json or {}
    target_date = data.get('date')

    if not target_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 date 参数",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        success_flag, msg = service.send_to_feishu(target_date)
        if not success_flag:
            body, status = _failure(
                DragonEyeErrorCode.PUSH_FAILED, f"飞书推送失败: {msg}",
                detail=msg, retryable=True, status_code=503,
                endpoint=endpoint, target_date=target_date,
                elapsed_ms=_elapsed_ms(start_ms),
            )
            return jsonify(body), status
        payload = {"status": "success", "message": msg}
        return jsonify(_success(
            payload,
            target_date=target_date,
            has_data=True,
            source="local_spider",
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to push to feishu: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"飞书推送异常: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=target_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/confirm', methods=['POST'])
def confirm_and_push():
    """确认数据并推送到飞书（兼容旧接口，转发到 /push）。"""
    return push_to_feishu()


# ==========================================
# 数据可视化分析接口
# ==========================================

@dragon_bp.route('/limit-up-trend', methods=['GET'])
def get_limit_up_trend():
    """
    获取涨停趋势（统一响应）

    Query Parameters:
        - start_date: 开始日期 (YYYY-MM-DD)
        - end_date: 结束日期 (YYYY-MM-DD)

    Returns:
        success.data: {"dates", "limit_up_counts", "max_heights",
                       "broken_ratios", "limit_down_counts"}
    """
    start_ms = _now_ms()
    endpoint = request.path
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if not start_date or not end_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 start_date 或 end_date",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        df = service.manager.get_market_sentiment(start_date, end_date)
        if df is None or df.is_empty():
            payload = {
                "dates": [], "limit_up_counts": [], "max_heights": [],
                "broken_ratios": [], "limit_down_counts": [],
            }
            return jsonify(_success(
                payload,
                target_date=end_date,
                has_data=False,
                source="lancedb",
                missing_parts=["has_ladder", "has_limit_up"],
                elapsed_ms=_elapsed_ms(start_ms),
                endpoint=endpoint,
            ))

        df = df.sort('trade_date')
        dates = []
        if 'trade_date' in df.columns:
            try:
                dates = df['trade_date'].dt.strftime('%Y-%m-%d').to_list()
            except Exception:
                dates = [str(d) for d in df['trade_date'].to_list()]

        def _col(name: str) -> list:
            if name in df.columns:
                return df[name].to_list()
            return []

        payload = {
            "dates": dates,
            "limit_up_counts": _col("limit_up_count"),
            "max_heights": _col("max_height"),
            "broken_ratios": _col("broken_ratio"),
            "limit_down_counts": _col("limit_down_count"),
        }
        has_data = len(dates) > 0
        return jsonify(_success(
            payload,
            target_date=end_date,
            has_data=has_data,
            source="lancedb",
            missing_parts=[] if has_data else ["has_ladder"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get limit up trend: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取涨停趋势失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=end_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/bubble-matrix', methods=['GET'])
def get_bubble_matrix():
    """
    获取涨停强度气泡图数据（统一响应）

    Query Parameters:
        - date: 日期 (YYYY-MM-DD)

    Returns:
        success.data: {"date", "bubbles": [...], "quadrant_labels": {...}}
    """
    start_ms = _now_ms()
    endpoint = request.path
    target_date = request.args.get('date')

    if not target_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 date 参数",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        result = service.get_bubble_matrix_data(target_date) or {}
        bubbles = result.get("bubbles", []) if isinstance(result, dict) else []
        payload = {
            "date": result.get("date", target_date) if isinstance(result, dict) else target_date,
            "bubbles": bubbles,
            "quadrant_labels": result.get("quadrant_labels", {}) if isinstance(result, dict) else {},
        }
        has_data = len(bubbles) > 0
        return jsonify(_success(
            payload,
            target_date=target_date,
            has_data=has_data,
            source="local_spider",
            missing_parts=[] if has_data else ["has_ladder", "has_limit_up"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get bubble matrix: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取气泡矩阵失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=target_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/theme-flow', methods=['GET'])
def get_theme_flow():
    """
    获取题材流向数据（统一响应）

    Query Parameters:
        - start_date: 开始日期 (YYYY-MM-DD)
        - end_date: 结束日期 (YYYY-MM-DD)

    Returns:
        success.data: {"nodes": [...], "links": [...]}
    """
    start_ms = _now_ms()
    endpoint = request.path
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if not start_date or not end_date:
        body, status = _failure(
            DragonEyeErrorCode.BAD_REQUEST, "缺少 start_date 或 end_date",
            retryable=False, status_code=400,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status

    try:
        service = get_dragon_service()
        flow = service.get_theme_flow_data(start_date, end_date) or {}
        nodes = flow.get("nodes", []) if isinstance(flow, dict) else []
        links = flow.get("links", []) if isinstance(flow, dict) else []
        payload = {"nodes": nodes, "links": links}
        has_data = len(nodes) > 0
        return jsonify(_success(
            payload,
            target_date=end_date,
            has_data=has_data,
            source="local_spider",
            missing_parts=[] if has_data else ["has_theme_flow"],
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get theme flow: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取题材流向失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, target_date=end_date,
            elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status


@dragon_bp.route('/latest-date', methods=['GET'])
def get_latest_date():
    """
    获取数据库中最新的数据日期（统一响应）

    Returns:
        success.data: {"latest_date", "has_data"}
    """
    start_ms = _now_ms()
    endpoint = request.path

    try:
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.now()
        start = end - _td(days=30)
        service = get_dragon_service()
        df = service.manager.get_market_sentiment(
            start.strftime('%Y-%m-%d'),
            end.strftime('%Y-%m-%d'),
        )
        if df is None or df.is_empty():
            payload = {"latest_date": None, "has_data": False}
            return jsonify(_success(
                payload,
                has_data=False,
                source="lancedb",
                missing_parts=["has_sentiment"],
                elapsed_ms=_elapsed_ms(start_ms),
                endpoint=endpoint,
            ))

        latest_date = df['trade_date'].max()
        if hasattr(latest_date, 'strftime'):
            latest_date = latest_date.strftime('%Y-%m-%d')
        else:
            latest_date = str(latest_date)
        payload = {"latest_date": latest_date, "has_data": True}
        return jsonify(_success(
            payload,
            latest_date=latest_date,
            has_data=True,
            source="lancedb",
            elapsed_ms=_elapsed_ms(start_ms),
            endpoint=endpoint,
        ))
    except Exception as e:
        logger.error(f"Failed to get latest date: {e}")
        code, retryable, status_code = _error_code_for(e)
        body, status = _failure(
            code, f"获取最新日期失败: {e}", detail=str(e),
            retryable=retryable, status_code=status_code,
            endpoint=endpoint, elapsed_ms=_elapsed_ms(start_ms),
        )
        return jsonify(body), status
