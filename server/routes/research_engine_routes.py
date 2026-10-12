"""Research Engine 执行入口。

提供 HTTP 接口触发真实研究流程，调用 sandbox.run_real_research_pipeline 的子流程。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request

from core.research.control_plane.contracts import ResearchTask, TaskStatus, utc_now
from core.research.control_plane.repository import ResearchRepository

research_engine_bp = Blueprint('research_engine', __name__, url_prefix='/api/research/engine')


@research_engine_bp.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({'status': 'ok', 'module': 'research_engine'})


@research_engine_bp.route('/run', methods=['POST'])
def run_research():
    """触发研究执行。

    请求体可选字段：
    - topic: 研究主题（如 "HBM"）
    - task_id: 关联的任务 ID
    - max_rounds: 最大闭环轮数（默认 1）

    返回：
    - 202 Accepted + task_id（异步执行）
    """
    payload = request.get_json(silent=True) or {}
    topic = str(payload.get("topic") or "")
    task_id = str(payload.get("task_id") or "")
    max_rounds = int(payload.get("max_rounds") or 1)

    if not topic:
        return jsonify({"ok": False, "error": {"code": "BAD_REQUEST", "message": "topic 必填"}}), 400

    # 异步执行
    def _run() -> None:
        try:
            # 确保 sandbox 在 sys.path
            project_root = Path(__file__).resolve().parents[2]
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            from sandbox.run_real_research_pipeline import main as run_pipeline
            # main() 通过 argparse 解析 sys.argv，需显式设置
            sys.argv = ["run_real_research_pipeline.py", "--topic", topic, "--max-rounds", str(max_rounds)]
            run_pipeline()
        except Exception as exc:
            # 错误记录到任务（如果 task_id 存在）
            if task_id:
                try:
                    repo = ResearchRepository()
                    task = repo.get_task(task_id)
                    if task:
                        task.status = TaskStatus.FAILED.value
                        task.finished_at = utc_now()
                        task.failure = {"reason": str(exc)}
                        repo.save_task(task)
                except Exception:
                    pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"ok": True, "data": {"topic": topic, "task_id": task_id, "status": "running"}}), 202


@research_engine_bp.route('/status/<task_id>', methods=['GET'])
def get_status(task_id: str):
    """查询任务执行状态"""
    repo = ResearchRepository()
    task = repo.get_task(task_id)
    if task is None:
        return jsonify({"ok": False, "error": {"code": "NOT_FOUND", "message": "任务不存在"}}), 404
    return jsonify({"ok": True, "data": task.to_dict()})
