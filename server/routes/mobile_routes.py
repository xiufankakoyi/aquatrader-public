"""移动端数据路由。

提供移动端首页摘要、日报、收件箱、观察池、任务、报告和命令 API。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

mobile_bp = Blueprint("mobile", __name__, url_prefix="/api/mobile")


@mobile_bp.route("/summary", methods=["GET"])
def get_summary():
    """获取移动端首页摘要。

    返回:
        {
            "success": true,
            "data": {
                "date": "...",
                "latest_daily_brief": "...",
                "top_candidates": [...],
                "inbox_stats": {...},
                "watchlist_stats": {...},
                "task_stats": {...},
                "last_updated": "..."
            }
        }
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_summary()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/daily", methods=["GET"])
def get_daily():
    """获取紫苏叶日报。
    
    返回:
        {
            "success": true,
            "data": {
                "daily_brief": {...},
                "perilla_discovery": {...},
                "raw_terms_section": {...},
                ...
            }
        }
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_daily()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/daily/refresh", methods=["POST"])
def refresh_daily():
    """刷新紫苏叶日报（触发流水线重新运行）。
    
    返回:
        {"success": true, "message": "日报刷新任务已提交"}
    """
    try:
        from core.tasks.task_runner import TaskRunner
        runner = TaskRunner()
        task_id = runner.submit(
            task_type="daily_brief",
            params={"execute_search": True, "use_llm": True},
            source="mobile_api",
        )
        return jsonify({
            "success": True,
            "message": "日报刷新任务已提交",
            "task_id": task_id,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/perilla", methods=["GET"])
def get_perilla():
    """获取紫苏叶新词雷达数据。
    
    返回:
        {
            "success": true,
            "data": {
                "raw_terms": [...],
                "candidate_concepts": [...],
                "evidence_edges": [...]
            }
        }
    """
    import json
    from pathlib import Path
    
    try:
        base_dir = Path("data/reports/perilla_discovery")
        
        def load_json(filename):
            path = base_dir / filename
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            return []
        
        return jsonify({
            "success": True,
            "data": {
                "raw_terms": load_json("raw_terms_latest.json"),
                "candidate_concepts": load_json("candidate_concepts_latest.json"),
                "evidence_edges": load_json("evidence_edges_latest.json"),
                "concept_scores": load_json("concept_scores_latest.json"),
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/inbox", methods=["GET"])
def get_inbox():
    """获取收件箱列表。

    查询参数:
        status: 状态过滤
        classification: 分类过滤
        limit: 返回数量上限（默认 50）
        offset: 分页偏移（默认 0）
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_inbox(
            status=request.args.get("status"),
            classification=request.args.get("classification"),
            limit=int(request.args.get("limit", 50)),
            offset=int(request.args.get("offset", 0)),
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/chain", methods=["GET"])
def get_chain():
    """获取基于收件箱真实数据的产业链拓扑图。

    查询参数:
        concept: 指定概念名称（如 "光互连"）；为空时使用节点数最多的主概念

    返回:
        {
            "success": true,
            "data": {
                "nodes": [...],
                "links": [...],
                "layers": [...],
                "summary": {...},
                "data_date": "...",
                "source": "inbox_items.json",
                "has_data": true,
                "demo_mode": false
            }
        }
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        concept = request.args.get("concept", "").strip() or None
        result = service.get_chain_from_inbox(concept=concept)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/chain/concepts", methods=["GET"])
def get_chain_concepts():
    """列出收件箱中可用于产业链展示的所有真实概念。

    返回:
        {
            "success": true,
            "data": [
                {"chain_id": "inbox::光互连", "name": "光互连", "node_count": 9, "stock_count": 27, "max_score": 75.0, "source": "inbox_items.json"},
                ...
            ]
        }
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.list_inbox_chain_concepts()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/watchlist", methods=["GET"])
def get_watchlist():
    """获取观察池列表。

    查询参数:
        classification: 分类过滤
        min_priority: 最低优先级分数
        limit: 返回数量上限（默认 50）
        offset: 分页偏移（默认 0）
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_watchlist(
            classification=request.args.get("classification"),
            min_priority=_parse_float(request.args.get("min_priority")),
            limit=int(request.args.get("limit", 50)),
            offset=int(request.args.get("offset", 0)),
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/tasks", methods=["GET"])
def get_tasks():
    """获取任务列表。

    查询参数:
        status: 状态过滤
        limit: 返回数量上限（默认 50）
        offset: 分页偏移（默认 0）
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_tasks(
            status=request.args.get("status"),
            limit=int(request.args.get("limit", 50)),
            offset=int(request.args.get("offset", 0)),
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/tasks/<task_id>", methods=["GET"])
def get_task(task_id: str):
    """获取单个任务详情。"""
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_task(task_id)
        if result is None:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/reports", methods=["GET"])
def get_reports():
    """获取可用报告列表。"""
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_reports()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/reports/<path:report_id>", methods=["GET"])
def get_report(report_id: str):
    """获取单个报告内容。

    Args:
        report_id: 报告标识，相对于 data/reports/ 的路径
    """
    try:
        from server.services.research.mobile_research_service import MobileResearchService
        service = MobileResearchService()
        result = service.get_report(report_id)
        if result is None:
            return jsonify({"success": False, "error": "报告不存在或路径无效"}), 404
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/command", methods=["POST"])
def submit_command():
    """接收移动端命令并提交为后台任务。

    请求体:
        {
            "command": "scan <topic>" | "daily" | "refresh",
            "params": {...},
            "confirm": false
        }
    """
    try:
        data = request.get_json() or {}
        command = (data.get("command") or "").strip().lower()
        params = data.get("params") or {}

        # 命令映射到 task_type
        command_map = {
            "daily": ("run_daily_brief", {"execute_search": True, "use_llm": True}),
            "refresh": ("refresh_watchlist", {}),
        }

        # 支持 "scan <topic>" 语法
        task_type: str | None = None
        task_params: dict = {}
        if command.startswith("scan "):
            topic = command[5:].strip()
            if not topic:
                return jsonify({"success": False, "error": "scan 命令缺少 topic"}), 400
            task_type = "run_perilla_topic_fast"
            task_params = {"topic": topic}
        elif command in command_map:
            task_type, task_params = command_map[command]
        else:
            return jsonify({"success": False, "error": f"未知命令: {command}"}), 400

        from core.tasks.task_runner import TaskRunner
        runner = TaskRunner()
        merged_params = {**task_params, **params}
        task_id = runner.submit(task_type=task_type, params=merged_params, source="mobile_web")
        return jsonify({
            "success": True,
            "data": {
                "accepted": True,
                "task_id": task_id,
                "requires_confirmation": False,
                "confirmation_token": None,
                "message": f"命令已提交，任务 ID: {task_id}",
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_bp.route("/command/confirm", methods=["POST"])
def confirm_command():
    """确认执行需要二次确认的命令（当前移动端命令均无需确认，直接返回成功）。"""
    return jsonify({"success": True, "data": {"accepted": True, "message": "已确认"}})


def _parse_float(value: str | None) -> float | None:
    """安全解析浮点查询参数。"""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
