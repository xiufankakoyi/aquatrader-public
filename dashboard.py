"""AquaResearch Cockpit - FastAPI backend for research observability.

Serves a single-page dashboard that visualizes the research graph's
learning progress: evidence accumulation, confidence curves, neuron
health, algorithm contribution, and recent trace events.

Usage:
    uvicorn dashboard:app --port 8000 --reload
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("dashboard")

app = FastAPI(title="AquaResearch Cockpit", version="0.1.0")

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RESEARCH_RUNS_DIR = DATA_DIR / "research_runs"

# cockpit_data.json 是最高优先级数据源
COCKPIT_JSON = RESEARCH_RUNS_DIR / "latest" / "cockpit" / "cockpit_data.json"

# 降级路径: 从各 research_run 子目录聚合
LEARNING_METRICS_JSON = RESEARCH_RUNS_DIR / "latest" / "learning_metrics.json"
LEARNING_CURVE_JSONL = RESEARCH_RUNS_DIR / "latest" / "learning_curve_history.jsonl"
TOPIC_MASTERY_JSON = RESEARCH_RUNS_DIR / "latest" / "topic_mastery_profile.json"
BREAKTHROUGH_JSON = RESEARCH_RUNS_DIR / "latest" / "breakthrough_report.json"

# 离线 demo 数据 (无真实数据时的兜底)
DEMO_DATA_PATH = BASE_DIR / "dashboard_demo_data.json"

# ---------------------------------------------------------------------------
# Data loading with graceful degradation
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    """Safely load a JSON file, return empty dict on any failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load JSON: %s", path, exc_info=True)
        return {}


def _load_jsonl(path: Path, limit: int = 500) -> list[dict[str, Any]]:
    """Safely load JSONL, return list of dicts."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        result: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            if line.strip():
                result.append(json.loads(line))
        return result
    except Exception:
        logger.warning("Failed to load JSONL: %s", path, exc_info=True)
        return []


def _find_latest_run_dir() -> Path | None:
    """Find the most recently modified research_run subdirectory."""
    if not RESEARCH_RUNS_DIR.exists():
        return None
    run_dirs = [
        d
        for d in RESEARCH_RUNS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]
    if not run_dirs:
        return None
    return max(run_dirs, key=lambda d: d.stat().st_mtime)


def _load_data() -> dict[str, Any]:
    """Load cockpit data with multi-level fallback.

    Priority:
        1. cockpit_data.json (unified export)
        2. Aggregate from individual artifact files
        3. Demo data
        4. Empty defaults
    """
    # Level 1: Unified cockpit_data.json
    data = _load_json(COCKPIT_JSON)
    if data:
        return data

    # Level 2: Aggregate from individual files
    latest_dir = _find_latest_run_dir()
    if latest_dir:
        metrics = _load_json(latest_dir / "learning_metrics.json")
        curve = _load_jsonl(latest_dir / "learning_curve_history.jsonl")
        mastery = _load_json(latest_dir / "topic_mastery_profile.json")
        breakthrough = _load_json(latest_dir / "breakthrough_report.json")
        if metrics or curve or mastery:
            return _aggregate_artifacts(metrics, curve, mastery, breakthrough)

    # Level 3: Demo data
    demo = _load_json(DEMO_DATA_PATH)
    if demo:
        return demo

    # Level 4: Empty defaults
    return _empty_cockpit_data()


def _aggregate_artifacts(
    metrics: dict[str, Any],
    curve: list[dict[str, Any]],
    mastery: dict[str, Any],
    breakthrough: dict[str, Any],
) -> dict[str, Any]:
    """Merge individual artifact files into unified cockpit payload."""
    return {
        "summary": {
            "canonical_evidence_count": metrics.get("canonical_evidence_count", 0),
            "active_edge_count": metrics.get("active_edge_count", 0),
            "conflicted_edge_count": metrics.get("conflicted_edge_count", 0),
            "avg_confidence": metrics.get("avg_confidence", 0.0),
            "avg_uncertainty": metrics.get("avg_uncertainty", 0.0),
            "evidence_saturation": metrics.get("evidence_saturation", 0.0),
            "duplicate_ratio": metrics.get("duplicate_ratio", 0.0),
            "llm_rejected_ratio": metrics.get("llm_rejected_ratio", 0.0),
            "breakthrough_score": metrics.get("breakthrough_score", 0.0),
            "new_evidence_delta": metrics.get("new_evidence_delta", 0),
            "confidence_delta": metrics.get("confidence_delta", 0.0),
        },
        "learning_curve": curve,
        "evidence_flow": {
            "doc_retrieved": metrics.get("canonical_evidence_count", 0),
            "duplicate_skipped": int(
                metrics.get("duplicate_ratio", 0) * max(1, metrics.get("canonical_evidence_count", 0))
            ),
            "ledger_rejected": int(
                metrics.get("llm_rejected_ratio", 0) * max(1, metrics.get("canonical_evidence_count", 0))
            ),
            "evidence_accepted": metrics.get("canonical_evidence_count", 0),
            "evidence_routed": metrics.get("active_edge_count", 0),
        },
        "graph": {"nodes": [], "edges": []},
        "node_metrics": mastery.get("topics", []),
        "algorithm_contribution": [],
        "scheduler": {},
        "alerts": [],
        "trace_tail": [],
        "topic_mastery": mastery,
        "breakthrough": breakthrough,
    }


def _empty_cockpit_data() -> dict[str, Any]:
    """Return zero-initialized cockpit data structure."""
    return {
        "summary": {
            "canonical_evidence_count": 0,
            "active_edge_count": 0,
            "conflicted_edge_count": 0,
            "avg_confidence": 0.0,
            "avg_uncertainty": 0.0,
            "evidence_saturation": 0.0,
            "duplicate_ratio": 0.0,
            "llm_rejected_ratio": 0.0,
            "breakthrough_score": 0.0,
            "new_evidence_delta": 0,
            "confidence_delta": 0.0,
        },
        "learning_curve": [],
        "evidence_flow": {
            "doc_retrieved": 0,
            "duplicate_skipped": 0,
            "ledger_rejected": 0,
            "evidence_accepted": 0,
            "evidence_routed": 0,
        },
        "graph": {"nodes": [], "edges": []},
        "node_metrics": [],
        "algorithm_contribution": [],
        "scheduler": {},
        "alerts": [],
        "trace_tail": [],
    }


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_index():
    """Serve the single-page cockpit HTML."""
    html_path = BASE_DIR / "templates" / "index.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html; charset=utf-8")
    return {"error": "templates/index.html not found"}


@app.get("/api/summary")
async def get_summary():
    """总览卡片数据: 证据数、置信度、突破分数等."""
    data = _load_data()
    return data.get("summary", _empty_cockpit_data()["summary"])


@app.get("/api/learning-curve")
async def get_learning_curve():
    """学习曲线 tick 级历史."""
    data = _load_data()
    return data.get("learning_curve", [])


@app.get("/api/evidence-flow")
async def get_evidence_flow():
    """漏斗各阶段数据."""
    data = _load_data()
    return data.get("evidence_flow", _empty_cockpit_data()["evidence_flow"])


@app.get("/api/graph-data")
async def get_graph_data():
    """图谱节点 + 边."""
    data = _load_data()
    return data.get("graph", {"nodes": [], "edges": []})


@app.get("/api/neuron-table")
async def get_neuron_table():
    """神经元详细指标列表."""
    data = _load_data()
    return data.get("node_metrics", [])


@app.get("/api/algorithm-contribution")
async def get_algorithm_contribution():
    """模块贡献统计."""
    data = _load_data()
    return data.get("algorithm_contribution", [])


@app.get("/api/trace-events")
async def get_trace_events(limit: int = Query(default=50, ge=1, le=200)):
    """最近 trace 事件."""
    data = _load_data()
    events = data.get("trace_tail", [])
    return events[-limit:]


@app.get("/api/scheduler")
async def get_scheduler():
    """调度器选中的神经元与信息增益."""
    data = _load_data()
    return data.get("scheduler", {})


@app.get("/api/alerts")
async def get_alerts():
    """异常提醒."""
    data = _load_data()
    return data.get("alerts", [])


@app.get("/api/system-flow")
async def get_system_flow():
    """System structure, data flow and audit checkpoints."""
    data = _load_data()
    return data.get("system_flow", {"nodes": [], "lineage": []})


@app.get("/api/baseline-comparison")
async def get_baseline_comparison():
    """Baseline vs autonomous learning contribution."""
    data = _load_data()
    return data.get("baseline_comparison", {"kpis": {}, "metrics": []})


@app.get("/api/target-judgment")
async def get_target_judgment():
    """Target research judgment chain, without investment advice."""
    data = _load_data()
    return data.get("target_judgment", {})


# ---------------------------------------------------------------------------
# Static files (optional)
# ---------------------------------------------------------------------------
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
