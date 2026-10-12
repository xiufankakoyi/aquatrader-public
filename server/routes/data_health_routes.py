"""Read-only data-health API promised by the runtime runbook."""
from __future__ import annotations

from flask import Blueprint, jsonify

from server.services.data_health_service import build_data_health_report

data_health_bp = Blueprint("data_health", __name__, url_prefix="/api/data")


@data_health_bp.get("/health")
def get_data_health():
    """Return current dataset health without writing report artifacts."""
    try:
        report = build_data_health_report(write_files=False)
        return jsonify({"success": True, "data": report})
    except Exception as exc:  # defensive API degradation
        return jsonify({
            "success": False,
            "data": {},
            "error": f"data health unavailable: {exc}",
        }), 503
