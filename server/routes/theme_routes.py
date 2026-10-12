"""主题控制面 API 路由。

端点：
  POST /api/quant/themes/scan                        触发主题扫描
  GET  /api/quant/themes/runs                        列出最近的 theme runs
  GET  /api/quant/themes/<theme_id>                  获取指定主题详情
  POST /api/research/themes/review                   Research 审查主题
  GET  /api/research/themes/<theme_id>/versions      获取主题版本链
  POST /api/quant/themes/<theme_id>/daily-screen     对指定主题执行日线筛选
  GET  /api/quant/themes/<theme_id>/candidates       获取指定主题的候选列表

所有响应包含 as_of / source_snapshot_id / theme_version / strategy_version /
degraded / degradation_reasons 元数据字段。接口失败返回 500 + error 字段，
不得导致页面或查询崩溃。不输出投资建议。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from flask import Blueprint, Response, request

from core.quant.theme.candidate_quota import CandidateQuotaGenerator
from core.quant.theme.contracts import ThemePacket
from core.quant.theme.daily_screener import ThemeScreeningResult
from core.quant.theme.repository import ThemeRepository
from core.quant.theme.service import DynamicThemeEngine
from core.research.control_plane.theme_review import ThemeReviewProcessor

logger = logging.getLogger(__name__)

theme_bp = Blueprint("themes", __name__, url_prefix="/api")

# ------------------------------------------------------------------ #
# 模块级单例（惰性加载，测试可通过覆盖全局变量注入 mock）
# ------------------------------------------------------------------ #
_theme_engine: DynamicThemeEngine | None = None
_theme_repository: ThemeRepository | None = None
_theme_review_processor: ThemeReviewProcessor | None = None
_candidate_quota_generator: CandidateQuotaGenerator | None = None
_theme_daily_screener: Any | None = None
_theme_news_loader: Any | None = None
_theme_company_name_by_symbol: dict[str, str] = {}


def _get_theme_engine() -> DynamicThemeEngine:
    """惰性加载 DynamicThemeEngine 单例。

    首次调用时初始化全部依赖（source_router / event_clusterer /
    theme_assembler / 三个探测器 / repository），后续调用复用单例。

    Returns:
        DynamicThemeEngine 实例。
    """
    global _theme_engine, _theme_news_loader, _theme_company_name_by_symbol
    if _theme_engine is None:
        from core.quant.theme.event_clusterer import EventClusterer
        from core.quant.theme.legacy_theme_detector import LegacyThemeDetector
        from core.quant.theme.persistence_detector import NewsPersistenceDetector
        from core.quant.theme.reversal_detector import IceReversalDetector
        from core.quant.theme.source_router import ThemeSourceRouter
        from core.quant.theme.theme_assembler import ThemeAssembler
        from core.quant.theme.market_feature_enricher import ThemeMarketFeatureEnricher

        repository = _get_theme_repository()

        stock_name_map: dict[str, str] = {}
        known_theme_names: list[str] = []
        try:
            from data_svc.unified_data_query import UnifiedDataQuery

            basic = UnifiedDataQuery().get_stock_basic()
            if basic is not None and not basic.empty:
                for row in basic.to_dict(orient="records"):
                    name = str(row.get("name") or "").strip()
                    symbol = str(row.get("stock_code") or row.get("ts_code") or row.get("symbol") or "").strip()
                    if name and symbol:
                        stock_name_map[name] = symbol
        except Exception as exc:
            logger.warning("加载本地公司名映射失败，新闻只识别显式股票代码: %s", exc)
        try:
            from pathlib import Path
            import pandas as pd

            for path in (
                Path("data/industry/concept_boards.parquet"),
                Path("data/industry/concept_board_members.parquet"),
            ):
                if not path.exists():
                    continue
                frame = pd.read_parquet(path, columns=["board_name"])
                known_theme_names.extend(
                    str(value).strip()
                    for value in frame["board_name"].dropna().tolist()
                    if str(value).strip()
                )
        except Exception as exc:
            logger.warning("加载本地动态主题词典失败: %s", exc)

        # Production wiring must use the same repository for legacy-theme
        # lookups.  Tests used mocks and previously missed this required
        # constructor argument, causing the real endpoint to fail before the
        # first scan started.
        legacy_detector = LegacyThemeDetector(
            repository=repository, require_current_market_state=True
        )

        import threading
        import time

        news_cache: dict[str, Any] = {"expires_at": 0.0, "items": []}
        news_lock = threading.Lock()
        news_gateway: Any | None = None

        def _fetch_news_uncached() -> list[dict[str, Any]]:
            """Load the latest real news feed with safe degradation."""
            nonlocal news_gateway
            try:
                from data_svc.gateways.china_stock_data_gateway import (
                    ChinaStockDataGateway,
                )

                if news_gateway is None:
                    news_gateway = ChinaStockDataGateway()
                gateway = news_gateway
                # Preserve a mix of sources instead of taking the first 100
                # Sina rows from the aggregate result.
                groups: list[list[dict[str, Any]]] = []
                for loader in (
                    lambda: gateway.news.fetch_sina_news(pages=1),
                    gateway.news.fetch_ths_headlines,
                    lambda: gateway.news.fetch_eastmoney_7x24(pages=1),
                ):
                    try:
                        rows = loader() or []
                        groups.append([row for row in rows if isinstance(row, dict)][:40])
                    except Exception as exc:
                        logger.warning("单个新闻源不可用: %s", exc)
                merged: list[dict[str, Any]] = []
                for index in range(max((len(group) for group in groups), default=0)):
                    for group in groups:
                        if index < len(group):
                            merged.append(group[index])
                return merged[:100]
            except Exception as exc:  # pragma: no cover - provider dependent
                logger.warning("动态主题新闻源不可用，按空新闻降级: %s", exc)
                return []

        def _load_news() -> list[dict[str, Any]]:
            if news_cache["expires_at"] > time.time():
                return list(news_cache["items"])
            with news_lock:
                if news_cache["expires_at"] > time.time():
                    return list(news_cache["items"])
                result = _fetch_news_uncached()
                news_cache["items"] = list(result)
                news_cache["expires_at"] = time.time() + 120.0
                return result

        _theme_engine = DynamicThemeEngine(
            source_router=ThemeSourceRouter(),
            event_clusterer=EventClusterer(
                stock_name_to_symbol=stock_name_map,
                known_theme_names=known_theme_names,
            ),
            theme_assembler=ThemeAssembler(),
            persistence_detector=NewsPersistenceDetector(),
            reversal_detector=IceReversalDetector(
                repository=repository, require_research_validated_core=True
            ),
            legacy_theme_detector=legacy_detector,
            repository=repository,
            news_loader=_load_news,
            market_feature_enricher=ThemeMarketFeatureEnricher(),
        )
        _theme_news_loader = _load_news
        _theme_company_name_by_symbol = {
            symbol: name for name, symbol in stock_name_map.items()
        }
    return _theme_engine


def _get_theme_repository() -> ThemeRepository:
    """惰性加载 ThemeRepository 单例。

    Returns:
        ThemeRepository 实例。
    """
    global _theme_repository
    if _theme_repository is None:
        _theme_repository = ThemeRepository()
    return _theme_repository


def _get_theme_review_processor() -> ThemeReviewProcessor:
    """惰性加载 ThemeReviewProcessor 单例。

    Returns:
        ThemeReviewProcessor 实例。
    """
    global _theme_review_processor
    if _theme_review_processor is None:
        # Ensure the shared real news loader and company map are initialized.
        _get_theme_engine()
        from core.quant.theme.legacy_theme_detector import LegacyThemeDetector
        from core.research.control_plane.repository import ResearchRepository

        _theme_review_processor = ThemeReviewProcessor(
            repository=ResearchRepository(),
            legacy_detector=LegacyThemeDetector(
                repository=_get_theme_repository(), require_current_market_state=True
            ),
            news_loader=_theme_news_loader,
            company_name_by_symbol=_theme_company_name_by_symbol,
        )
    return _theme_review_processor


def _get_candidate_quota_generator() -> CandidateQuotaGenerator:
    """惰性加载 CandidateQuotaGenerator 单例。

    Returns:
        CandidateQuotaGenerator 实例。
    """
    global _candidate_quota_generator
    if _candidate_quota_generator is None:
        _candidate_quota_generator = CandidateQuotaGenerator(
            enforce_research_gate=True
        )
    return _candidate_quota_generator


def _apply_candidate_admission(
    candidates: list[Any],
    as_of: str,
    *,
    research_repository: Any | None = None,
) -> list[Any]:
    """对直接路由生成的候选执行与 QuantPipeline 相同的 fail-closed 门禁。"""
    from core.quant.gate import apply_admission_to_candidate, check_admission

    repository = research_repository
    if repository is None:
        try:
            from core.research.control_plane.repository import ResearchRepository

            repository = ResearchRepository()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ResearchRepository 不可用，候选门禁按 fail-closed 处理: %s", exc)

    for candidate in candidates:
        try:
            admission = check_admission(
                candidate,
                research_repository=repository,
                as_of=as_of,
            )
            apply_admission_to_candidate(candidate, admission)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "候选门禁异常，按 fail-closed 拒绝 candidate_id=%s: %s",
                getattr(candidate, "candidate_id", ""),
                exc,
            )
            candidate.admission_passed = False
            candidate.degraded = True
            degraded_fields = list(getattr(candidate, "degraded_fields", []) or [])
            if "admission" not in degraded_fields:
                degraded_fields.append("admission")
            candidate.degraded_fields = degraded_fields
    return candidates


def _get_theme_daily_screener() -> Any:
    """惰性加载 ThemeDailyScreener 单例。

    Returns:
        ThemeDailyScreener 实例。
    """
    global _theme_daily_screener
    if _theme_daily_screener is None:
        from core.quant.theme.daily_screener import ThemeDailyScreener

        _theme_daily_screener = ThemeDailyScreener()
    return _theme_daily_screener


# ------------------------------------------------------------------ #
# 响应构造工具
# ------------------------------------------------------------------ #


def _json_response(payload: dict[str, Any], status: int = 200) -> tuple[Response, int]:
    """构造 JSON 响应，确保中文不转义。

    Args:
        payload: 响应体字典。
        status: HTTP 状态码。

    Returns:
        (Response, status) 元组。
    """
    body = json.dumps(payload, ensure_ascii=False)
    return Response(body, status=status, mimetype="application/json; charset=utf-8"), status


def _empty_meta() -> dict[str, Any]:
    """返回空的元数据字段集合（无 packet 时使用）。

    Returns:
        含 as_of / source_snapshot_id / theme_version / strategy_version /
        degraded / degradation_reasons 的空元数据字典。
    """
    return {
        "as_of": "",
        "source_snapshot_id": "",
        "theme_version": "",
        "strategy_version": "",
        "degraded": False,
        "degradation_reasons": [],
    }


def _packet_meta(packet: ThemePacket | None) -> dict[str, Any]:
    """从 ThemePacket 提取响应必需的元数据字段。

    theme_version 取 theme_run_id（每次运行为一个版本）。

    Args:
        packet: ThemePacket 实例，可为 None。

    Returns:
        含 as_of / source_snapshot_id / theme_version / strategy_version /
        degraded / degradation_reasons 的元数据字典。
    """
    if packet is None:
        return _empty_meta()
    return {
        "as_of": packet.as_of,
        "source_snapshot_id": packet.source_snapshot_id,
        "theme_version": packet.theme_run_id,
        "strategy_version": packet.strategy_version,
        "degraded": packet.degraded,
        "degradation_reasons": list(packet.degradation_reasons),
    }


def _ok(data: Any = None, status: int = 200, **meta: Any) -> tuple[Response, int]:
    """统一成功响应。

    Args:
        data: 响应数据体。
        status: HTTP 状态码。
        **meta: 元数据字段（as_of / source_snapshot_id / theme_version /
            strategy_version / degraded / degradation_reasons 等）。

    Returns:
        (Response, status) 元组。
    """
    payload = {"ok": True, "data": data if data is not None else {}, **meta}
    return _json_response(payload, status)


def _error(
    message: str, code: str = "BAD_REQUEST", status: int = 400
) -> tuple[Response, int]:
    """统一错误响应。

    Args:
        message: 错误描述。
        code: 错误代码。
        status: HTTP 状态码。

    Returns:
        (Response, status) 元组。
    """
    payload = {"ok": False, "error": {"code": code, "message": message}}
    return _json_response(payload, status)


def _summarize_packet(packet: ThemePacket) -> dict[str, Any]:
    """将 ThemePacket 序列化为摘要字典（不含完整 members 列表）。

    用于 /runs 列表端点，保留元数据与统计字段，剥离 members / core_members
    完整列表以减小响应体积。

    Args:
        packet: ThemePacket 实例。

    Returns:
        摘要字典，含 member_count / core_member_count 统计字段。
    """
    d = packet.to_dict()
    d["member_count"] = len(d.get("members", []))
    d["core_member_count"] = len(d.get("core_members", []))
    d["members"] = []
    d["core_members"] = []
    return d


def _screening_result_to_dict(result: ThemeScreeningResult) -> dict[str, Any]:
    """将 ThemeScreeningResult 序列化为字典。

    ThemeScreeningResult 是 dataclass 但未提供 to_dict 方法，
    使用 dataclasses.asdict 转换。

    Args:
        result: ThemeScreeningResult 实例。

    Returns:
        可 JSON 序列化的字典。
    """
    return asdict(result)


def _resolve_packet(theme_id_or_run_id: str) -> ThemePacket | None:
    """按 theme_id 或 theme_run_id 解析 ThemePacket。

    优先按 theme_run_id 精确查询（get_packet），未命中时按 theme_id
    查版本链取最新版本（list_versions 取首条）。

    Args:
        theme_id_or_run_id: theme_id 或 theme_run_id 字符串。

    Returns:
        ThemePacket 实例，未找到返回 None。
    """
    repo = _get_theme_repository()
    # 优先按 theme_run_id 查询
    packet = repo.get_packet(theme_id_or_run_id)
    if packet is not None:
        return packet
    # 回退到按 theme_id 查版本链，取最新
    versions = repo.list_versions(theme_id_or_run_id)
    if versions:
        return versions[0]
    return None


# ------------------------------------------------------------------ #
# API 端点
# ------------------------------------------------------------------ #


@theme_bp.route("/quant/themes/scan", methods=["POST"])
def scan_themes() -> tuple[Response, int]:
    """触发主题扫描。

    Request body (optional):
        {
            "as_of": "2026-07-13T00:00:00Z",
            "trade_date": "2026-07-13",
            "max_themes": 15
        }

    Response:
        {
            "ok": true,
            "data": {"themes": [...], ...},
            "as_of": "...",
            "source_snapshot_id": "...",
            "theme_version": "...",
            "strategy_version": "...",
            "degraded": false,
            "degradation_reasons": []
        }
    """
    try:
        payload = request.get_json(silent=True) or {}
        as_of = str(payload.get("as_of") or "")
        trade_date = str(payload.get("trade_date") or "")
        max_themes = payload.get("max_themes", 15)
        try:
            max_themes = int(max_themes)
        except (TypeError, ValueError):
            return _error(
                f"max_themes 必须为整数: {max_themes}", "INVALID_PARAM", 400
            )
        if max_themes <= 0:
            return _error(
                f"max_themes 必须为正整数: {max_themes}", "INVALID_PARAM", 400
            )

        engine = _get_theme_engine()
        packets = engine.generate(
            as_of=as_of, trade_date=trade_date, max_themes=max_themes
        )

        # 取首个 packet 的元数据作为响应顶层元数据
        meta = _packet_meta(packets[0]) if packets else _empty_meta()
        themes_list = [p.to_dict() for p in packets]
        data = {
            "themes": themes_list,
            "total": len(themes_list),
        }
        return _ok(data, **meta)
    except Exception as exc:  # noqa: BLE001 — 防御性：接口失败不崩溃
        logger.exception("scan_themes failed: %s", exc)
        return _error(
            f"主题扫描失败: {exc}", "THEME_SCAN_FAILED", 500
        )


@theme_bp.route("/quant/themes/runs", methods=["GET"])
def list_theme_runs() -> tuple[Response, int]:
    """列出最近的 theme runs。

    Query Parameters:
        limit: 返回条数上限（默认 20，上限 100）。
        theme_type: 可选，按主题类型过滤。

    Response:
        {
            "ok": true,
            "data": {"runs": [summary, ...], "total": N},
            "as_of": "...",
            "source_snapshot_id": "...",
            "theme_version": "...",
            "strategy_version": "...",
            "degraded": false,
            "degradation_reasons": []
        }
    """
    try:
        limit = request.args.get("limit", 20, type=int)
        limit = max(1, min(limit, 100))
        theme_type = request.args.get("theme_type", "")

        repo = _get_theme_repository()
        packets = repo.list_recent_packets(limit=limit, theme_type=theme_type)

        # 取首个 packet 的元数据作为响应顶层元数据
        meta = _packet_meta(packets[0]) if packets else _empty_meta()
        runs = [_summarize_packet(p) for p in packets]
        data = {"runs": runs, "total": len(runs)}
        return _ok(data, **meta)
    except Exception as exc:  # noqa: BLE001 — 防御性：接口失败不崩溃
        logger.exception("list_theme_runs failed: %s", exc)
        return _error(
            f"查询 theme runs 失败: {exc}", "THEME_RUNS_QUERY_FAILED", 500
        )


@theme_bp.route("/quant/themes/<theme_id>", methods=["GET"])
def get_theme_detail(theme_id: str) -> tuple[Response, int]:
    """获取指定主题详情。

    路径参数 theme_id 可为 theme_id 或 theme_run_id。
    优先按 theme_run_id 精确查询，未命中时按 theme_id 取最新版本。

    Args:
        theme_id: 主题 ID 或主题运行 ID。

    Response:
        {
            "ok": true,
            "data": {完整 ThemePacket.to_dict()},
            "as_of": "...",
            "source_snapshot_id": "...",
            "theme_version": "...",
            "strategy_version": "...",
            "degraded": false,
            "degradation_reasons": []
        }
    """
    try:
        packet = _resolve_packet(theme_id)
        if packet is None:
            return _error(
                f"主题不存在: {theme_id}", "THEME_NOT_FOUND", 404
            )
        meta = _packet_meta(packet)
        return _ok(packet.to_dict(), **meta)
    except Exception as exc:  # noqa: BLE001 — 防御性：接口失败不崩溃
        logger.exception("get_theme_detail failed: %s", exc)
        return _error(
            f"查询主题详情失败: {exc}", "THEME_DETAIL_FAILED", 500
        )


@theme_bp.route("/research/themes/review", methods=["POST"])
def review_theme() -> tuple[Response, int]:
    """Research 审查主题。

    Request body:
        {
            "theme_packet": {...},          // 必填，ThemePacket.to_dict() 格式
            "required_outputs": [...],       // 可选，默认使用 DEFAULT_REQUIRED_OUTPUTS
            "max_new_themes": 2,             // 可选，默认 2，上限 2
            "as_of": "..."                   // 可选，PIT 时点
        }

    Response:
        {
            "ok": true,
            "data": {
                "enhanced_packet": {...},
                "new_theme_suggestions": [...],
                "review_summary": {...}
            },
            "as_of": "...",
            "source_snapshot_id": "...",
            "theme_version": "...",
            "strategy_version": "...",
            "degraded": false,
            "degradation_reasons": []
        }
    """
    try:
        payload = request.get_json(silent=True) or {}
        packet_data = payload.get("theme_packet")
        if not packet_data or not isinstance(packet_data, dict):
            return _error(
                "theme_packet 缺失或非对象", "MISSING_THEME_PACKET", 400
            )

        try:
            theme_packet = ThemePacket.from_dict(packet_data)
        except Exception as exc:  # noqa: BLE001 — 防御性：反序列化失败
            return _error(
                f"theme_packet 反序列化失败: {exc}", "INVALID_THEME_PACKET", 400
            )

        required_outputs = payload.get("required_outputs")
        if required_outputs is not None and not isinstance(required_outputs, list):
            return _error(
                "required_outputs 必须为数组", "INVALID_PARAM", 400
            )

        try:
            max_new_themes = int(payload.get("max_new_themes", 2))
        except (TypeError, ValueError):
            return _error(
                "max_new_themes 必须为整数", "INVALID_PARAM", 400
            )

        as_of = str(payload.get("as_of") or "")

        processor = _get_theme_review_processor()
        result = processor.review(
            theme_packet,
            required_outputs=required_outputs,
            max_new_themes=max_new_themes,
            as_of=as_of,
        )

        data = {
            "enhanced_packet": result.enhanced_packet.to_dict(),
            "new_theme_suggestions": [
                p.to_dict() for p in result.new_theme_suggestions
            ],
            "review_summary": result.review_summary,
        }
        meta = _packet_meta(result.enhanced_packet)
        return _ok(data, **meta)
    except Exception as exc:  # noqa: BLE001 — 防御性：接口失败不崩溃
        logger.exception("review_theme failed: %s", exc)
        return _error(
            f"主题审查失败: {exc}", "THEME_REVIEW_FAILED", 500
        )


@theme_bp.route(
    "/research/themes/<theme_id>/versions", methods=["GET"]
)
def list_theme_versions(theme_id: str) -> tuple[Response, int]:
    """获取主题版本链。

    按 theme_id 查询所有历史版本，按 as_of 降序排列。

    Args:
        theme_id: 主题 ID。

    Response:
        {
            "ok": true,
            "data": {"versions": [...], "total": N},
            "as_of": "...",
            "source_snapshot_id": "...",
            "theme_version": "...",
            "strategy_version": "...",
            "degraded": false,
            "degradation_reasons": []
        }
    """
    try:
        repo = _get_theme_repository()
        versions = repo.list_versions(theme_id)
        if not versions:
            return _error(
                f"主题版本链不存在: {theme_id}", "THEME_VERSIONS_NOT_FOUND", 404
            )

        meta = _packet_meta(versions[0])
        data = {
            "versions": [v.to_dict() for v in versions],
            "total": len(versions),
        }
        return _ok(data, **meta)
    except Exception as exc:  # noqa: BLE001 — 防御性：接口失败不崩溃
        logger.exception("list_theme_versions failed: %s", exc)
        return _error(
            f"查询主题版本链失败: {exc}", "THEME_VERSIONS_QUERY_FAILED", 500
        )


@theme_bp.route(
    "/quant/themes/<theme_id>/daily-screen", methods=["POST"]
)
def daily_screen_theme(theme_id: str) -> tuple[Response, int]:
    """对指定主题执行日线筛选。

    流程：
    1. 从 repository 获取 ThemePacket
    2. 调用 CandidateQuotaGenerator 生成候选
    3. 调用 ThemeDailyScreener 执行筛选
    4. 返回筛选结果

    Args:
        theme_id: 主题 ID 或主题运行 ID。

    Response:
        {
            "ok": true,
            "data": {"screening_results": [...], "total": N},
            "as_of": "...",
            "source_snapshot_id": "...",
            "theme_version": "...",
            "strategy_version": "...",
            "degraded": false,
            "degradation_reasons": []
        }
    """
    try:
        packet = _resolve_packet(theme_id)
        if packet is None:
            return _error(
                f"主题不存在: {theme_id}", "THEME_NOT_FOUND", 404
            )

        generator = _get_candidate_quota_generator()
        candidates = generator.generate([packet])
        _apply_candidate_admission(candidates, packet.as_of)
        admitted_candidates = [
            candidate for candidate in candidates if candidate.admission_passed
        ]

        screener = _get_theme_daily_screener()
        results = screener.screen(
            admitted_candidates, [packet], as_of=packet.as_of
        )

        data = {
            "screening_results": [
                _screening_result_to_dict(r) for r in results
            ],
            "total": len(results),
            "candidate_count": len(candidates),
            "admitted_candidate_count": len(admitted_candidates),
        }
        meta = _packet_meta(packet)
        return _ok(data, **meta)
    except Exception as exc:  # noqa: BLE001 — 防御性：接口失败不崩溃
        logger.exception("daily_screen_theme failed: %s", exc)
        return _error(
            f"主题日线筛选失败: {exc}", "THEME_DAILY_SCREEN_FAILED", 500
        )


@theme_bp.route(
    "/quant/themes/<theme_id>/candidates", methods=["GET"]
)
def list_theme_candidates(theme_id: str) -> tuple[Response, int]:
    """获取指定主题的候选列表。

    流程：
    1. 从 repository 获取 ThemePacket
    2. 调用 CandidateQuotaGenerator 生成候选
    3. 返回候选列表

    Args:
        theme_id: 主题 ID 或主题运行 ID。

    Response:
        {
            "ok": true,
            "data": {"candidates": [...], "total": N},
            "as_of": "...",
            "source_snapshot_id": "...",
            "theme_version": "...",
            "strategy_version": "...",
            "degraded": false,
            "degradation_reasons": []
        }
    """
    try:
        packet = _resolve_packet(theme_id)
        if packet is None:
            return _error(
                f"主题不存在: {theme_id}", "THEME_NOT_FOUND", 404
            )

        generator = _get_candidate_quota_generator()
        candidates = generator.generate([packet])
        _apply_candidate_admission(candidates, packet.as_of)

        data = {
            "candidates": [c.to_dict() for c in candidates],
            "total": len(candidates),
        }
        meta = _packet_meta(packet)
        return _ok(data, **meta)
    except Exception as exc:  # noqa: BLE001 — 防御性：接口失败不崩溃
        logger.exception("list_theme_candidates failed: %s", exc)
        return _error(
            f"查询主题候选失败: {exc}", "THEME_CANDIDATES_QUERY_FAILED", 500
        )
