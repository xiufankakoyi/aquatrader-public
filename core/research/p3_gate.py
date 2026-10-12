"""P3 工程门禁：检查 P0/P1/P2 前置认证状态。

按 P3 计划 §0 第 1 条「前置门禁不完整则停止」与用户决策「标记 P3.pending_p2」：
- 提供 ``check_p2_prerequisite`` 查询 P2 认证状态
- P2 未通过时返回 ``P3GateStatus.pending_p2``，不抛异常
- P3 入口（CLI/API/工作台）调用此模块统一标记状态
- 不阻止 P3 工程代码开发与单元测试
"""
from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
P2_SHADOW_REPORT_PATH = (
    PROJECT_ROOT / "data" / "research_control" / "p2_10" / "shadow_report.json"
)
P2_AI_INTERNAL_REPORT_PATH = (
    PROJECT_ROOT
    / "data"
    / "research_control"
    / "p2_ai_internal"
    / "replay_report.json"
)


class P3GateStatus(Enum):
    """P3 前置门禁状态。"""

    OK = "ok"
    PENDING_P2 = "pending_p2"
    PENDING_P0 = "pending_p0"
    PENDING_P1 = "pending_p1"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class P3GateResult:
    """P3 前置门禁查询结果。

    Attributes:
        status: 门禁状态
        p2_overall_passed: P2 认证是否通过
        reason: 失败原因（status != OK 时非空）
        artifact_path: 读取的 P2 报告路径
    """

    status: P3GateStatus
    p2_overall_passed: bool
    reason: str
    artifact_path: str
    certification_mode: str = ""

    @property
    def is_blocked(self) -> bool:
        """是否阻断 P3 正式启用（不阻断工程开发）。"""
        return self.status != P3GateStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "p2_overall_passed": self.p2_overall_passed,
            "reason": self.reason,
            "artifact_path": self.artifact_path,
            "certification_mode": self.certification_mode,
            "is_blocked": self.is_blocked,
        }


def _nonnegative_int(payload: Mapping[str, Any], key: str) -> int | None:
    """Read a required non-negative integer without trusting JSON coercion."""
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _valid_ai_internal_report(payload: Mapping[str, Any]) -> bool:
    """Validate the owner-authorized AI-only internal P2 contract."""
    digest = str(payload.get("evidence_sha256", ""))
    unsigned = dict(payload)
    unsigned.pop("evidence_sha256", None)
    try:
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return False
    fingerprint_valid = hashlib.sha256(canonical).hexdigest() == digest
    return (
        payload.get("status") == "P2_AI_INTERNAL_CERTIFIED"
        and payload.get("overall_passed") is True
        and payload.get("review_mode") == "ai_only_internal"
        and payload.get("human_independent_review") is False
        and payload.get("identity_independent") is False
        and payload.get("external_attestation_required") is False
        and payload.get("institutional_certification") is False
        and (_nonnegative_int(payload, "company_count") or -1) >= 10
        and (_nonnegative_int(payload, "observation_count") or -1) >= 20
        and _nonnegative_int(payload, "leakage_count") == 0
        and _nonnegative_int(payload, "missing_market_inputs") == 0
        and _nonnegative_int(payload, "future_p1_evidence_count") == 0
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest.lower())
        and fingerprint_valid
    )


def check_p2_prerequisite(
    shadow_report_path: Path | None = None,
    *,
    ai_internal_report_path: Path | None = None,
) -> P3GateResult:
    """查询 P2 认证状态；P2 未通过时返回 pending_p2。

    Args:
        shadow_report_path: 自定义 P2 shadow_report.json 路径；
            默认读取 data/research_control/p2_10/shadow_report.json

    Returns:
        P3GateResult: 门禁结果
    """
    path = shadow_report_path or P2_SHADOW_REPORT_PATH
    if not path.exists():
        return P3GateResult(
            status=P3GateStatus.UNKNOWN,
            p2_overall_passed=False,
            reason=f"P2 报告不存在：{path}",
            artifact_path=str(path),
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return P3GateResult(
            status=P3GateStatus.UNKNOWN,
            p2_overall_passed=False,
            reason=f"P2 报告读取失败：{type(exc).__name__}: {exc}",
            artifact_path=str(path),
        )
    if not isinstance(payload, Mapping):
        return P3GateResult(
            status=P3GateStatus.UNKNOWN,
            p2_overall_passed=False,
            reason="P2 报告根节点必须是对象",
            artifact_path=str(path),
        )
    overall_passed = payload.get("overall_passed") is True
    artifact_status = str(payload.get("artifact_status", ""))
    if overall_passed:
        return P3GateResult(
            status=P3GateStatus.OK,
            p2_overall_passed=True,
            reason="",
            artifact_path=str(path),
            certification_mode="institutional",
        )
    internal_path = ai_internal_report_path
    if internal_path is None and shadow_report_path is None:
        internal_path = P2_AI_INTERNAL_REPORT_PATH
    if internal_path and internal_path.exists():
        try:
            internal_payload = json.loads(internal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            internal_payload = {}
        if isinstance(internal_payload, Mapping) and _valid_ai_internal_report(
            internal_payload
        ):
            return P3GateResult(
                status=P3GateStatus.OK,
                p2_overall_passed=True,
                reason="",
                artifact_path=str(internal_path),
                certification_mode="ai_only_internal",
            )
    reason = payload.get("reason") or payload.get("p3_blocked_reason") or "P2 未通过"
    if artifact_status == "superseded_tombstone":
        reason = f"P2 报告已撤销墓碑：{reason}"
    return P3GateResult(
        status=P3GateStatus.PENDING_P2,
        p2_overall_passed=False,
        reason=f"pending_p2: {reason}",
        artifact_path=str(path),
    )
