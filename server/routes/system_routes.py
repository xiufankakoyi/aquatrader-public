"""
系统相关路由
"""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from flask import Blueprint, jsonify, request, Response, stream_with_context
from server.utils.system import _schedule_restart

system_bp = Blueprint('system', __name__, url_prefix='/api')
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _consume_status_path() -> Path:
    return Path(
        os.getenv(
            "AQUATRADE_MANUAL_CONSUME_STATUS_FILE",
            str(PROJECT_ROOT / "data" / "state" / "research_manual_consume.json"),
        )
    )


def _write_consume_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _execute_research_consume(
    *,
    task_id: str,
    workers: int,
    batch: int,
    status_path: Path,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run one bounded queue-drain job and persist its real outcome."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    running = {
        "task_id": task_id,
        "status": "running",
        "workers": workers,
        "batch": batch,
        "started_at": started_at,
    }
    _write_consume_status(status_path, running)
    command = [
        sys.executable,
        str(PROJECT_ROOT / "sandbox" / "drain_search_tasks.py"),
        "--workers",
        str(workers),
        "--batch",
        str(batch),
        "--rounds",
        "1",
    ]
    try:
        completed = runner(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        return_code = int(completed.returncode)
        result = {
            **running,
            "status": "succeeded" if return_code == 0 else "failed",
            "return_code": return_code,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if return_code != 0:
            result["error"] = str(completed.stderr or completed.stdout or "")[-2000:]
    except Exception as exc:
        result = {
            **running,
            "status": "failed",
            "return_code": None,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}",
        }
    _write_consume_status(status_path, result)
    return result


@system_bp.route('/', methods=['GET'])
def index():
    """根路径路由"""
    response = jsonify({"success": True, "message": "服务器正在运行"})
    response.headers.add('Content-Type', 'application/json')
    return response


@system_bp.route('/direct_test', methods=['GET'])
def direct_test():
    """全新的、完全独立的测试端点，用于调试路由和响应处理"""
    test_data = {"success": True, "data": [{"id": "test1", "name": "测试数据1"}]}
    return jsonify(test_data)


@system_bp.route('/restart-backend', methods=['POST'])
def restart_backend():
    """
    触发后端自我重启：立即给前端成功响应，再在后台调用 os.execl 重新拉起进程。
    """
    _schedule_restart()
    return jsonify({"success": True, "message": "后端正在重启，请稍候 1-2 秒"}), 202


@system_bp.route('/startup-status', methods=['GET'])
def get_startup_status():
    """
    获取启动状态
    返回三阶段自检的当前进度
    """
    from server.services.startup_service import get_startup_service
    
    service = get_startup_service()
    return jsonify(service.get_status())


@system_bp.route('/startup-logs', methods=['GET'])
def stream_startup_logs():
    """
    SSE 流式推送启动日志
    前端通过 EventSource 连接，实时接收日志更新
    """
    from server.services.startup_service import get_startup_service
    
    service = get_startup_service()
    
    def generate():
        last_log_count = 0
        last_status = None
        max_iterations = 300
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            status = service.get_status()
            
            if status != last_status:
                last_status = status
                yield f"data: {json.dumps(status)}\n\n"
            
            if status.get('ready') or status.get('error_code'):
                break
            
            time.sleep(0.1)
        
        yield f"data: {json.dumps({'event': 'close'})}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@system_bp.route('/startup-begin', methods=['POST'])
def begin_startup():
    """
    触发启动检查
    前端加载完成后调用此接口开始后台自检
    """
    from server.services.startup_service import get_startup_service
    
    service = get_startup_service()
    
    if service.is_ready():
        return jsonify({"success": True, "message": "系统已就绪", "status": service.get_status()})
    
    if service.has_error():
        return jsonify({"success": False, "message": "启动检查失败", "status": service.get_status()})
    
    service.start_async()
    
    return jsonify({"success": True, "message": "启动检查已开始", "status": service.get_status()})


@system_bp.route('/research-daemon/status', methods=['GET'])
def research_daemon_status():
    """获取 ResearchMonitor daemon 运行状态与任务队列进度。

    返回：
    - daemon 状态文件内容（各 mode 当日完成情况）
    - 任务队列统计（queued / succeeded / failed 数量）
    - 日线数据最新日期
    """
    import json as _json
    from pathlib import Path as _Path

    payload: dict = {"success": True, "data": {}}

    # 1. daemon 状态文件
    state_path = _Path(
        __import__('os').getenv(
            "AQUATRADE_DAEMON_STATE_FILE",
            "data/state/research_monitor_daemon.json",
        )
    )
    daemon_state: dict = {}
    if state_path.exists():
        try:
            daemon_state = _json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload["data"]["daemon_state"] = daemon_state

    # 2. 任务队列统计
    try:
        from core.research.control_plane.repository import ResearchRepository
        repo = ResearchRepository()
        stats = repo.stats()
        payload["data"]["task_stats"] = stats
        # 按状态细分
        all_tasks = repo.list_tasks(limit=1000)
        from collections import Counter
        status_dist = Counter(t.status for t in all_tasks)
        payload["data"]["task_status_distribution"] = dict(status_dist)
        payload["data"]["task_total_fetched"] = len(all_tasks)
    except Exception as exc:
        payload["data"]["task_stats"] = {"error": str(exc)}

    try:
        from core.research.control_plane.learning_loop import (
            ProductionLearningLoop,
        )
        from core.research.control_plane.repository import ResearchRepository
        learning_repo = ResearchRepository()
        payload["data"]["learning_stats"] = ProductionLearningLoop(
            learning_repo
        ).stats(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as exc:
        payload["data"]["learning_stats"] = {"error": str(exc)}

    # 3. 日线最新日期
    try:
        from data_svc.unified_data_query import get_unified_data_query
        query = get_unified_data_query()
        db_stats = query.get_db_stats()
        payload["data"]["daily_latest_date"] = db_stats.get("latest_date")
    except Exception as exc:
        payload["data"]["daily_latest_date"] = None
        payload["data"]["daily_error"] = str(exc)

    # 4. PID identity + heartbeat freshness. A PID alone can be stale or reused.
    from core.runtime.research_daemon_health import inspect_daemon_health

    pid_path = _Path(
        __import__("os").getenv(
            "AQUATRADE_DAEMON_PID_FILE",
            str(state_path.with_suffix(".pid")),
        )
    )
    payload["data"].update(
        inspect_daemon_health(state_path, pid_path=pid_path)
    )

    manual_status_path = _consume_status_path()
    try:
        payload["data"]["manual_consume"] = json.loads(
            manual_status_path.read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError):
        payload["data"]["manual_consume"] = {"status": "never_run"}

    return jsonify(payload)


@system_bp.route('/research-daemon/consume', methods=['POST'])
def research_daemon_consume():
    """手动触发一轮 queued 任务消费（前端按钮调用）。

    请求体可选：
    - workers: 并发数（默认 8）
    - batch: 批大小（默认 30）
    """
    payload = request.get_json(silent=True) or {}
    try:
        workers = int(payload.get("workers", 8))
        batch = int(payload.get("batch", 30))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message": "workers 和 batch 必须为整数",
        }), 400
    if not 1 <= workers <= 32 or not 1 <= batch <= 500:
        return jsonify({
            "success": False,
            "message": "workers 必须为 1-32，batch 必须为 1-500",
        }), 400

    task_id = f"manual_consume_{uuid4().hex[:16]}"
    status_path = _consume_status_path()
    _write_consume_status(status_path, {
        "task_id": task_id,
        "status": "accepted",
        "workers": workers,
        "batch": batch,
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })

    def _consume() -> None:
        _execute_research_consume(
            task_id=task_id,
            workers=workers,
            batch=batch,
            status_path=status_path,
        )

    thread = threading.Thread(target=_consume, daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "message": f"已受理消费 workers={workers} batch={batch}",
        "data": {
            "task_id": task_id,
            "status": "accepted",
            "workers": workers,
            "batch": batch,
        },
    }), 202
