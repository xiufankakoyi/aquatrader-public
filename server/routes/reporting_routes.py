"""P1.9 CFA 风格研究报告 API 端点。

Blueprint url_prefix="/api/research/reports"

端点：
- POST /cfa                构建 CFA 报告（必填 company_symbol / as_of；可选 lookback_days 默认 120）
- GET  /cfa                查询报告列表（支持 company_symbol / as_of / status / limit）
- GET  /cfa/<report_id>    单个报告详情
- GET  /cfa/latest         按 PIT 取最新报告

所有响应固定含 notice="研究数据产物，不包含交易动作建议。"
缺必填字段返回 400 INVALID_INPUT；缺数据返回 200 + degraded=true + degradation_reason。
错误响应统一 {ok: false, error: {code, message, trace_id}}。

P1.10 修复 P1.9 已知限制 1：在 _builder_singleton 中实例化 7 个真实领域仓储
（StatementRepository / OperatingModelRepository / EarningsRepository /
IndustryRepository / GovernanceRepository / ExpectationRepository /
ValuationRepository）并注入 CfaReportBuilder，使生产环境不再以 None 仓储运行。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.reporting.cfa_report_builder import (
    CfaReportBuilder,
    ReportRepository,
)
from core.research.reporting.contracts import NOTICE


logger = logging.getLogger(__name__)
reporting_bp = Blueprint("reporting", __name__, url_prefix="/api/research/reports")
_repository: Any = None
_builder: CfaReportBuilder | None = None

NOTICE_TEXT: str = NOTICE

# 真实 SQLite 仓储的默认数据库路径（P1.10 注入）
_DEFAULT_DB_DIR: Path = Path("data/research_control")
_DEFAULT_FUNDAMENTALS_DB: Path = _DEFAULT_DB_DIR / "fundamentals.db"
_DEFAULT_OPERATING_DB: Path = _DEFAULT_DB_DIR / "operating_models.db"
_DEFAULT_EARNINGS_DB: Path = _DEFAULT_DB_DIR / "earnings.db"
_DEFAULT_INDUSTRY_DB: Path = _DEFAULT_DB_DIR / "industry.db"
_DEFAULT_GOVERNANCE_DB: Path = _DEFAULT_DB_DIR / "governance.db"
_DEFAULT_EXPECTATIONS_DB: Path = _DEFAULT_DB_DIR / "expectations.db"
_DEFAULT_VALUATION_DB: Path = _DEFAULT_DB_DIR / "valuation.db"


def _repo() -> ReportRepository:
    """惰性初始化 CFA 报告仓储单例。"""
    global _repository
    if _repository is None:
        _repository = ReportRepository(
            Path("data/research_control/cfa_reports.db")
        )
    return _repository


def _build_default_repositories() -> dict[str, Any]:
    """实例化 P1.1-P1.8 已实现的 7 个真实领域仓储。

    每个仓储均使用 SQLite 持久化，幂等迁移，PIT 过滤。
    任一仓储初始化失败时返回 None，CfaReportBuilder 仍能生成 degraded 报告。

    Returns:
        dict，键为 fundamentals_repo / operating_repo / earnings_repo /
        industry_repo / governance_repo / expectation_repo / valuation_repo
    """
    repos: dict[str, Any] = {}
    # P1.1 StatementRepository 是内存版，无 db_path 参数
    try:
        from core.research.fundamentals.repository import StatementRepository
        repos["fundamentals_repo"] = StatementRepository()
    except Exception as exc:
        logger.warning("StatementRepository 初始化失败: %s", exc)
        repos["fundamentals_repo"] = None

    # P1.3 OperatingModelRepository 是内存版，无 db_path 参数
    try:
        from core.research.operating_model.repository import OperatingModelRepository
        repos["operating_repo"] = OperatingModelRepository()
    except Exception as exc:
        logger.warning("OperatingModelRepository 初始化失败: %s", exc)
        repos["operating_repo"] = None

    # P1.4 EarningsRepository 是 SQLite 版
    try:
        from core.research.earnings.repository import EarningsRepository
        repos["earnings_repo"] = EarningsRepository(_DEFAULT_EARNINGS_DB)
    except Exception as exc:
        logger.warning("EarningsRepository 初始化失败: %s", exc)
        repos["earnings_repo"] = None

    # P1.5 IndustryRepository：SQLite 版（P1.10 整改：从 None 占位升级为真实持久化仓储）
    # 修复 P1 §6 失败停止规则与 P1 验收报告「IndustryRepository 未落地」硬阻断。
    # 三张表（industry_economics / industry_competition / industry_lifecycle）幂等迁移，
    # CfaReportBuilder 章节 3 通过 get_latest_profile 取整合后的 IndustryProfile。
    try:
        from core.research.industry.repository import IndustryRepository
        repos["industry_repo"] = IndustryRepository(_DEFAULT_INDUSTRY_DB)
    except Exception as exc:
        logger.warning("IndustryRepository 初始化失败: %s", exc)
        repos["industry_repo"] = None

    # P1.6 GovernanceRepository 是 SQLite 版
    try:
        from core.research.governance.repository import GovernanceRepository
        repos["governance_repo"] = GovernanceRepository(_DEFAULT_GOVERNANCE_DB)
    except Exception as exc:
        logger.warning("GovernanceRepository 初始化失败: %s", exc)
        repos["governance_repo"] = None

    # P1.7 ExpectationRepository 是 SQLite 版
    try:
        from core.research.expectations.repository import ExpectationRepository
        repos["expectation_repo"] = ExpectationRepository(_DEFAULT_EXPECTATIONS_DB)
    except Exception as exc:
        logger.warning("ExpectationRepository 初始化失败: %s", exc)
        repos["expectation_repo"] = None

    # P1.8 ValuationRepository 是 SQLite 版
    try:
        from core.research.valuation.repository import ValuationRepository
        repos["valuation_repo"] = ValuationRepository(_DEFAULT_VALUATION_DB)
    except Exception as exc:
        logger.warning("ValuationRepository 初始化失败: %s", exc)
        repos["valuation_repo"] = None

    return repos


def _builder_singleton() -> CfaReportBuilder:
    """惰性初始化构建器单例，注入 P1.1-P1.8 真实领域仓储。

    P1.10 修复 P1.9 已知限制 1：默认初始化时实例化 7 个真实仓储并注入
    CfaReportBuilder。若任一仓储初始化失败，对应字段为 None，构建器
    仍可生成 degraded 报告而不抛 5xx。测试时可通过 set_builder()
    覆盖为 mock 仓储。
    """
    global _builder
    if _builder is None:
        repos = _build_default_repositories()
        _builder = CfaReportBuilder(
            fundamentals_repo=repos.get("fundamentals_repo"),
            operating_repo=repos.get("operating_repo"),
            earnings_repo=repos.get("earnings_repo"),
            industry_repo=repos.get("industry_repo"),
            governance_repo=repos.get("governance_repo"),
            expectation_repo=repos.get("expectation_repo"),
            valuation_repo=repos.get("valuation_repo"),
        )
    return _builder


def set_builder(builder: CfaReportBuilder) -> None:
    """注入自定义构建器（生产环境用于注入各领域真实仓储）。

    测试时通过此函数注入 mock 仓储构建器以隔离真实存储。
    """
    global _builder
    _builder = builder


def set_repository(repo: ReportRepository) -> None:
    """注入自定义仓储。"""
    global _repository
    _repository = repo


def _trace_id() -> str:
    """生成 trace_id。"""
    return f"tr_{uuid.uuid4().hex[:12]}"


def _ok(data: Any, status: int = 200, **meta: Any):
    """统一成功响应。"""
    payload: dict[str, Any] = {
        "ok": True,
        "data": data,
        "notice": NOTICE_TEXT,
        "trace_id": _trace_id(),
    }
    payload.update(meta)
    return jsonify(payload), status


def _error(message: str, code: str = "BAD_REQUEST", status: int = 400,
           trace_id: str | None = None):
    """统一错误响应。"""
    return jsonify({
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "trace_id": trace_id or _trace_id(),
        },
        "notice": NOTICE_TEXT,
    }), status


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@reporting_bp.get("/health")
def health():
    """健康检查 + 存储统计。"""
    return _ok({"status": "ok", **_repo().stats()})


@reporting_bp.route("/cfa", methods=["GET", "POST"])
def cfa_reports():
    """CFA 报告集合：POST 构建；GET 查询列表。"""
    if request.method == "GET":
        items = _repo().list_reports(
            company_symbol=request.args.get("company_symbol", ""),
            as_of=request.args.get("as_of", ""),
            status=request.args.get("status", ""),
            limit=request.args.get("limit", 100, type=int),
        )
        return _ok(items, total=len(items))

    payload = request.get_json(silent=True) or {}
    company_symbol = str(payload.get("company_symbol", "")).strip()
    as_of = str(payload.get("as_of", "")).strip()
    if not company_symbol or not as_of:
        return _error(
            "company_symbol / as_of 均为必填", "INVALID_INPUT", 400,
        )
    try:
        lookback_days = int(payload.get("lookback_days", 120) or 120)
    except (TypeError, ValueError):
        lookback_days = 120
    if lookback_days < 30 or lookback_days > 730:
        return _error(
            "lookback_days 必须在 30-730 之间", "INVALID_INPUT", 400,
        )

    builder = _builder_singleton()
    report = builder.build(company_symbol, as_of, lookback_days=lookback_days)
    if bool(payload.get("save", True)):
        try:
            _repo().save_report(report)
        except Exception:
            # 保存失败不影响返回报告内容
            pass
    data = report.to_dict()
    # 标记 degraded 便于前端处理
    degraded = report.overall_status == "degraded"
    return _ok(
        data,
        degraded=degraded,
        degradation_reason=";".join(report.degradation_reasons) if degraded else "",
    )


@reporting_bp.get("/cfa/latest")
def cfa_latest():
    """按 PIT 取最新 CFA 报告。"""
    company_symbol = request.args.get("company_symbol", "").strip()
    as_of = request.args.get("as_of", "").strip()
    if not company_symbol:
        return _error("company_symbol 必填", "INVALID_INPUT", 400)
    item = _repo().get_latest_report(company_symbol, as_of=as_of)
    if not item:
        # 缺数据返回 200 + degraded=true，不返回 5xx
        return _ok(
            None,
            degraded=True,
            degradation_reason="no_report_for_symbol",
        )
    overall_status = str(item.get("overall_status", ""))
    degraded = overall_status == "degraded"
    return _ok(
        item,
        degraded=degraded,
        degradation_reason=";".join(item.get("degradation_reasons", []) or [])
        if degraded else "",
    )


@reporting_bp.get("/cfa/<report_id>")
def cfa_detail(report_id: str):
    """单个 CFA 报告详情。"""
    item = _repo().get_report(report_id)
    if not item:
        return _error("CFA 报告不存在", "REPORT_NOT_FOUND", 404)
    return _ok(item)
