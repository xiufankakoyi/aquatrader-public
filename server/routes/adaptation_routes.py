"""Stage 4 API for domain-isolated feedback, calibration and attribution reports."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.adaptation.contracts import CalibrationVersion, DomainFeedback, PortfolioDiagnostics
from core.research.adaptation.engine import CalibrationGate, DomainFeedbackFirewall, diagnose_portfolio, walk_forward_windows
from core.research.adaptation.repository import AdaptationRepository
from core.research.adaptation.reporting import FourDomainReportBuilder

adaptation_bp = Blueprint("adaptation", __name__, url_prefix="/api/research/adaptation")
_repository: AdaptationRepository | None = None


def _repo() -> AdaptationRepository:
    global _repository
    if _repository is None: _repository = AdaptationRepository(Path("data/research_control/adaptation.db"))
    return _repository


def ok(data: Any, status: int = 200, **meta: Any): return jsonify({"ok": True, "data": data, **meta}), status
def error(message: str, code: str = "BAD_REQUEST", status: int = 400): return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


@adaptation_bp.get("/health")
def health(): return ok({"status": "ok", **_repo().stats()})


@adaptation_bp.route("/feedback", methods=["GET", "POST"])
def feedback():
    if request.method == "GET":
        domain = request.args.get("domain", ""); rows = _repo().list("domain_feedback", domain)
        return ok(rows, total=len(rows))
    payload = request.get_json(silent=True) or {}
    item = DomainFeedback(**{k: v for k, v in payload.items() if k in DomainFeedback.__dataclass_fields__})
    try: review = DomainFeedbackFirewall().route(item)
    except ValueError as exc: return error(str(exc), "DOMAIN_BOUNDARY_VIOLATION")
    _repo().save_feedback_and_review(item, review)
    return ok({"feedback": asdict(item), "review_task": asdict(review) if review else None}, 201)


@adaptation_bp.get("/reviews")
def reviews():
    domain = request.args.get("source_domain", ""); rows = _repo().list("attribution_review_tasks", domain)
    return ok(rows, total=len(rows))


@adaptation_bp.route("/calibrations", methods=["GET", "POST"])
def calibrations():
    if request.method == "GET":
        domain = request.args.get("domain", ""); rows = _repo().list("adaptation_calibrations", domain)
        return ok(rows, total=len(rows))
    payload = request.get_json(silent=True) or {}
    raw_bounds = payload.get("bounds", {})
    payload["bounds"] = {key: tuple(value) for key, value in raw_bounds.items()}
    item = CalibrationVersion(**{k: v for k, v in payload.items() if k in CalibrationVersion.__dataclass_fields__})
    if item.status != "shadow":
        return error("新校准版本必须先以 shadow 状态创建", "SHADOW_REQUIRED")
    reasons = CalibrationGate().validate(item)
    if reasons: return error(";".join(reasons), "CALIBRATION_GATE_REJECTED")
    _repo().save("adaptation_calibrations", item)
    return ok(asdict(item), 201)


def _calibration_from_saved(raw: dict[str, Any]) -> CalibrationVersion:
    raw = dict(raw); raw["bounds"] = {key: tuple(value) for key, value in raw.get("bounds", {}).items()}
    return CalibrationVersion(**{k: v for k, v in raw.items() if k in CalibrationVersion.__dataclass_fields__})


@adaptation_bp.post("/calibrations/<calibration_id>/enable")
def enable_calibration(calibration_id: str):
    raw = _repo().get("adaptation_calibrations", calibration_id)
    if not raw: return error("校准版本不存在", "CALIBRATION_NOT_FOUND", 404)
    if raw.get("status") != "shadow": return error("只有 shadow 版本可启用", "INVALID_CALIBRATION_TRANSITION")
    approved_by = str((request.get_json(silent=True) or {}).get("approved_by") or "").strip()
    item = _calibration_from_saved(raw); item.status = "enabled"; item.approved_by = approved_by
    reasons = CalibrationGate().validate(item)
    if reasons: return error(";".join(reasons), "CALIBRATION_GATE_REJECTED")
    _repo().save("adaptation_calibrations", item); return ok(asdict(item))


@adaptation_bp.post("/calibrations/<calibration_id>/rollback")
def rollback_calibration(calibration_id: str):
    raw = _repo().get("adaptation_calibrations", calibration_id)
    if not raw: return error("校准版本不存在", "CALIBRATION_NOT_FOUND", 404)
    operator = str((request.get_json(silent=True) or {}).get("approved_by") or "").strip()
    if not operator: return error("回滚需要审批人", "MANUAL_APPROVAL_REQUIRED")
    target = _calibration_from_saved(raw)
    rolled = CalibrationVersion(
        domain=target.domain, parameters=dict(target.baseline_parameters),
        baseline_parameters=dict(target.baseline_parameters), bounds=dict(target.bounds),
        sample_size=target.sample_size, minimum_sample_size=target.minimum_sample_size,
        shadow_metrics=dict(target.shadow_metrics), status="rolled_back", approved_by=operator,
        rollback_of=target.calibration_id,
    )
    reasons = CalibrationGate().validate(rolled)
    if reasons: return error(";".join(reasons), "CALIBRATION_GATE_REJECTED")
    _repo().save("adaptation_calibrations", rolled); return ok(asdict(rolled), 201)


@adaptation_bp.route("/reports", methods=["GET", "POST"])
def reports():
    if request.method == "GET":
        as_of = request.args.get("as_of", ""); rows = _repo().list("four_domain_reports", as_of)
        return ok(rows, total=len(rows))
    payload = request.get_json(silent=True) or {}
    as_of = str(payload.get("as_of") or "")
    if not as_of:
        return error("归因报告必须提供 as_of", "MISSING_AS_OF")
    rows = list(payload.get("portfolio_rows") or [])
    diagnostics = diagnose_portfolio(rows, as_of=as_of)
    feedback_items = [DomainFeedback(**{k: v for k, v in row.items() if k in DomainFeedback.__dataclass_fields__}) for row in payload.get("feedback", [])]
    dates = list(payload.get("dates") or [])
    windows = walk_forward_windows(dates, int(payload.get("train_size", 1)), int(payload.get("test_size", 1))) if dates else []
    report = FourDomainReportBuilder().build(
        as_of=as_of, signals=[float(v) for v in payload.get("signals", [])],
        outcomes=[float(v) for v in payload.get("outcomes", [])], diagnostics=diagnostics,
        feedback=feedback_items, pit_blocked_count=int(payload.get("pit_blocked_count", 0)),
        cost_attribution={k: float(v) for k, v in dict(payload.get("cost_attribution") or {}).items()},
        walk_forward_windows=windows,
    )
    _repo().save("four_domain_reports", report)
    return ok(report.to_dict(), 201)
