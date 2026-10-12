"""ResearchCandidate API 路由。

端点：
  GET  /api/research/candidates/health              健康检查
  GET  /api/research/candidates                     查询候选列表
  POST /api/research/candidates/build               从上游对象构建候选
  GET  /api/research/candidates/<candidate_id>       查询单个候选
  GET  /api/research/candidates/<candidate_id>/report 查询研究报告

不输出买卖、仓位、止盈止损建议。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.candidate.contracts import ResearchCandidate
from core.research.candidate.engine import CandidateBuilder, CandidateReportBuilder
from core.research.candidate.publisher import ResearchCandidatePublisher
from core.research.candidate.repository import CandidateRepository


candidate_bp = Blueprint("candidate", __name__, url_prefix="/api/research/candidates")
_repository: Any = None
_builder: Any = None
_report_builder: Any = None


def _repo() -> CandidateRepository:
    global _repository
    if _repository is None:
        _repository = CandidateRepository(Path("data/research_control/candidates.db"))
    return _repository


def _get_builder() -> CandidateBuilder:
    global _builder
    if _builder is None:
        _builder = CandidateBuilder()
    return _builder


def _get_report_builder() -> CandidateReportBuilder:
    global _report_builder
    if _report_builder is None:
        _report_builder = CandidateReportBuilder()
    return _report_builder


def _ok(data: Any = None, status: int = 200, **meta: Any):
    return jsonify({"ok": True, "data": data if data is not None else {}, **meta}), status


def _error(message: str, code: str = "BAD_REQUEST", status: int = 400):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


@candidate_bp.get("/health")
def health():
    """健康检查 + 存储统计。"""
    return _ok({"status": "ok", **_repo().stats()})


@candidate_bp.route("", methods=["GET"])
def list_candidates():
    """查询候选列表。"""
    symbol = request.args.get("symbol", "")
    status = request.args.get("status", "")
    limit = request.args.get("limit", 100, type=int)
    items = _repo().list_candidates(symbol=symbol, status=status, limit=limit)
    return _ok(items, total=len(items))


@candidate_bp.post("/build")
def build_candidate():
    """从上游对象构建 ResearchCandidate。

    P0.6: 支持 research_run_id 等只读接入字段的透传。
    未传 research_run_id 时默认按 observational_only 处理。
    """
    payload = request.get_json(silent=True) or {}
    research_run_id = str(payload.get("research_run_id", "") or "")
    readiness_summary = payload.get("research_readiness_summary") or {}
    research_gaps = payload.get("research_gaps") or []
    research_completeness = str(
        payload.get("research_completeness", "observational_only") or "observational_only"
    )
    candidate = _get_builder().build(
        thesis=payload.get("thesis"),
        earnings_impact=payload.get("earnings_impact"),
        expectation_gap=payload.get("expectation_gap"),
        valuation_snapshot=payload.get("valuation_snapshot"),
        sector_id=str(payload.get("sector_id", "")),
        as_of=str(payload.get("as_of", "")),
        industry_core_score=float(payload.get("industry_core_score", 0.0)),
        company_exposure_score=float(payload.get("company_exposure_score", 0.0)),
        catalysts=payload.get("catalysts"),
        research_run_id=research_run_id,
        research_readiness_summary=readiness_summary,
        research_gaps=research_gaps,
        research_completeness=research_completeness,
    )
    if bool(payload.get("save", True)):
        _repo().save_candidate(candidate)
    return _ok(candidate.to_dict(), 201)


@candidate_bp.get("/<candidate_id>")
def get_candidate(candidate_id: str):
    """查询单个候选。"""
    item = _repo().get_candidate(candidate_id)
    return _ok(item) if item else _error("候选不存在", "CANDIDATE_NOT_FOUND", 404)


@candidate_bp.get("/<candidate_id>/report")
def get_report(candidate_id: str):
    """查询或生成研究报告。"""
    raw = _repo().get_candidate(candidate_id)
    if not raw:
        return _error("候选不存在", "CANDIDATE_NOT_FOUND", 404)
    existing = _repo().get_report(candidate_id)
    if existing:
        return _ok(existing)
    candidate = ResearchCandidate.from_dict(raw)
    report = _get_report_builder().build(candidate)
    _repo().save_report(report)
    return _ok(report.to_dict())


@candidate_bp.post("/publish")
def publish_candidates():
    """从版本化上游对象自动发布 ResearchCandidate。

    只从持久化的 Thesis、EarningsImpact、ExpectationGap 读取数据，
    不接受 API 调用方传入的裸分数。

    请求体:
        {
            "symbols": ["300308.SZ", "002475.SZ"],
            "as_of": "2026-07-11T00:00:00+00:00"
        }
    """
    payload = request.get_json(silent=True) or {}
    symbols = payload.get("symbols", [])
    if not isinstance(symbols, list) or not symbols:
        return _error("必须提供 symbols 列表", "MISSING_SYMBOLS", 422)
    as_of = str(payload.get("as_of", ""))

    # 延迟导入避免循环依赖
    from pathlib import Path
    from core.research.earnings.repository import EarningsRepository
    from core.research.expectations.repository import ExpectationRepository
    from server.services.research.company_research_service import (
        CompanyResearchService,
    )

    expectations_repo = ExpectationRepository(
        Path("data/research_control/expectations.db")
    )
    earnings_repo = EarningsRepository()
    publisher = ResearchCandidatePublisher(
        expectations_repo=expectations_repo,
        earnings_repo=earnings_repo,
        candidate_repo=_repo(),
        research_service=CompanyResearchService(),
    )
    result = publisher.publish_batch(symbols, as_of=as_of)
    return _ok(result, 201)
