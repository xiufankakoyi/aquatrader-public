"""
Perilla 知识闭环 API

提供前端 KnowledgeGraphPage 所需的数据接口：
- 证据边图谱
- 候选概念
- 原始术语
- 今日新词 / 日报
- 调度器状态
- 触发日报生成
- 知识链增长过程与 SAG 最终结果
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

perilla_bp = Blueprint("perilla", __name__, url_prefix="/api/perilla")


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent.parent


def _reports_dir() -> Path:
    """返回 Perilla 报告输出目录。"""
    return _project_root() / "data" / "reports" / "perilla_discovery"


def _read_json(filename: str, default: Any | None = None) -> Any:
    """读取报告目录下的 JSON 文件。"""
    path = _reports_dir() / filename
    if not path.exists():
        return default if default is not None else []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 %s 失败: %s", filename, exc)
        return default if default is not None else []


def _read_text(filename: str, default: str = "") -> str:
    """读取报告目录下的文本文件。"""
    path = _reports_dir() / filename
    if not path.exists():
        return default
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("读取 %s 失败: %s", filename, exc)
        return default


@perilla_bp.route("/health", methods=["GET"])
def health() -> Any:
    """健康检查"""
    return jsonify({"status": "ok", "module": "perilla"})


@perilla_bp.route("/evidence-edges", methods=["GET"])
def evidence_edges() -> Any:
    """获取证据边图谱（ECharts graph 格式）。"""
    limit = request.args.get("limit", type=int) or 200
    relation_filter = request.args.get("relation", "", type=str).strip()
    min_evidence = request.args.get("min_evidence", 0, type=int)

    edges = _read_json("evidence_edges_latest.json", [])
    nodes_map: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    categories_map: dict[str, int] = {}

    for edge in edges:
        rel = edge.get("relation", "")
        if relation_filter and rel != relation_filter:
            continue
        evidence_quotes = edge.get("evidence_quotes") or []
        evidence_count = len(evidence_quotes)
        if evidence_count < min_evidence:
            continue
        if len(links) >= limit:
            break

        head = edge.get("head", "")
        tail = edge.get("tail", "")
        head_type = edge.get("head_type", "unknown")
        tail_type = edge.get("tail_type", "unknown")

        for name, node_type in ((head, head_type), (tail, tail_type)):
            if name not in nodes_map:
                category = node_type
                if category not in categories_map:
                    categories_map[category] = len(categories_map)
                nodes_map[name] = {
                    "name": name,
                    "category": categories_map[category],
                    "value": 0,
                    "symbolSize": 20,
                }
            nodes_map[name]["value"] += 1

        links.append({
            "source": head,
            "target": tail,
            "relation": rel,
            "confidence_level": edge.get("confidence_level", "medium"),
            "evidence_count": evidence_count,
            "evidence_quotes": evidence_quotes[:3],
        })

    # 根据连接数调整节点大小
    nodes = []
    for node in nodes_map.values():
        node["symbolSize"] = min(60, 15 + node["value"] * 3)
        nodes.append(node)

    categories = [{"name": name} for name, _ in sorted(categories_map.items(), key=lambda x: x[1])]

    return jsonify({
        "data": {"nodes": nodes, "links": links, "categories": categories},
        "meta": {
            "nodes": len(nodes),
            "filtered_edges": len(links),
            "total_edges": len(edges),
        },
    })


@perilla_bp.route("/candidate-concepts", methods=["GET"])
def candidate_concepts() -> Any:
    """获取候选概念列表。"""
    limit = request.args.get("limit", 50, type=int)
    root_theme = request.args.get("root_theme", "", type=str).strip()

    concepts = _read_json("candidate_concepts_latest.json", [])
    if root_theme:
        concepts = [c for c in concepts if c.get("root_theme") == root_theme]
    return jsonify({"data": concepts[:limit], "meta": {"total": len(concepts)}})


@perilla_bp.route("/raw-terms", methods=["GET"])
def raw_terms() -> Any:
    """获取原始术语列表。"""
    limit = request.args.get("limit", 100, type=int)
    terms = _read_json("raw_terms_latest.json", [])
    return jsonify({"data": terms[:limit], "meta": {"total": len(terms)}})


@perilla_bp.route("/new-terms-today", methods=["GET"])
def new_terms_today() -> Any:
    """获取今日新词 markdown。"""
    markdown = _read_text("perilla_new_terms_today.md")
    path = _reports_dir() / "perilla_new_terms_today.md"
    size = path.stat().st_size if path.exists() else 0
    return jsonify({
        "data": {
            "markdown": markdown,
            "generated_at": _file_mtime(path),
            "size_bytes": size,
        }
    })


@perilla_bp.route("/daily-brief", methods=["GET"])
def daily_brief() -> Any:
    """获取日报 markdown。"""
    brief_path = _project_root() / "data" / "reports" / "daily_brief.md"
    fallback_path = _reports_dir() / "perilla_new_terms_today.md"

    if brief_path.exists():
        markdown = brief_path.read_text(encoding="utf-8")
        path = brief_path
    else:
        markdown = _read_text("perilla_new_terms_today.md")
        path = fallback_path

    size = path.stat().st_size if path.exists() else 0
    return jsonify({
        "data": {
            "markdown": markdown,
            "generated_at": _file_mtime(path),
            "size_bytes": size,
        }
    })


@perilla_bp.route("/scheduler-status", methods=["GET"])
def scheduler_status() -> Any:
    """获取日报调度器状态。"""
    try:
        from core.tasks.task_store import TaskStore

        store = TaskStore()
        tasks = store.list_tasks(task_type="run_daily_brief", limit=5)
        last = tasks[0] if tasks else None
        last_status = "idle"
        last_error = ""
        last_summary = None
        running = False
        if last:
            last_status = last.status
            last_error = last.error or ""
            last_summary = last.result
            if last_status == "running":
                running = True

        output_mtime = _file_mtime(_reports_dir() / "perilla_new_terms_today.md")
        return jsonify({
            "data": {
                "enabled": True,
                "started": True,
                "running": running,
                "run_on_startup": False,
                "startup_delay_seconds": 0,
                "schedule": {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai", "skip_weekends": False},
                "next_run_at": None,
                "last_run_at": last.created_at if last else output_mtime,
                "last_finished_at": last.finished_at if last else None,
                "last_status": last_status,
                "last_error": last_error,
                "last_summary": last_summary,
            }
        })
    except Exception as exc:
        logger.warning("读取调度器状态失败: %s", exc)
        return jsonify({
            "data": {
                "enabled": True,
                "started": True,
                "running": False,
                "schedule": {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai", "skip_weekends": False},
                "last_status": "idle",
            }
        })


@perilla_bp.route("/run-daily-brief", methods=["POST"])
def run_daily_brief() -> Any:
    """触发日报生成任务。"""
    try:
        from core.tasks.task_runner import TaskRunner

        params = request.get_json(silent=True) or {}
        runner = TaskRunner()
        task_id = runner.submit(
            task_type="run_daily_brief",
            params={
                "execute_search": bool(params.get("execute_search", True)),
                "use_llm": bool(params.get("use_llm", True)),
                "skip_perilla": bool(params.get("skip_perilla", False)),
                "skip_research": bool(params.get("skip_research", False)),
            },
            source="web",
        )
        return jsonify({"data": {"task_id": task_id, "status": "queued"}})
    except Exception as exc:
        logger.warning("触发日报任务失败: %s", exc)
        return jsonify({"error": str(exc)}), 500


@perilla_bp.route("/growth-process", methods=["GET"])
def growth_process() -> Any:
    """获取知识链增长过程与最终结果。"""
    raw_sources = _read_json("raw_sources_latest.json", [])
    raw_documents = _read_json("raw_documents_latest.json", [])
    raw_terms = _read_json("raw_terms_latest.json", [])
    candidate_concepts = _read_json("candidate_concepts_latest.json", [])
    evidence_edges = _read_json("evidence_edges_latest.json", [])
    search_tasks = _read_json("search_tasks_latest.json", [])
    task_runs = _read_json("search_task_runs_latest.json", [])
    sag_stats = _read_json("sag_stats_latest.json", {})
    promotion_candidates = _read_json("promotion_candidates_latest.json", [])
    selected_new_term = _read_json("selected_new_term_latest.json", {})

    # 按来源统计文档
    doc_source_count: dict[str, int] = {}
    for doc in raw_documents:
        sid = doc.get("source_id", "unknown")
        doc_source_count[sid] = doc_source_count.get(sid, 0) + 1

    # 按类型统计术语
    term_type_count: dict[str, int] = {}
    for term in raw_terms:
        tt = term.get("term_type", "unknown")
        term_type_count[tt] = term_type_count.get(tt, 0) + 1

    # 按关系统计证据边
    relation_count: dict[str, int] = {}
    for edge in evidence_edges:
        rel = edge.get("relation", "unknown")
        relation_count[rel] = relation_count.get(rel, 0) + 1

    completed_tasks = sum(1 for t in search_tasks if t.get("status") == "completed")
    pending_tasks = len(search_tasks) - completed_tasks

    layers = [
        {
            "layer": 1,
            "title": "采集源接入",
            "count": len(raw_sources),
            "detail": f"已接入 {len(raw_sources)} 个数据源",
            "status": "done" if raw_sources else "pending",
        },
        {
            "layer": 2,
            "title": "原始文档采集",
            "count": len(raw_documents),
            "detail": f"共 {len(raw_documents)} 条原始文档",
            "breakdown": doc_source_count,
            "status": "done" if raw_documents else "pending",
        },
        {
            "layer": 3,
            "title": "新词雷达",
            "count": len(raw_terms),
            "detail": f"发现 {len(raw_terms)} 个新术语",
            "breakdown": term_type_count,
            "status": "done" if raw_terms else "pending",
        },
        {
            "layer": 4,
            "title": "候选概念生成",
            "count": len(candidate_concepts),
            "detail": f"生成 {len(candidate_concepts)} 个候选概念",
            "status": "done" if candidate_concepts else "pending",
        },
        {
            "layer": 5,
            "title": "证据边构建",
            "count": len(evidence_edges),
            "detail": f"构建 {len(evidence_edges)} 条证据边",
            "breakdown": relation_count,
            "status": "done" if evidence_edges else "pending",
        },
        {
            "layer": 6,
            "title": "搜索任务调度",
            "count": len(search_tasks),
            "detail": f"{completed_tasks} 已完成 / {pending_tasks} 待执行",
            "status": "running" if pending_tasks > 0 else "done",
        },
        {
            "layer": 7,
            "title": "SAG 写入与晋升候选",
            "count": sag_stats.get("ingested", 0),
            "detail": (
                f"写入 SAG {sag_stats.get('ingested', 0)} 个概念，"
                f"生成 {len(promotion_candidates)} 个晋升候选"
            ),
            "status": "done" if sag_stats.get("ingested", 0) > 0 else "pending",
        },
    ]

    return jsonify({
        "data": {
            "layers": layers,
            "sag_stats": sag_stats,
            "promotion_candidates": promotion_candidates[:20],
            "selected_new_term": selected_new_term,
            "task_runs": task_runs[:10],
            "generated_at": _file_mtime(_reports_dir() / "candidate_concepts_latest.json"),
        }
    })


@perilla_bp.route("/sag-stats", methods=["GET"])
def sag_stats() -> Any:
    """获取 SAG 集成统计与晋升候选。"""
    stats = _read_json("sag_stats_latest.json", {})
    candidates = _read_json("promotion_candidates_latest.json", [])
    return jsonify({
        "data": {
            "sag_stats": stats,
            "promotion_candidates": candidates[:50],
        }
    })


def _file_mtime(path: Path) -> str:
    """返回文件修改时间 ISO 字符串，不存在则返回空。"""
    if not path.exists():
        return ""
    try:
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return ""
