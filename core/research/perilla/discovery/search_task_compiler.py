"""P3 搜索任务编译器。

从 missing_edges 编译 search_tasks，每条 missing_edge 生成 3-5 条 query。

输出到 search_tasks_from_missing_edges_latest.json（不直接写
search_tasks_latest.json，由 P8 聚合，避免覆盖未来多来源任务）。

search_task 含 task_source / generated_by 字段，为 P8 多来源聚合预留。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_INPUT_DIR = Path("data/reports/perilla_discovery")
_DEFAULT_OUTPUT_DIR = Path("data/reports/perilla_discovery")

# 按 source_type 的 query 模板（每类 3-5 条）
_QUERY_TEMPLATES: Dict[str, List[str]] = {
    "product_platform": [
        "{node} 产业链 下游 {target}",
        "{node} {target} 需求 供应商",
        "{node} {target} A股 相关公司",
        "{node} {target} 订单 产能",
    ],
    "technology_route": [
        "{node} 技术路线 {target} 厂商",
        "{node} {target} 量产 进展",
        "{node} {target} 客户 导入",
        "{node} {target} 技术对比 竞争",
    ],
    "component": [
        "{node} {target} 供应商 认证",
        "{node} {target} 市场份额 竞争格局",
        "{node} {target} 价格 涨价",
    ],
    "material": [
        "{node} {target} 产能 扩产",
        "{node} {target} 价格 涨价 供给",
        "{node} {target} 上游 原料 供应商",
        "{node} {target} 国产替代 进口",
    ],
}

# query_type 映射
_QUERY_TYPE_MAP: Dict[str, str] = {
    "product_platform": "product_downstream",
    "technology_route": "tech_downstream",
    "component": "component_supplier",
    "material": "material_supply",
}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, (str, bytes, Mapping)) or value is None:
        return []
    try:
        values = list(value)
    except Exception:
        return []
    return [item for item in values if isinstance(item, Mapping)]


def _priority(value: Any, default: float = 0.5) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return min(max(parsed, 0.0), 1.0)


def _short_hash(text: str) -> str:
    """计算短哈希。"""
    return hashlib.sha1(_text(text).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    """当前时间 ISO 格式。"""
    return datetime.now().isoformat()


def load_missing_edges(input_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """加载 missing_edges。

    Args:
        input_dir: 输入目录

    Returns:
        missing_edges 列表
    """
    base = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    path = base / "missing_edges_latest.json"
    try:
        if not path.exists():
            return []
    except OSError:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        logger.warning("读取 missing_edges_latest.json 失败: %s", exc)
        return []
    return [dict(item) for item in _rows(data)]


def compile_search_tasks(
    missing_edges: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """从 missing_edges 编译搜索任务。

    每条 missing_edge 生成 3-5 条 query，按 source_type 应用对应模板。
    search_task 含 task_source = missing_edge / generated_by = industry_chain_graph_builder。

    Args:
        missing_edges: 缺失边列表

    Returns:
        搜索任务列表
    """
    tasks: List[Dict[str, Any]] = []
    seen_queries: set[str] = set()

    for missing_edge in _rows(missing_edges):
        source_node = _text(missing_edge.get("source_node"))
        source_type = _text(missing_edge.get("source_type"), "unknown")
        target_hint = _text(missing_edge.get("target_hint"))
        missing_edge_id = _text(missing_edge.get("missing_edge_id"))
        priority_score = _priority(missing_edge.get("priority_score"), 0.5)

        if not source_node or not target_hint:
            continue

        # 获取该 source_type 的 query 模板
        templates = _QUERY_TEMPLATES.get(source_type, _QUERY_TEMPLATES["component"])
        query_type = _QUERY_TYPE_MAP.get(source_type, "generic_search")

        for template in templates:
            query = template.format(node=source_node, target=target_hint)
            query = " ".join(query.split())  # 规范化空格

            if not query or query in seen_queries:
                continue
            seen_queries.add(query)

            tasks.append({
                "task_id": f"task_{_short_hash(query)}",
                "query": query,
                "query_type": query_type,
                "related_node": source_node,
                "related_missing_edge_id": missing_edge_id,
                "priority_score": round(priority_score, 4),
                "reason": _text(missing_edge.get("reason")),
                "status": "pending",
                "created_at": _now_iso(),
                "last_run_at": "",
                "task_source": "missing_edge",
                "generated_by": "industry_chain_graph_builder",
            })

    # 按优先级降序排序
    tasks.sort(key=lambda x: x["priority_score"], reverse=True)
    return tasks


def export_search_tasks(
    tasks: List[Dict[str, Any]],
    output_dir: Optional[Path] = None,
) -> str:
    """导出搜索任务到 search_tasks_from_missing_edges_latest.json。

    不直接写 search_tasks_latest.json，由 P8 聚合。

    Args:
        tasks: 搜索任务列表
        output_dir: 输出目录

    Returns:
        输出文件路径
    """
    out = Path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    path = out / "search_tasks_from_missing_edges_latest.json"
    safe_tasks = [dict(task) for task in _rows(tasks)]
    path.write_text(
        json.dumps(safe_tasks, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return str(path)


def run_search_task_compiler(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """运行搜索任务编译全流程。

    Args:
        input_dir: 输入目录
        output_dir: 输出目录

    Returns:
        dict summary
    """
    base = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    out = Path(output_dir) if output_dir else base

    try:
        missing_edges = load_missing_edges(base)
        tasks = compile_search_tasks(missing_edges)
        output_path = export_search_tasks(tasks, out)

        return {
            "module": "search_task_compiler",
            "success": True,
            "outputs": [output_path],
            "counts": {
                "missing_edges": len(missing_edges),
                "search_tasks": len(tasks),
            },
        }
    except Exception as exc:
        logger.error("run_search_task_compiler 失败: %s", exc, exc_info=True)
        return {
            "module": "search_task_compiler",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
