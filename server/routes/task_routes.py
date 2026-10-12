"""旧 Task API 兼容层；新调用应使用 /api/research/tasks。"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from server.routes.research_routes import get_research_repository

task_bp = Blueprint("task", __name__, url_prefix="/api/task")


def _response(data):
    return jsonify({
        "data": data,
        "meta": {"deprecated": True, "replacement": "/api/research/tasks"},
    })


@task_bp.get("/health")
def health():
    return _response({"status": "ok", "module": "task_compat"})


@task_bp.get("/")
def list_items():
    repo = get_research_repository()
    items = repo.list_tasks(request.args.get("status", ""), request.args.get("limit", 100, type=int))
    return _response([item.to_dict() for item in items])


@task_bp.get("/<task_id>")
def detail(task_id: str):
    item = get_research_repository().get_task(task_id)
    if item is None:
        return jsonify({"error": {"code": "TASK_NOT_FOUND", "message": "任务不存在"}}), 404
    return _response(item.to_dict())
