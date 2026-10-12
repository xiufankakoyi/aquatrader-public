"""P3 门禁单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.research.p3_gate import (
    P2_SHADOW_REPORT_PATH,
    P3GateResult,
    P3GateStatus,
    check_p2_prerequisite,
)


def _write_report(tmp_path: Path, payload: dict) -> Path:
    report_path = tmp_path / "shadow_report.json"
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return report_path


class TestP3Gate:
    """P3 前置门禁测试。"""

    def test_p2_passed_returns_ok(self, tmp_path: Path) -> None:
        """P2 overall_passed=true → P3GateStatus.OK。"""
        report_path = _write_report(
            tmp_path, {"overall_passed": True, "artifact_status": "signed_bundle"}
        )
        result = check_p2_prerequisite(report_path)
        assert result.status is P3GateStatus.OK
        assert result.p2_overall_passed is True
        assert not result.is_blocked
        assert result.reason == ""

    def test_p2_failed_returns_pending_p2(self, tmp_path: Path) -> None:
        """P2 overall_passed=false → P3GateStatus.PENDING_P2。"""
        report_path = _write_report(
            tmp_path,
            {
                "overall_passed": False,
                "artifact_status": "superseded_tombstone",
                "reason": "旧报告撤销",
            },
        )
        result = check_p2_prerequisite(report_path)
        assert result.status is P3GateStatus.PENDING_P2
        assert result.p2_overall_passed is False
        assert result.is_blocked
        assert "pending_p2" in result.reason
        assert "撤销墓碑" in result.reason

    def test_missing_report_returns_unknown(self, tmp_path: Path) -> None:
        """报告不存在 → P3GateStatus.UNKNOWN。"""
        result = check_p2_prerequisite(tmp_path / "missing.json")
        assert result.status is P3GateStatus.UNKNOWN
        assert result.is_blocked
        assert "不存在" in result.reason

    def test_malformed_report_returns_unknown(self, tmp_path: Path) -> None:
        """报告 JSON 解析失败 → UNKNOWN，不抛异常。"""
        report_path = tmp_path / "shadow_report.json"
        report_path.write_text("not a json", encoding="utf-8")
        result = check_p2_prerequisite(report_path)
        assert result.status is P3GateStatus.UNKNOWN
        assert "读取失败" in result.reason

    @pytest.mark.parametrize("payload, expected_status", [
        (["not an object"], P3GateStatus.UNKNOWN),
        ({"overall_passed": "true"}, P3GateStatus.PENDING_P2),
    ])
    def test_invalid_report_shape_or_boolean_does_not_unlock(
        self, tmp_path: Path, payload: object, expected_status: P3GateStatus
    ) -> None:
        report_path = tmp_path / "shadow_report.json"
        report_path.write_text(json.dumps(payload), encoding="utf-8")

        result = check_p2_prerequisite(report_path)

        assert result.status is expected_status
        assert result.is_blocked

    def test_default_path_points_to_p2_10_shadow_report(self) -> None:
        """默认路径指向 data/research_control/p2_10/shadow_report.json。"""
        assert P2_SHADOW_REPORT_PATH.name == "shadow_report.json"
        assert "p2_10" in P2_SHADOW_REPORT_PATH.parts

    def test_to_dict_serializable(self, tmp_path: Path) -> None:
        """to_dict 返回可 JSON 序列化结构。"""
        report_path = _write_report(tmp_path, {"overall_passed": True})
        result = check_p2_prerequisite(report_path)
        payload = result.to_dict()
        json.dumps(payload)  # 不抛异常即可
        assert payload["status"] == "ok"
        assert payload["is_blocked"] is False

    def test_real_p2_gate_accepts_valid_ai_internal_fallback(self) -> None:
        """机构报告未通过时，可由明确标注且完整的 AI 内部认证解锁。"""
        if not P2_SHADOW_REPORT_PATH.exists():
            pytest.skip("P2 shadow_report.json 不存在")
        result = check_p2_prerequisite()
        assert result.status is P3GateStatus.OK
        assert result.certification_mode == "ai_only_internal"
        assert not result.is_blocked
